"""Background worker.

One thread, one loop. Each pass:
- runs the periodic ticks: send collected backlog jobs as one Message Batch, poll open batches,
  re-check jobs waiting for Bazarr, and the daily housekeeping;
- takes the next live job (batch-mode jobs too when the provider has no Batch API) and runs it.

A job is prepared the same way for live and batch: pick a source subtitle, build the profile
and series context, check the budget. Live jobs then call the model; batch jobs send the same
main request in a batch and finish when the result comes back (missing cues and fixes run live).
"""
from __future__ import annotations

import logging
import json
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import bible, cues as cuesmod
from . import maintenance, media, reuse
from .arr import Arr, radarr_id_for_path, sonarr_ids_for_path
from .bazarr import Bazarr
from .config import Settings
from .jellyfin import refresh_all
from .notify import ntfy
from .pipeline import cues_to_send, first_request, translate
from .pricing import cost as price_of
from .pricing import price
from .profile import Profile
from .prompt import context_block, episode_block, system_prompt
from .store import Job, Store, month_now
from .writer import WriteRefused, replace_own, target_path, write_next_to

log = logging.getLogger("captionizerr.worker")

TICK_BATCH_POLL = 300        # seconds between batch status checks
TICK_WAITING = 300           # between checks for a Bazarr download
CUT_PREFIX = "Source cut short:"
SIDECAR_SETTLE = 120         # a new sidecar must be this old (Bazarr syncs right after download)
MAINTENANCE_EVERY = 86400


@dataclass
class Prepared:
    video: Path
    doc: cuesmod.SubtitleDoc
    profile: Profile
    context: str | None
    source: media.Source
    reuse: "reuse.Reuse | None" = None      # translations carried over from a replaced release
    previous: Path | None = None            # that release's (orphaned) translation file
    warning: str = ""                       # the source is incomplete: said in the job detail, job goes to review
    note: str = ""                          # worth saying in the detail, no review needed


class Worker:
    def __init__(self, settings: Settings, store: Store, provider_factory):
        self.s, self.store, self.provider_factory = settings, store, provider_factory
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.current: Job | None = None
        self._last: dict[str, float] = {}
        self._batch_cap: tuple | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        t = threading.Thread(target=self._loop, name="captionizerr-worker", daemon=True)
        t.start()
        return t

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def batch_capable(self) -> bool:
        """Whether the configured provider has a Batch API. Cached per provider setting, so the
        loop doesn't build a new API client every pass."""
        key = (getattr(self.s, "provider", ""), getattr(self.s, "active_model", ""))
        if self._batch_cap is None or self._batch_cap[0] != key:
            try:
                ok = bool(getattr(self.provider_factory(), "supports_batch", False))
            except Exception:
                ok = False
            self._batch_cap = (key, ok)
        return self._batch_cap[1]

    def _loop(self):
        last_month = month_now()
        while not self._stop.is_set():
            if month_now() != last_month:          # new month: budget resets, resume paused jobs
                last_month = month_now()
                n = self.store.resume_paused()
                if n:
                    log.info("new month: resumed %d paused jobs", n)
            self.ticks()
            job = self.store.next_queued(include_batch=not self.batch_capable())
            if job is None:
                self._wake.wait(timeout=30)
                self._wake.clear()
                continue
            self.current = job
            try:
                self.process(job)
            except Exception as e:                  # never let one job kill the worker
                log.exception("job %s crashed", job.id)
                self.store.finish(job.id, "failed", f"{type(e).__name__}: {e}")
            finally:
                self.current = None

    def ticks(self, force: bool = False):
        now = time.time()
        for name, fn, every in (("batch_submit", self.submit_batch, 0), ("batch_poll", self.poll_batches, TICK_BATCH_POLL),
                                ("waiting", self.check_waiting, TICK_WAITING), ("maintenance", self.maybe_maintain, 0)):
            if not force and every and now - self._last.get(name, 0) < every:
                continue
            self._last[name] = now
            try:
                fn()
            except Exception:
                log.exception("tick %s failed", name)

    # ------------------------------------------------------------ preparing a job
    def context_for(self, series_key: str | None) -> str | None:
        from .hooks import valid_key
        if not valid_key(series_key):
            return None
        f = self.s.context_dir / f"{series_key}.md"
        return f.read_text(encoding="utf-8") if f.is_file() else None

    def pinned_source(self, job: Job) -> media.Source | None:
        """The exact source a batch request was built from (its line numbers must still match)."""
        try:
            d = json.loads(job.source_pin or "{}")
            p = Path(d["path"])
            return media.Source(p, d["lang"], d["origin"]) if p.is_file() else None
        except (KeyError, ValueError, TypeError):
            return None

    def prepare(self, job: Job, provider, ask_bazarr: bool = True,
                pinned: media.Source | None = None) -> Prepared | None:
        """Everything before the model call. Returns None when the job was finished or parked here."""
        s, store = self.s, self.store
        video = s.map_path(job.video_path)
        if not s.in_library(video):
            store.finish(job.id, "failed", f"refused: not a video under the library roots {s.library_roots}")
            return None
        if not video.is_file():
            store.finish(job.id, "failed", f"video not found: {video}")
            return None
        if not job.redo and (target_path(video, job.lang, "srt").exists() or target_path(video, job.lang, "ass").exists()):
            store.finish(job.id, "skipped", "translation already exists")
            return None
        try:
            src = pinned or media.find_source(video, s.cache_dir, s.source_langs, job.lang, s.any_source)
        except subprocess.CalledProcessError as e:      # ffprobe/ffmpeg can't open it
            why = ((e.stderr or "") if isinstance(e.stderr, str) else "").strip().splitlines()
            store.finish(job.id, "failed", f"can't read the video ({why[-1][-160:] if why else 'ffprobe failed'}); "
                                           "if the library is on a network share, check the mount and retry")
            return None
        if src and src.origin == "sidecar" and time.time() - src.path.stat().st_mtime < SIDECAR_SETTLE:
            store.set_waiting(job.id, f"{src.path.name} is brand new; waiting for Bazarr's sync to finish")
            return None
        if not src:
            self.ask_bazarr(job, video)
            return None
        doc = cuesmod.load(src.path)
        if not cues_to_send(doc):
            store.finish(job.id, "no_source", f"the {src.lang} subtitle ({src.origin}) has no dialogue lines")
            return None
        warning = ""
        duration = media.probe(video)[1]
        cut = last_cut = media.cut_off(doc.cues, duration)
        if cut is not None and src.origin == "embedded" and media.end_confirmed(video, src.lang, doc.cues, cut, duration):
            cut = None                     # another subtitle ends there too: only the last line was stretched
        if cut is not None:
            where = f"{media.clock(cut)} of {media.clock(duration * 1000)}" if duration else media.clock(cut)
            if ask_bazarr and not job.wait_since and self._ask_bazarr_quietly(job):
                store.set_waiting(job.id, f"{CUT_PREFIX} the {src.lang} subtitle ({src.origin}) stops at {where}; "
                                          f"asked Bazarr for a complete one, translating anyway in "
                                          f"{s.bazarr_wait_hours:g} h if none arrives")
                return None
            warning = (f"incomplete source: the {src.lang} subtitle ({src.origin}) stops at {where}, so the rest of "
                       f"the video has no subtitles. Put a complete {src.lang} subtitle next to the video "
                       f"(e.g. from Bazarr) and press Re-translate")
        inside = media.stretched_inside(doc.cues, last_cut)
        if inside and not warning:
            c = inside[0]
            warning = (f"a source line at {media.clock(c.start_ms)} stayed on screen for "
                       f"{media.clock(c.end_ms - c.start_ms)}, so dialogue may be missing there; it was shortened")
        note = "a source line that stayed on screen over a minute was shortened" if media.clamp_long_cues(doc) else ""
        ser = store.series(job.series_key) if job.series_key else None
        profile = s.profile(job.lang, src.lang, (ser or {}).get("tone"))
        context = self.context_for(job.series_key)
        if context is None and s.auto_notes and job.series_key:
            context = self.make_notes(job, doc, provider)
        prep = Prepared(video, doc, profile, context, src, warning=warning, note=note)
        self.find_reuse(prep, job)
        return prep

    def find_reuse(self, prep: Prepared, job: Job) -> None:
        """An upgrade left our old translation behind: carry it over if the dialogue matches."""
        try:
            old = reuse.predecessor(prep.video, job.lang, self.s.VIDEO_EXT)
            if not old:
                return
            old_src = reuse.stored_source(self.s.data_dir / "sources", old)
            r = reuse.align(prep.doc, cuesmod.load(old), cuesmod.load(old_src[0]) if old_src else None)
            log.info("job %s: previous release %s matches %.0f%% (%s)", job.id, old.name, 100 * r.coverage, r.method)
            if r.coverage >= reuse.MIN_COVERAGE:
                prep.reuse, prep.previous = r, old
        except Exception as e:
            log.warning("job %s: reuse check failed: %s", job.id, e)

    def estimate(self, provider, doc, profile, context) -> float:
        """Free token count of the real prompt, plus output at the calibrated ratio (0.85x episode input)."""
        episode = episode_block(cues_to_send(doc))
        try:
            n_in = provider.count_tokens(system_prompt(profile), context_block(context), episode)
            n_ep = provider.count_tokens("", "", episode)
        except Exception:
            n_in = n_ep = int(len(episode) / 2.5)
        p_in, p_out, _ = price(provider.model) or (4.0, 20.0, 0.2)
        return (n_in * 1.15 * p_in + n_ep * 0.85 * 1.1 * p_out) / 1_000_000

    def over_budget(self, job: Job, est: float, reserved: float = 0.0) -> bool:
        s, store = self.s, self.store
        spent = store.spent()
        in_flight = store.reserved()          # submitted batches count as spent until their results arrive
        if spent + in_flight + reserved + est <= s.monthly_cap_usd:
            return False
        extra = f", ${in_flight:.2f} reserved for open batches" if in_flight else ""
        store.set_state(job.id, "paused_budget",
                        f"estimate ${est:.2f} would exceed the ${s.monthly_cap_usd:.2f} cap (spent ${spent:.2f}{extra})")
        if store.alert_once("cap"):
            ntfy(s.ntfy_url, s.ntfy_topic, "Captionizerr: monthly budget reached",
                 f"Spent ${spent:.2f} of ${s.monthly_cap_usd:.2f}. New translations are paused until the cap is "
                 f"raised or the month resets. Paused: {job.title}.", priority=4, tags=["warning"],
                 click=s.public_url or None)
        return True

    # ------------------------------------------------------------ live job
    def process(self, job: Job):
        provider = self.provider_factory()
        prep = self.prepare(job, provider)
        if prep is None:
            return
        est = self.estimate(provider, prep.doc, prep.profile, prep.context)
        if prep.reuse:
            est *= max(0.05, 1 - prep.reuse.coverage) * 2      # only the unmatched lines (and their context)
        if self.over_budget(job, est):
            return
        what = (f"re-timing the previous release ({100 * prep.reuse.coverage:.0f}% reused)" if prep.reuse
                else f"translating {len(prep.doc.cues)} cues from {prep.source.lang} ({prep.source.origin})")
        self.store.set_state(job.id, "running", f"{what}, est ${est:.2f}")
        res = translate(prep.doc, prep.profile, provider, series_context=prep.context, title=job.title,
                        log=lambda m: log.info("job %s %s", job.id, m.strip()),
                        prefilled=prep.reuse.translations if prep.reuse else None)
        self.finalize(job, prep, res)

    def finalize(self, job: Job, prep: Prepared, res):
        s, store = self.s, self.store
        out_text = cuesmod.render(prep.doc, res.translations)
        try:
            existing = target_path(prep.video, job.lang, prep.doc.fmt)
            if job.redo and existing.exists():
                out = replace_own(existing, out_text, s.history_dir)    # previous version stays in editor history
            else:
                out = write_next_to(prep.video, job.lang, prep.doc.fmt, out_text)
        except WriteRefused as e:
            store.finish(job.id, "skipped", str(e), cost_usd=res.cost() or 0, usage=vars(res.usage))
            return
        state = "needs_review" if res.needs_review or prep.warning else "done"
        cost = res.cost()
        used_model = res.steps[0].model if res.steps else ""
        unpriced = cost is None
        cost = cost or 0.0
        via = " via Batch" if any(st.batch for st in res.steps) else ""
        if prep.reuse:
            via += (f"; re-timed from the previous release: {len(prep.reuse.translations)} lines reused "
                    f"({prep.reuse.method} match), {len(res.translations) - len(prep.reuse.translations)} translated")
        from .editor import FLAG_LABEL
        open_ = "".join("; open: " + ", ".join(f"#{i.index} {FLAG_LABEL.get(i.kind, i.kind.replace('_', ' '))}"
                                                 for i in res.issues_final[:4]) if res.issues_final else "")
        detail = (f"{len(res.translations)} cues from {prep.source.lang} ({prep.source.origin}){via}; "
                  f"{len(res.issues_initial)} flagged, {len(res.issues_final)} open{open_}")
        if prep.warning:
            detail = f"{prep.warning[0].upper()}{prep.warning[1:]}. " + detail
        elif prep.note:
            detail += f"; {prep.note}"
        if unpriced:   # never pretend it was free: say so where the owner will see it
            detail += f"; cost unknown: no price for {used_model} (set it in Settings → AI provider)"
            log.warning("job %s: no price known for %s; cost recorded as $0", job.id, used_model)
        try:
            reuse.store_source(s.data_dir / "sources", out, prep.source.path, prep.source.lang)
            if prep.previous:
                maintenance.quarantine(s, prep.previous)        # replaced: out of the library, kept 30 days
        except Exception as e:
            log.warning("job %s: source copy / quarantine: %s", job.id, e)
        store.finish(job.id, state, detail, str(out), cost, vars(res.usage), used_model)
        forced_cost = self.translate_forced(job, prep)
        refresh_all(s, [job.video_path])
        self.notify_done(job, prep.doc, res, state, cost + forced_cost)
        total = store.spent()
        if total >= s.alert_at * s.monthly_cap_usd and store.alert_once("warn"):
            ntfy(s.ntfy_url, s.ntfy_topic, "Captionizerr: budget at "
                 f"{100 * total / s.monthly_cap_usd:.0f}%", f"Spent ${total:.2f} of ${s.monthly_cap_usd:.2f} this month.",
                 priority=3, tags=["moneybag"])

    def translate_forced(self, job: Job, prep: Prepared) -> float:
        """Also translate the forced track, if the file has one, into `….<lang>.forced.<fmt>`.
        Small (tens of lines), always live. Never fails the main job. Returns its cost."""
        s = self.s
        try:
            if not job.redo and (target_path(prep.video, job.lang, "srt", True).exists()
                                 or target_path(prep.video, job.lang, "ass", True).exists()):
                return 0.0
            src = media.find_forced(prep.video, s.cache_dir, s.source_langs, job.lang)
            if not src:
                return 0.0
            doc = cuesmod.load(src.path)
            if not cues_to_send(doc):
                return 0.0
            provider = self.provider_factory()
            res = translate(doc, s.profile(job.lang, src.lang, prep.profile.tone), provider,
                            series_context=prep.context, title=f"{job.title} (forced: foreign-language lines and signs)",
                            log=lambda m: log.info("job %s forced %s", job.id, m.strip()))
            text = cuesmod.render(doc, res.translations)
            existing = target_path(prep.video, job.lang, doc.fmt, True)
            out = (replace_own(existing, text, self.s.history_dir) if job.redo and existing.exists()
                   else write_next_to(prep.video, job.lang, doc.fmt, text, forced=True))
            c = res.cost() or 0.0
            self.store.record("forced", f"{job.title}: forced track", job.video_path, job.lang, job.series_key,
                              f"{len(res.translations)} lines → {out.name}", c, vars(res.usage),
                              res.steps[0].model if res.steps else "")
            return c
        except Exception as e:
            log.warning("job %s: forced track not translated: %s", job.id, e)
            return 0.0

    # ------------------------------------------------------------ backlog via the Batch API
    def submit_batch(self):
        """Send every collected backlog job as one batch once the newest is `batch_min_wait_s` old
        (so a season ticked in the UI goes out together and shares the cached series context)."""
        jobs = self.store.queued_batch()
        if not jobs or not self.batch_capable():
            return
        if time.time() - max(j.created for j in jobs) < self.s.batch_min_wait_s:
            return
        provider = self.provider_factory()
        items, sent, reserved = [], [], 0.0
        for job in jobs:
            if not self.store.claim(job.id, "queued", "running", "preparing for batch"):
                continue
            try:
                prep = self.prepare(job, provider)
                if prep is None:
                    continue
                if prep.reuse:                     # mostly carried over: a few live lines beat a batch
                    self.store.to_live(job.id, "previous release can be re-timed; running live")
                    self.wake()
                    continue
                est = self.estimate(provider, prep.doc, prep.profile, prep.context) / 2
                if self.over_budget(job, est, reserved):
                    continue
                reserved += est
                items.append((str(job.id), provider.batch_params(*first_request(prep.doc, prep.profile,
                                                                                prep.context, job.title))))
                sent.append((job, est, prep.source))
            except Exception as e:
                log.exception("batch prep failed for job %s", job.id)
                self.store.finish(job.id, "failed", f"{type(e).__name__}: {e}")
        if not items:
            return
        try:
            bid = provider.submit_batch(items)
        except Exception as e:
            for job, _, _ in sent:
                self.store.finish(job.id, "failed", f"batch submit failed: {e}")
            return
        for job, est, src in sent:
            self.store.set_batched(job.id, bid, f"in batch {bid} (est ${est:.2f}); results within 24 h", est,
                                   json.dumps({"path": str(src.path), "lang": src.lang, "origin": src.origin}))
        log.info("submitted batch %s with %d jobs, est $%.2f", bid, len(sent), reserved)

    def batch_provider(self):
        """Batches are always Anthropic's. If the owner switched to another provider while a batch
        was open, still collect it with Anthropic instead of stranding the jobs."""
        p = self.provider_factory()
        if getattr(p, "supports_batch", False):
            return p
        from .providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=self.s.model, effort=self.s.effort, api_key=self.s.anthropic_key or None)

    def poll_batches(self):
        groups = self.store.batched()
        if not groups:
            return
        provider = self.batch_provider()
        for bid, jobs in groups.items():
            try:
                if provider.batch_status(bid) != "ended":
                    continue
                results = provider.batch_results(bid)
            except Exception as e:
                log.warning("batch %s: %s", bid, e)
                continue
            for job in jobs:
                self.finish_batched(job, results.get(str(job.id)), provider)

    def finish_batched(self, job: Job, result, provider):
        store = self.store
        if not store.claim(job.id, "batched", "running", "batch result arrived; checking and fixing"):
            return
        if result is None or isinstance(result, Exception):
            # refusals (no fallback inside a batch), expiry and errors: run it live instead
            store.to_live(job.id, f"batch: {result or 'no result'}; retrying live")
            self.wake()
            return
        try:
            prep = self.prepare(job, provider, ask_bazarr=False,   # the batch is already paid for
                                pinned=self.pinned_source(job))
            if prep is None:
                return
            res = translate(prep.doc, prep.profile, provider, series_context=prep.context, title=job.title,
                            log=lambda m: log.info("job %s %s", job.id, m.strip()), first=result)
            self.finalize(job, prep, res)
        except Exception as e:
            log.exception("batch job %s failed", job.id)
            store.finish(job.id, "failed", f"{type(e).__name__}: {e}",
                         cost_usd=price_of(result.model, result.usage, True) or 0)

    # ------------------------------------------------------------ Bazarr fallback
    def ask_bazarr(self, job: Job, video: Path):
        s, store = self.s, self.store
        want = ", ".join(s.source_langs)
        bz = Bazarr(s.bazarr_url, s.bazarr_key)
        if not bz.configured:
            store.finish(job.id, "no_source", f"no text subtitle in {want} (or any other language) in or next to the "
                                              "file; set up Bazarr to fetch one")
            return
        if job.wait_since:
            if time.time() - job.wait_since > s.bazarr_wait_hours * 3600:
                store.finish(job.id, "no_source", f"Bazarr found no {want} subtitle within {s.bazarr_wait_hours:g} h")
            else:
                store.set_waiting(job.id, f"waiting for Bazarr to download a {want} subtitle")
            return
        try:
            asked = self._bazarr_request(bz, job)
        except Exception as e:
            store.finish(job.id, "no_source", f"no source subtitle, and asking Bazarr failed: {e}")
            return
        if not asked:
            store.finish(job.id, "no_source", "no source subtitle; Sonarr/Radarr don't know this file, so Bazarr can't search")
            return
        store.set_waiting(job.id, f"asked Bazarr for a {want} subtitle; checking every few minutes")

    def _ask_bazarr_quietly(self, job: Job) -> bool:
        """Ask Bazarr for a better source without failing the job when it can't help."""
        bz = Bazarr(self.s.bazarr_url, self.s.bazarr_key)
        if not bz.configured:
            return False
        try:
            return self._bazarr_request(bz, job)
        except Exception as e:
            log.info("job %s: asking Bazarr for a complete source failed: %s", job.id, e)
            return False

    def _bazarr_request(self, bz: Bazarr, job: Job) -> bool:
        s = self.s
        langs = [l for l in s.source_langs if l != job.lang][:2] or ["en"]
        if s.sonarr_url and s.sonarr_key and job.source != "radarr":
            ids = sonarr_ids_for_path(Arr(s.sonarr_url, s.sonarr_key), job.video_path)
            if ids:
                for l in langs:
                    bz.request_episode(ids[0], ids[1], l)
                return True
        if s.radarr_url and s.radarr_key and job.source != "sonarr":
            rid = radarr_id_for_path(Arr(s.radarr_url, s.radarr_key), job.video_path)
            if rid:
                for l in langs:
                    bz.request_movie(rid, l)
                return True
        return False

    def check_waiting(self):
        s = self.s
        for job in self.store.waiting():
            video = s.map_path(job.video_path)
            if not video.is_file():
                self.store.finish(job.id, "failed", f"video not found: {video}")
                continue
            side = [p for p, l, _ in media.sidecars(video) if l != job.lang]
            settled = [p for p in side if time.time() - p.stat().st_mtime >= SIDECAR_SETTLE]
            if job.detail.startswith(CUT_PREFIX):   # only a subtitle that arrived after we asked can help
                settled = [p for p in settled if p.stat().st_mtime > job.wait_since]
            if settled:
                self.store.claim(job.id, "waiting_source", "queued", f"source arrived: {settled[0].name}")
                self.wake()
            elif job.wait_since and time.time() - job.wait_since > s.bazarr_wait_hours * 3600:
                if job.detail.startswith(CUT_PREFIX):     # it has a source, just an incomplete one: use it
                    self.store.claim(job.id, "waiting_source", "queued", "no complete subtitle arrived; "
                                                                        "translating the incomplete one")
                    self.wake()
                    continue
                self.store.finish(job.id, "no_source",
                                  f"Bazarr found no {', '.join(s.source_langs)} subtitle within {s.bazarr_wait_hours:g} h")

    # ------------------------------------------------------------ housekeeping
    def maybe_maintain(self):
        last = (self.store.load_settings().get("_maintenance_last") or {}).get("at", 0)
        if time.time() - last >= MAINTENANCE_EVERY:
            maintenance.run(self.s, self.store)

    # ------------------------------------------------------------ series notes
    def make_notes(self, job: Job, doc, provider) -> str | None:
        """First episode of a series without notes: draft them from the overview + this episode."""
        s, store = self.s, self.store
        if store.spent() + 0.5 > s.monthly_cap_usd:
            return None
        title = (store.series(job.series_key) or {}).get("title") or job.title.rsplit(" S", 1)[0]
        overview = ""
        if s.sonarr_url and s.sonarr_key:
            try:
                from .arr import sonarr_series
                from .hooks import slug
                for x in sonarr_series(Arr(s.sonarr_url, s.sonarr_key)):
                    if slug(x["title"]) == job.series_key:
                        title, overview = x["title"], x["overview"]
                        break
            except Exception as e:                       # notes are a bonus, never block the job
                log.warning("notes: Sonarr lookup failed: %s", e)
        try:
            text, res = bible.generate_notes(provider, title, overview, job.lang, doc.cues)
        except Exception as e:
            log.warning("notes: generation failed for %s: %s", job.series_key, e)
            return None
        if not text.strip():
            return None
        s.context_dir.mkdir(parents=True, exist_ok=True)
        f = s.context_dir / f"{job.series_key}.md"
        if f.exists():                                   # someone wrote notes meanwhile: theirs win
            return f.read_text(encoding="utf-8")
        f.write_text(text.strip() + "\n", encoding="utf-8")
        store.record("notes", f"{title}: series notes drafted", job.video_path, job.lang, job.series_key,
                     "auto-generated from the overview and this episode; edit them on the series page",
                     price_of(res.model, res.usage) or 0.0, vars(res.usage), res.model)
        return text

    def notify_done(self, job: Job, doc, res, state: str, cost: float):
        by = {c.index: c for c in doc.cues}
        flagged = [i.index for i in res.issues_initial if res.translations.get(i.index)]
        pool = [n for n, t in res.translations.items() if t and n in by and len(by[n].text) > 15]
        pick = list(dict.fromkeys(flagged[:4] + random.sample(pool, min(10, len(pool)))))[:10]
        lines = [f"• {by[n].text.replace(chr(10), ' ')}\n  → {res.translations[n].replace(chr(10), ' ')}" for n in sorted(pick)]
        head = "needs review: " + "; ".join(f"#{i.index} {i.kind}" for i in res.issues_final[:5]) + "\n\n" \
            if state == "needs_review" else ""
        ntfy(self.s.ntfy_url, self.s.ntfy_topic,
             f"Subtitles ({job.lang}) ready: {job.title}",
             f"{head}${cost:.2f} · spot-check:\n\n" + "\n".join(lines),
             priority=4 if state == "needs_review" else 2, tags=["speech_balloon"], click=self.s.public_url or None)
