"""Web UI: dashboard, library, per-series settings, global settings, providers.

Server-rendered (Jinja2), no JavaScript build. Every state-changing request is a
POST that must come from this UI's own pages (Origin/Referer check), because the
UI has no login on a trusted LAN and some actions spend money.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..arr import Arr, ArrError, radarr_movies, sonarr_episodes, sonarr_series
from ..hooks import slug, valid_key
from ..pricing import PRICES
from ..profile import LANG_NAMES, TONE_RULES
from ..store import month_now
from .. import bible, cues as cuesmod, editor, media, reuse
from ..jellyfin import refresh_all
from ..pricing import cost as price_of
from ..writer import WriteRefused, replace_own, target_path

log = logging.getLogger("captionizerr.web")

HERE = Path(__file__).parent
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
from ..store import STATES


_ORIGIN = {"embedded": "the built-in {L} track",
           "embedded + sidecar ending": "the built-in {L} track, its missing ending from a downloaded subtitle",
           "sidecar": "a downloaded {L} subtitle", "sidecar, re-timed": "a downloaded {L} subtitle (re-timed)",
           "bazarr": "a {L} subtitle from Bazarr",
           "speech": "the {L} audio (speech recognition)"}


def human_detail(detail: str) -> str:
    """A job's detail line in plain words. The stored text stays as the worker wrote it (stable for
    tests and old rows); only what is shown changes."""
    if not detail:
        return ""

    def src(m):
        origin = m.group(3)
        synced = origin.endswith(", synced to speech")
        origin = origin.removesuffix(", synced to speech")
        lang = LANG_NAMES.get(m.group(2), m.group(2))
        what = _ORIGIN.get(origin, f"{lang} ({origin})").replace("{L}", lang)
        return f"{m.group(1)} lines from {what}" + (", synced to the speech" if synced else "")
    d = re.sub(r"(\d+) cues from ([a-z]{2,3}) \(([^)]*)\)", src, detail)
    d = re.sub(r"(\d+) cues; ", r"\1 lines; ", d)

    soft_only = bool(re.search(r"open: ", d)) and all(
        x.strip().split(" ", 1)[-1] in ("speaker label?", "italics lost")
        for x in re.sub(r"^.*?open: ", "", d).split(";")[0].split(", "))

    def checks(m):
        n, left = int(m.group(1)), int(m.group(2))
        if not n:
            return "no lines needed fixing"
        if left and soft_only:
            return f"{n} lines checked and fixed, {left} small note{'s' if left != 1 else ''} left (no review needed)"
        return (f"{n} line{'s' if n != 1 else ''} checked and fixed" if not left
                else f"{n} lines checked, {left} still need{'s' if left == 1 else ''} a look")
    d = re.sub(r"(\d+) flagged, (\d+) open", checks, d)
    d = d.replace(" via Batch", " via the Batch API (half price)")
    d = d.replace("translated lines follow", "translated lines moved with them")
    return d


def share_comment(model: str, history) -> str:
    """The OpenSubtitles comment, saying where the subtitle really came from: the release's own
    track, a completed one, or a downloaded one, and whether its timing was checked against speech."""
    last = next((j for j in reversed(history) if j.source not in ("edit", "notes", "forced", "sync")
                 and j.state in ("done", "needs_review")), None)
    detail = last.detail if last else ""
    m = re.search(r"cues from [a-z]{2,3} \(([^)]*)\)", detail)
    origin = m.group(1) if m else "embedded"
    synced = "synced to speech" in origin or any(
        j.source == "sync" and j.state == "done" and "moved" in (j.detail or "") and j.created > (last.created if last else 0)
        for j in history)
    origin = origin.replace(", synced to speech", "")
    if origin.startswith("embedded + sidecar"):
        what = "of this release's own subtitles, with the missing ending completed from another subtitle"
    elif origin.startswith("sidecar"):
        what = "of a downloaded subtitle, re-timed to this release"
    else:
        what = "of this release's own subtitles, timed to the file"
    return (f"AI translation ({model}) {what}" + ("; timing checked against the speech" if synced else "")
            + "; reviewed by the uploader.")


def file_marks(jobs) -> tuple[list[tuple[str, str]], int | None]:
    """Small status markers for one file's translation, and the latest translation job's id."""
    side = ("edit", "notes", "forced", "sync")
    last = next((j for j in reversed(jobs) if j.source not in side), None)
    marks: list[tuple[str, str]] = []
    if last is not None:
        if last.state in ("queued", "running", "batched", "waiting_source"):
            marks.append(("in progress", "running"))
        elif last.state == "needs_review":
            marks.append(("needs review", "needs_review"))
        elif last.state in ("failed", "no_source"):
            marks.append(("failed" if last.state == "failed" else "no source", "failed"))
        if "Incomplete source" in (last.detail or "") or "Source cut short" in (last.detail or ""):
            marks.append(("source cut short", "warn"))
        elif "sidecar ending" in (last.detail or ""):
            marks.append(("ending completed", ""))
        if "synced to speech" in (last.detail or "") or any(
                j.source == "sync" and j.state == "done" and "moved" in (j.detail or "") and j.created > last.created for j in jobs):
            marks.append(("re-synced", "done"))
    return marks, (last.id if last else None)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B" if n else "empty"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1048576:.1f} MB" if n < 1024 ** 3 else f"{n / 1024 ** 3:.2f} GB"


def short_title(path: str) -> str:
    """"The Expanse S02E05" from a video or subtitle file name; the plain stem when there is no SxxEyy."""
    stem = Path(path).name.split(".captionizerr.")[0]
    stem = re.sub(r"\.(mkv|mp4|m4v|avi|mov|ts|webm|srt|ass)$", "", stem, flags=re.I)
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", stem)
    if not m:
        return stem
    series = re.split(r"[\s._-]+[Ss]\d{1,2}[Ee]\d", stem)[0].replace(".", " ").strip(" -_")
    return f"{series} S{int(m.group(1)):02d}E{int(m.group(2)):02d}".strip()


def gone_videos(paths, roots) -> set[str]:
    """Video paths whose file no longer exists (the series or movie was deleted on the server).
    A path counts as gone only when its library root is reachable and not empty, so a dropped
    network mount doesn't make every row look deleted."""
    alive: dict[str, bool] = {}

    def root_ok(r: str) -> bool:
        if r not in alive:
            try:
                with os.scandir(r) as it:
                    alive[r] = any(True for _ in it)
            except OSError:
                alive[r] = False
        return alive[r]

    gone = set()
    for p in {p for p in paths if p}:
        root = next((r for r in roots if p.startswith(r.rstrip("/") + "/")), None)
        if root and root_ok(root) and not os.path.exists(p):
            gone.add(p)
    return gone
STATE_LABEL = {"queued": "Queued", "running": "Running", "batched": "In batch", "waiting_source": "Waiting for source", "done": "Done", "needs_review": "Needs review",
               "failed": "Failed", "paused_budget": "Paused (budget)", "skipped": "Skipped", "no_source": "No source"}
# calibrated output/input ratio and default sizes, for the per-episode price hint on the providers page
EP_IN, EP_OUT = 16_000, 12_000


class Preparing(Exception):
    """The editor's source track is still being extracted."""


def _same_origin(request: Request) -> None:
    host = request.headers.get("host", "")
    src = request.headers.get("origin") or request.headers.get("referer") or ""
    if not src or urllib.parse.urlparse(src).netloc != host:
        raise HTTPException(403, "cross-site request refused")


def local_path(target, default: str = "/") -> str:
    """A redirect/back target from a request: only a path on this site. Rejects other hosts in any
    disguise browsers accept (//host, /\\host, scheme:, control characters)."""
    t = str(target or "")
    if not t.startswith("/") or t.startswith(("//", "/\\")) or "\\" in t or any(ord(c) < 32 for c in t):
        return default
    return t


def _redirect(path: str, msg: str = "", level: str = "ok") -> RedirectResponse:
    if msg:
        sep = "&" if "?" in path else "?"
        path += f"{sep}{urllib.parse.urlencode({'msg': msg, 'lvl': level})}"
    return RedirectResponse(path, status_code=303)


def _ep_cost(model: str) -> float:
    p_in, p_out, _ = PRICES[model]
    return (EP_IN * p_in + EP_OUT * p_out) / 1_000_000


def register(app: FastAPI) -> None:
    store, s = app.state.store, app.state.settings
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(version=__version__, state_label=STATE_LABEL, lang_names=LANG_NAMES)
    templates.env.filters["money"] = lambda v: f"${v:.3f}" if 0 < (v or 0) < 0.1 else f"${(v or 0):.2f}"
    templates.env.filters["datefmt"] = lambda t: datetime.fromtimestamp(t).strftime("%d %b %Y") if t else ""
    templates.env.filters["human"] = human_detail
    templates.env.filters["size"] = human_size
    templates.env.filters["num"] = lambda v: f"{v:g}" if isinstance(v, float) else v
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("setup_needed", getattr(app.state, "setup_needed", lambda: False)())
        ctx.update(request=request, nav=name.split(".")[0], msg=request.query_params.get("msg"),
                   lvl=request.query_params.get("lvl", "ok"), settings=s)
        return templates.TemplateResponse(request, name, ctx)

    def sonarr() -> Arr:
        return Arr(s.sonarr_url, s.sonarr_key)

    def radarr() -> Arr:
        return Arr(s.radarr_url, s.radarr_key)

    def langs_for(key: str | None) -> list[str]:
        ser = store.series(key) if key else None
        return (ser and ser["langs"]) or s.languages

    def status_for(path: str, langs: list[str]) -> dict[str, bool]:
        local = s.map_path(path)
        return {l: target_path(local, l, "srt").exists() or target_path(local, l, "ass").exists() for l in langs}

    # ------------------------------------------------------------------ dashboard
    @app.get("/")
    def dashboard(request: Request, state: str = "", replaced: str = ""):
        if getattr(app.state, "setup_needed", lambda: False)() and not request.query_params.get("msg"):
            return RedirectResponse("/setup", 307)
        all_jobs = store.list(300)
        side = ("edit", "notes", "forced", "sync")
        groups = {"attention": ("needs_review", "failed", "no_source", "paused_budget"),
                  "active": ("queued", "running", "batched", "waiting_source"), "done": ("done", "skipped")}
        view = state if state in (*groups, "other") or state in STATES else ""
        if view == "other":
            jobs = [j for j in all_jobs if j.source in side]
        else:
            jobs = [j for j in all_jobs if j.source not in side]
            if view in groups:
                jobs = [j for j in jobs if j.state in groups[view]]
            elif view:
                jobs = [j for j in jobs if j.state == view]
        counts = store.counts()
        spent, cap, reserved = store.spent(), s.monthly_cap_usd, store.reserved()
        # an older translation of a file that was translated again: its file is gone, so no Edit for it
        show_replaced = bool(replaced)
        seen, replaced = set(), set()
        for j in jobs:                                   # newest first
            if j.source in side or j.state not in ("done", "needs_review"):
                continue
            k = (j.video_path, j.lang)
            if k in seen:
                replaced.add(j.id)
            seen.add(k)
        hidden_replaced = 0
        if not show_replaced and view != "other":        # older versions of re-translated files: hidden by default
            hidden_replaced = sum(1 for j in jobs if j.id in replaced)
            jobs = [j for j in jobs if j.id not in replaced]
        jobs = jobs[:100]
        gone = gone_videos([j.video_path for j in jobs], s.library_roots)
        deleted = {j.id for j in jobs if j.video_path in gone}
        return render(request, "dashboard.html", jobs=jobs, counts=counts, replaced=replaced, deleted=deleted,
                      hidden_replaced=hidden_replaced, show_replaced=show_replaced,
                      running=[j for j in all_jobs if j.state == "running"][:3], now=time.time(),
                      spent=spent, cap=cap,
                      reserved=reserved, pct=min(100, 100 * (spent + reserved) / cap) if cap else 0, month=month_now(),
                      months=store.spend_by_month(), view=view, avg=episode_estimate(),
                      attention={st: counts.get(st, 0) for st in groups["attention"]},
                      active={st: counts.get(st, 0) for st in groups["active"]},
                      side_count=sum(1 for j in all_jobs if j.source in side),
                      fmt_time=lambda t: datetime.fromtimestamp(t).strftime("%d %b %H:%M"))

    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: int):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404)
        import json
        return render(request, "job.html", job=job, usage=json.loads(job.usage or "{}"), replaced_by=store.replaced_by(job),
                      deleted=bool(gone_videos([job.video_path], s.library_roots)),
                      history=store.jobs_for(job.video_path, job.lang),
                      fmt_time=lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S"))

    @app.post("/jobs/{job_id}/retry")
    def job_retry(request: Request, job_id: int):
        _same_origin(request)
        ok = store.requeue(job_id)
        app.state.worker.wake()
        return _redirect("/", f"Job {job_id} queued again." if ok else f"Job {job_id} can't be retried.", "ok" if ok else "warn")

    @app.post("/jobs/{job_id}/retranslate")
    def job_retranslate(request: Request, job_id: int):
        """Translate the same video and language again, replacing our file (the old one goes to the editor's history)."""
        _same_origin(request)
        j = store.get(job_id)
        if not j or j.source in ("edit", "notes", "forced", "sync"):
            return _redirect("/", "Nothing to re-translate.", "warn")
        if gone_videos([j.video_path], s.library_roots):
            return _redirect(f"/jobs/{job_id}", "The video was deleted on the server, so there is nothing to translate.", "warn")
        new = store.enqueue("manual", j.title, j.video_path, j.lang, j.series_key, redo=True)
        app.state.worker.wake()
        if not new:
            return _redirect(f"/jobs/{job_id}", "Already queued or running.", "warn")
        return _redirect("/", f"Re-translating {j.title} ({j.lang}) with {s.active_model}, "
                              f"about ${episode_estimate():.2f}. The previous version is kept in the editor's history.")

    @app.post("/jobs/{job_id}/resync")
    def job_resync(request: Request, job_id: int):
        """Move this translation onto the speech (Whisper). Free: nothing is re-translated."""
        _same_origin(request)
        j = store.get(job_id)
        if not j or j.source in ("edit", "notes", "forced", "sync"):
            return _redirect("/", "Nothing to re-sync.", "warn")
        if not s.whisper_url:
            return _redirect(f"/jobs/{job_id}", "Set up a Whisper service first (Settings → Connections).", "warn")
        if gone_videos([j.video_path], s.library_roots):
            return _redirect(f"/jobs/{job_id}", "The video was deleted on the server.", "warn")
        new = store.enqueue("sync", f"{j.title}: re-sync to speech", j.video_path, j.lang, j.series_key)
        app.state.worker.wake()
        if not new:
            return _redirect(f"/jobs/{job_id}", "Something is already running for this file.", "warn")
        return _redirect("/", f"Re-syncing {j.title} ({j.lang}) to the speech: a few minutes, no cost. "
                              "The previous timing is kept in the editor's history.")

    @app.post("/jobs/resume")
    def jobs_resume(request: Request):
        _same_origin(request)
        n = store.resume_paused()
        app.state.worker.wake()
        return _redirect("/", f"Resumed {n} paused job(s)." if n else "No paused jobs.")

    # ------------------------------------------------------------------ library
    @app.get("/library")
    def library(request: Request):
        series, movies, err = [], [], []
        try:
            series = sonarr_series(sonarr())
        except ArrError as e:
            err.append(f"Sonarr: {e}")
        try:
            movies = radarr_movies(radarr())
            for m in movies:
                m["key"] = slug(m["title"])
                m["status"] = status_for(m["path"], langs_for(m["key"]))
        except ArrError as e:
            err.append(f"Radarr: {e}")
        for x in series:
            x["key"] = slug(x["title"])
            x["override"] = store.series(x["key"])
            x["translated"] = translated_count(x.get("path"), langs_for(x["key"]))
        film_langs = sorted({l for m in movies for l in m["status"]}) or list(s.languages)
        for l in film_langs:
            jobs = store.jobs_by_video([m["path"] for m in movies], l)
            for m in movies:
                if l in m["status"]:
                    m.setdefault("review", {})[l] = any(mk == "needs review" for mk, _ in file_marks(jobs.get(m["path"], []))[0])
        configured = {"sonarr": bool(s.sonarr_url and s.sonarr_key), "radarr": bool(s.radarr_url and s.radarr_key)}
        return render(request, "library.html", series=series, movies=movies, errors=err, avg=episode_estimate(),
                      configured=configured, film_langs=film_langs)

    def translated_count(series_path: str | None, langs: list[str]) -> int:
        """Episodes with our translation in every wanted language: a folder scan, no Sonarr calls."""
        if not series_path or not langs:
            return 0
        root = s.map_path(series_path)
        if not root.is_dir():
            return 0
        import os
        from ..writer import _SIDECAR
        have: dict[str, set] = {}
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                m = _SIDECAR.match(f)
                if m and not m.group("forced"):
                    have.setdefault(os.path.join(dirpath, m.group("stem")), set()).add(m.group("lang"))
        return sum(1 for got in have.values() if set(langs) <= got)

    @app.get("/library/series/{series_id}")
    def series_page(request: Request, series_id: int):
        return series_view(request, series_id)

    def series_view(request: Request, series_id: int, proposed: str | None = None, note: str = ""):
        try:
            allseries = {x["id"]: x for x in sonarr_series(sonarr())}
            eps = sonarr_episodes(sonarr(), series_id)
        except ArrError as e:
            return _redirect("/library", f"Sonarr: {e}", "warn")
        ser = allseries.get(series_id)
        if not ser:
            raise HTTPException(404)
        key = slug(ser["title"])
        langs = langs_for(key)
        seasons: dict[int, list] = {}
        by_lang = {l: store.jobs_by_video([e["path"] for e in eps if e.get("path")], l) for l in langs}
        for e in eps:
            e["status"] = status_for(e["path"], langs)
            e["missing"] = not all(e["status"].values())
            e["marks"], e["job"], e["review"] = [], None, {}
            for l in langs:
                jobs = by_lang[l].get(e.get("path") or "", [])
                marks, job_id = file_marks(jobs)
                e["review"][l] = any(m == "needs review" for m, _ in marks)
                e["marks"] += [(f"{l}: {m}" if len(langs) > 1 else m, c) for m, c in marks]
                e["job"] = e["job"] or job_id
            seasons.setdefault(e["season"], []).append(e)
        ctx_file = s.context_dir / f"{key}.md"
        saved = ctx_file.read_text(encoding="utf-8") if ctx_file.is_file() else ""
        resp = render(request, "series.html", ser=ser, key=key, seasons=seasons, langs=langs,
                      override=store.series(key) or {"enabled": True, "langs": [], "tone": ""},
                      context=proposed if proposed is not None else saved, proposed=proposed is not None,
                      corrections=len(bible.corrections(s.feedback_dir, key)), tones=list(TONE_RULES),
                      avg=episode_estimate(), note=note)
        return resp

    def first_source(series_id: int):
        """Cues of the first episode that has a file and a usable source track."""
        for e in sorted(sonarr_episodes(sonarr(), series_id), key=lambda e: (e["season"] == 0, e["season"], e["episode"])):
            local = s.map_path(e["path"])
            if s.in_library(local) and local.is_file():
                src = media.find_source(local, s.cache_dir, s.source_langs, (langs_for(None) or ["en"])[0], s.any_source)
                if src:
                    return e, cuesmod.load(src.path)
        return None, None

    def episode_estimate() -> float:
        """Typical cost of one episode with the model in use now: its own recent average once it has
        3+ jobs, else its price (built-in / OpenRouter / LiteLLM, cached), else a conservative $0.31."""
        model = s.active_model
        avg = store.avg_cost(default=None, model=model, min_jobs=3)
        if avg is not None:
            return avg
        from ..pricing import price
        from ..price_lookup import EP_IN, EP_OUT
        p = price(model)
        if p is None and s.provider == "openai":
            from ..price_lookup import lookup
            pl = lookup("openai", s.openai_base_url, model, s.cache_dir)
            p = (pl.inp, pl.out, pl.cache_read) if pl else None
        if p is None and s.provider == "ollama":
            return 0.0
        return (EP_IN * p[0] + EP_OUT * p[1]) / 1_000_000 if p else 0.31

    app.state.episode_estimate = episode_estimate

    def budget_left(need: float) -> bool:
        return store.spent() + store.reserved() + need <= s.monthly_cap_usd

    @app.post("/library/series/{series_id}/notes")
    def series_notes(request: Request, series_id: int):
        """Draft notes with the model; shown in the form, saved only when the user saves."""
        _same_origin(request)
        if not budget_left(0.5):
            return _redirect(f"/library/series/{series_id}", "Monthly budget reached; notes not generated.", "warn")
        try:
            ser = {x["id"]: x for x in sonarr_series(sonarr())}.get(series_id)
            if not ser:
                raise HTTPException(404)
            ep, doc = first_source(series_id)
        except ArrError as e:
            return _redirect(f"/library/series/{series_id}", f"Sonarr: {e}", "warn")
        if not doc:
            return _redirect(f"/library/series/{series_id}", "No episode with an English text track to learn from.", "warn")
        key = slug(ser["title"])
        try:
            text, res = bible.generate_notes(app.state.worker.provider_factory(), ser["title"], ser.get("overview", ""),
                                             (langs_for(key) or ["en"])[0], doc.cues)
        except Exception as e:
            return _redirect(f"/library/series/{series_id}", f"Generation failed: {type(e).__name__}: {str(e)[:160]}", "warn")
        c = price_of(res.model, res.usage) or 0.0
        store.record("notes", f"{ser['title']}: series notes drafted", ep["path"], (langs_for(key) or ["en"])[0], key,
                     "generated from the series page", c, vars(res.usage), res.model)
        return series_view(request, series_id, proposed=text,
                           note=f"Draft notes below (${c:.3f}), not saved yet. Edit, then save series settings.")

    @app.post("/library/series/{series_id}/learn")
    def series_learn(request: Request, series_id: int):
        _same_origin(request)
        try:
            ser = {x["id"]: x for x in sonarr_series(sonarr())}.get(series_id)
        except ArrError as e:
            return _redirect(f"/library/series/{series_id}", f"Sonarr: {e}", "warn")
        if not ser:
            raise HTTPException(404)
        key = slug(ser["title"])
        items = bible.corrections(s.feedback_dir, key)
        if not items:
            return _redirect(f"/library/series/{series_id}", "No corrections recorded yet.", "warn")
        if not budget_left(0.3):
            return _redirect(f"/library/series/{series_id}", "Monthly budget reached.", "warn")
        f = s.context_dir / f"{key}.md"
        notes = f.read_text(encoding="utf-8") if f.is_file() else ""
        try:
            text, res = bible.learn(app.state.worker.provider_factory(), notes, items)
        except Exception as e:
            return _redirect(f"/library/series/{series_id}", f"Failed: {type(e).__name__}: {str(e)[:160]}", "warn")
        c = price_of(res.model, res.usage) or 0.0
        store.record("notes", f"{ser['title']}: notes updated from {len(items)} correction(s)", "", (langs_for(key) or ["en"])[0],
                     key, "learned from editor corrections", c, vars(res.usage), res.model)
        return series_view(request, series_id, proposed=text,
                           note=f"Updated notes from {len(items)} correction(s) (${c:.3f}), not saved yet. Review, then save.")

    @app.post("/library/series/{series_id}/settings")
    def series_save(request: Request, series_id: int, title: str = Form(...), enabled: str = Form(""),
                    langs: str = Form(""), tone: str = Form(""), context: str = Form("")):
        _same_origin(request)
        return save_title_settings(f"/library/series/{series_id}", "Series", title, enabled, langs, tone, context)

    def save_title_settings(back: str, what: str, title: str, enabled: str, langs: str, tone: str, context: str):
        """Per-title settings and notes: the same for a series and a film (keyed by the title's slug)."""
        key = slug(title)
        lang_list = [x for x in langs.replace(" ", "").split(",") if x]
        bad = [x for x in lang_list if not x.isalpha() or len(x) not in (2, 3)]
        if bad:
            return _redirect(back, f"Not a language code: {', '.join(bad)}", "warn")
        if tone and tone not in TONE_RULES:
            raise HTTPException(400, "unknown tone")
        store.save_series(key, title, bool(enabled), lang_list, tone)
        s.context_dir.mkdir(parents=True, exist_ok=True)
        f = s.context_dir / f"{key}.md"
        if context.strip():
            f.write_text(context.strip() + "\n", encoding="utf-8")
        elif f.exists():
            f.unlink()
        return _redirect(back, f"{what} settings saved.")

    # ------------------------------------------------------------------ films
    def radarr_movie(movie_id: int) -> dict:
        try:
            m = radarr().get(f"/movie/{movie_id}")
        except ArrError as e:
            if "404" in str(e):
                raise HTTPException(404)
            raise
        f = m.get("movieFile") or {}
        return {"id": m["id"], "title": m["title"], "year": m.get("year"), "overview": m.get("overview") or "",
                "path": f.get("path") or ""}

    @app.get("/library/film/{movie_id}")
    def film_page(request: Request, movie_id: int):
        return film_view(request, movie_id)

    def film_view(request: Request, movie_id: int, proposed: str | None = None, note: str = ""):
        try:
            m = radarr_movie(movie_id)
        except ArrError as e:
            return _redirect("/library", f"Radarr: {e}", "warn")
        key = slug(m["title"])
        langs = langs_for(key)
        status = status_for(m["path"], langs) if m["path"] else {l: False for l in langs}
        per_lang = []
        for l in langs:
            jobs = store.jobs_for(m["path"], l) if m["path"] else []
            marks, job_id = file_marks(jobs)
            per_lang.append({"lang": l, "ok": status.get(l, False), "marks": marks, "job": job_id,
                             "review": any(mk == "needs review" for mk, _ in marks)})
        history = sorted((j for l in langs for j in (store.jobs_for(m["path"], l) if m["path"] else [])),
                         key=lambda j: j.created)
        ctx_file = s.context_dir / f"{key}.md"
        saved = ctx_file.read_text(encoding="utf-8") if ctx_file.is_file() else ""
        return render(request, "film.html", m=m, key=key, langs=langs, per_lang=per_lang, history=history,
                      on_disk=bool(m["path"]) and s.map_path(m["path"]).is_file(),
                      override=store.series(key) or {"enabled": True, "langs": [], "tone": ""},
                      context=proposed if proposed is not None else saved, proposed=proposed is not None,
                      corrections=len(bible.corrections(s.feedback_dir, key)), tones=list(TONE_RULES),
                      avg=episode_estimate(), note=note,
                      fmt_time=lambda t: datetime.fromtimestamp(t).strftime("%d %b %Y %H:%M"))

    @app.post("/library/film/{movie_id}/settings")
    def film_save(request: Request, movie_id: int, title: str = Form(...), enabled: str = Form(""),
                  langs: str = Form(""), tone: str = Form(""), context: str = Form("")):
        _same_origin(request)
        return save_title_settings(f"/library/film/{movie_id}", "Film", title, enabled, langs, tone, context)

    @app.post("/library/film/{movie_id}/notes")
    def film_notes(request: Request, movie_id: int):
        """Draft notes for a film from its overview and its own subtitle track; shown, saved only on save."""
        _same_origin(request)
        if not budget_left(0.5):
            return _redirect(f"/library/film/{movie_id}", "Monthly budget reached; notes not generated.", "warn")
        try:
            m = radarr_movie(movie_id)
        except ArrError as e:
            return _redirect(f"/library/film/{movie_id}", f"Radarr: {e}", "warn")
        local = s.map_path(m["path"]) if m["path"] else None
        src = (media.find_source(local, s.cache_dir, s.source_langs, (langs_for(None) or ["en"])[0], s.any_source)
               if local and s.in_library(local) and local.is_file() else None)
        if not src:
            return _redirect(f"/library/film/{movie_id}", "No text subtitle in the film to learn from.", "warn")
        key = slug(m["title"])
        try:
            text, res = bible.generate_notes(app.state.worker.provider_factory(), m["title"], m["overview"],
                                             (langs_for(key) or ["en"])[0], cuesmod.load(src.path).cues)
        except Exception as e:
            return _redirect(f"/library/film/{movie_id}", f"Generation failed: {type(e).__name__}: {str(e)[:160]}", "warn")
        c = price_of(res.model, res.usage) or 0.0
        store.record("notes", f"{m['title']}: film notes drafted", m["path"], (langs_for(key) or ["en"])[0], key,
                     "generated from the film page", c, vars(res.usage), res.model)
        return film_view(request, movie_id, proposed=text,
                         note=f"Draft notes below (${c:.3f}), not saved yet. Edit, then save film settings.")

    @app.post("/library/translate")
    async def translate_selected(request: Request):
        _same_origin(request)
        form = await request.form()
        paths = form.getlist("path")
        back = local_path(form.get("back"), "/library")
        default_key = form.get("series_key") or None
        title = form.get("title") or ""
        mode = "batch" if form.get("mode") == "batch" else "live"
        if not paths:
            return _redirect(back, "Nothing selected.", "warn")
        ids = []
        for p in paths:
            if not s.in_library(s.map_path(str(p))):
                continue
            label = form.get(f"label:{p}") or Path(str(p)).stem
            key = valid_key(form.get(f"key:{p}")) or default_key     # films chosen together: each its own notes
            for lang in langs_for(key):
                vid = s.map_path(str(p))
                redo = target_path(vid, lang, "srt").exists() or target_path(vid, lang, "ass").exists()
                job = store.enqueue("manual", f"{title} {label}".strip(), str(p), lang, key, mode, redo=redo)
                if job:
                    ids.append(job.id)
        app.state.worker.wake()
        if mode == "batch":
            how = ("sent as one batch in about two minutes; results within 24 h (usually under an hour)"
                   if app.state.worker.batch_capable() else "this provider has no Batch API, so they run live at full price")
            return _redirect("/", f"Added {len(ids)} job(s) to the backlog, about ${len(ids) * episode_estimate() / 2:.2f}: {how}.")
        return _redirect("/", f"Queued {len(ids)} job(s), estimated ${len(ids) * episode_estimate():.2f}.")

    # ------------------------------------------------------------------ editor
    @app.exception_handler(Preparing)
    async def _preparing(request: Request, exc: Preparing):
        return JSONResponse({"error": "The original subtitle track is still being read; try again in a moment."}, 503)

    pending: dict[str, threading.Thread] = {}
    found: dict[str, object] = {}

    def editor_source(video: Path, target: Path, lang: str):
        """The exact source the translation was made from (stored copy, instant); otherwise the
        video's track, extracted in the background (can take a minute over a network share)."""
        st = reuse.stored_source(s.data_dir / "sources", target)
        if st:
            return media.Source(st[0], st[1], "stored")
        k = f"{video}|{lang}"
        if k not in pending:
            def work():
                try:
                    found[k] = media.find_source(video, s.cache_dir, s.source_langs, lang)
                except Exception as e:          # an unreadable track only means no source column
                    found[k] = e
            pending[k] = threading.Thread(target=work, daemon=True, name="editor-source")
            pending[k].start()
        pending[k].join(timeout=2)
        if pending[k].is_alive():
            raise Preparing()
        pending.pop(k, None)
        r = found.pop(k, None)
        return None if isinstance(r, Exception) else r

    def load_editor(path: str, lang: str, key: str | None):
        video = s.map_path(path)
        if not s.in_library(video) or not video.is_file():
            raise HTTPException(404, "video not in the library")
        if not lang.replace("-", "").isalpha():
            raise HTTPException(400, "bad language")
        target = next((t for t in (target_path(video, lang, "srt"), target_path(video, lang, "ass")) if t.is_file()), None)
        if target is None:
            raise HTTPException(404, "no CaptionizeRR translation for this file and language")
        src = editor_source(video, target, lang)
        tdoc = cuesmod.load(target)
        rows = editor.build_rows(cuesmod.load(src.path) if src else None, tdoc)
        ser = store.series(key) if key else None
        profile = s.profile(lang, src.lang if src else "en", (ser or {}).get("tone"))
        return video, target, tdoc, rows, profile, str(target.stat().st_mtime_ns)

    def skipped_rows(path: str, video: Path, target: Path, lang: str, rows) -> set[int]:
        """Rows with a source line but no translation that the pipeline left out on purpose (promos,
        adverts). Recorded per translation since 0.2.9; for older files, an empty line counts as
        intentional when the file's last translation finished with nothing open."""
        drops = reuse.stored_drops(s.data_dir / "sources", target)
        empty = [r for r in rows if r.src.strip() and not r.tr.strip()]
        if drops is not None:
            return {r.id for r in empty if r.start_ms in drops}
        jobs = store.jobs_for(path, lang) or store.jobs_for(str(video), lang)
        last = next((j for j in reversed(jobs) if j.source not in ("edit", "notes", "forced", "sync")
                     and j.state in ("done", "needs_review")), None)
        return {r.id for r in empty} if last is not None and last.state == "done" else set()

    @app.get("/editor")
    def editor_home(request: Request):
        """Where to start editing: translations waiting for review first, then the latest ones."""
        side = ("edit", "notes", "forced", "sync")
        jobs = [j for j in store.list(500) if j.source not in side and j.output_path
                and j.output_path.endswith((".srt", ".ass")) and j.state in ("done", "needs_review")]
        seen, latest = set(), []
        for j in jobs:                                   # newest first: one row per file and language
            k = (j.video_path, j.lang)
            if k not in seen:
                seen.add(k)
                latest.append(j)
        gone = gone_videos([j.video_path for j in latest], s.library_roots)
        latest = [j for j in latest if j.video_path not in gone]
        review = [j for j in latest if j.state == "needs_review"]
        recent = [j for j in latest if j.state != "needs_review"][:60]
        return render(request, "editor.html", review=review, recent=recent,
                      fmt_time=lambda t: datetime.fromtimestamp(t).strftime("%d %b %H:%M"))

    @app.get("/edit")
    def edit_page(request: Request, path: str, lang: str, key: str = "", back: str = "", view: str = ""):
        key = valid_key(key) or ""
        try:
            video, target, tdoc, rows, profile, token = load_editor(path, lang, key or None)
        except Preparing:
            return render(request, "preparing.html", back=local_path(back))
        flags = editor.flags_for(rows, profile)
        skipped = skipped_rows(path, video, target, lang, rows)
        for rid in skipped:                       # left out on purpose: not an error to fix
            flags[rid] = [f for f in flags.get(rid, []) if f != "empty"]
        data = [{"id": r.id, "t": r.start_ms, "d": round(r.duration, 2), "src": r.src, "tr": r.tr,
                 "extra": r.extra, "flags": flags.get(r.id, []), "skip": r.id in skipped} for r in rows if not r.hidden]
        stem = video.stem
        m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", stem)
        series = re.split(r"[\s._-]+[Ss]\d{1,2}[Ee]\d", stem)[0].replace(".", " ").strip(" -_") if m else ""
        ep_title = ""
        if m:
            rest = stem[m.end():].strip(" -")
            ep_title = re.sub(r"\s*(WEBDL|WEBRip|Bluray|HDTV|DVD|Remux|\d{3,4}p).*$", "", rest, flags=re.I).strip(" -")
        heading = (f"{series} · Season {int(m.group(1))}, Episode {int(m.group(2))}" if m else
                   re.sub(r"[\s._-]+(Remux|Bluray|BDRip|WEB-?DL|WEBRip|HDTV|DVD|\d{3,4}p)\b.*$", "", stem, flags=re.I) or stem)
        review = store.review_job(path, lang) or store.review_job(str(video), lang)
        hist = store.jobs_for(path, lang) or store.jobs_for(str(video), lang)
        job = next((j for j in reversed(hist) if j.source not in ("edit", "notes", "forced", "sync")
                    and j.state in ("done", "needs_review")), None)
        return render(request, "edit.html", rows=data, path=path, lang=lang, key=key, token=token, review=review, job=job,
                      heading=heading, ep_title=ep_title,
                      back=local_path(back),
                      fname=target.name, profile=profile, view="flag" if view == "flag" else "all", flagged=sum(1 for d in data if d["flags"]),
                      has_source=any(r.src for r in rows))

    async def edit_body(request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("edits", {}), dict):
            raise HTTPException(400, "bad body")
        return body

    def apply_edits(rows, edits: dict) -> list[dict]:
        by = {r.id: r for r in rows}
        changed = []
        for k, v in edits.items():
            r = by.get(int(k))
            if r is None or r.hidden or not isinstance(v, str):
                raise HTTPException(400, f"unknown row {k}")
            v = v.replace("\r\n", "\n").strip()
            if v != r.tr:
                changed.append({"src": r.src, "before": r.tr, "after": v})
                r.tr = v
        return changed

    @app.post("/api/edit/save")
    async def edit_save(request: Request):
        b = await edit_body(request)
        return await run_in_threadpool(_edit_save, b)

    def _edit_save(b: dict):
        key = valid_key(b.get("key"))
        video, target, tdoc, rows, profile, token = load_editor(str(b.get("path", "")), str(b.get("lang", "")), key)
        if b.get("token") != token:
            return JSONResponse({"error": "The file changed since you opened it. Reload to see the new version."}, 409)
        changed = apply_edits(rows, b.get("edits") or {})
        if not changed:
            return {"saved": 0, "token": token}
        try:
            replace_own(target, editor.save_text(tdoc, rows), s.history_dir)
        except WriteRefused as e:
            raise HTTPException(400, str(e))
        bible.log_corrections(s.feedback_dir, key or "", changed)
        refresh_all(s, [str(b["path"])])
        flags = editor.flags_for(rows, profile)
        reviewed = close_review_if_clean(str(b["path"]), video, target, str(b["lang"]), rows, flags)
        return {"saved": len(changed), "token": str(target.stat().st_mtime_ns), "reviewed": reviewed,
                "flags": {str(k): v for k, v in flags.items()}}

    SOFT_FLAGS = {"speaker label?", "italics lost"}       # don't hold a job in review (validate.SOFT)

    def close_review_if_clean(path: str, video: Path, target: Path, lang: str, rows, flags) -> bool:
        """After a save: when nothing but soft notes is left, the job's review is done."""
        job = store.review_job(path, lang) or store.review_job(str(video), lang)
        if job is None:
            return False
        skipped = skipped_rows(path, video, target, lang, rows)
        hard = [f for rid, fs in flags.items() for f in fs
                if f not in SOFT_FLAGS and not (f == "empty" and rid in skipped)]
        return not hard and store.mark_reviewed(job.id, "reviewed in the editor")

    @app.post("/jobs/{job_id}/reviewed")
    def job_reviewed(request: Request, job_id: int, back: str = Form("")):
        """The owner accepts the file as it is (a flagged line can be fine: a fast line nobody can shorten)."""
        _same_origin(request)
        ok = store.mark_reviewed(job_id, "marked as reviewed")
        return _redirect(local_path(back, f"/jobs/{job_id}"), "Marked as reviewed." if ok else "Nothing to review.",
                         "ok" if ok else "warn")

    @app.post("/api/edit/retranslate")
    async def edit_retranslate(request: Request):
        b = await edit_body(request)
        return await run_in_threadpool(_edit_retranslate, b)

    def _edit_retranslate(b: dict):
        key = valid_key(b.get("key"))
        video, target, tdoc, rows, profile, _ = load_editor(str(b.get("path", "")), str(b.get("lang", "")), key)
        apply_edits(rows, b.get("edits") or {})
        try:
            row_id = int(b.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "bad row id")
        if not any(r.id == row_id for r in rows):
            raise HTTPException(400, "unknown row")
        if not budget_left(0.05):
            return JSONResponse({"error": "Monthly budget reached."}, 402)
        instruction = str(b.get("instruction") or "")[:500]
        try:
            prov = app.state.worker.provider_factory()
            text, usage = editor.retranslate(prov, profile, app.state.worker.context_for(key), rows, row_id, instruction)
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 502)
        c = price_of(prov.model, usage) or 0.0          # the model that did the work, not the Anthropic setting
        store.record("edit", f"{short_title(target.name)}: line {row_id} re-translated", str(b["path"]), str(b["lang"]), key,
                     instruction or "re-translate", c, vars(usage), prov.model)
        r = next(r for r in rows if r.id == row_id)
        r.tr = text
        return {"text": text, "cost": round(c, 4), "flags": editor.flags_for(rows, profile).get(row_id, [])}

    # ------------------------------------------------------------------ share on OpenSubtitles
    def share_target(path: str, lang: str, forced: bool) -> tuple[Path, Path]:
        video = s.map_path(path)
        if not s.in_library(video) or not video.is_file():
            raise HTTPException(404, "video not in the library")
        if not lang.replace("-", "").isalpha():
            raise HTTPException(400, "bad language")
        sub = next((t for t in (target_path(video, lang, "srt", forced), target_path(video, lang, "ass", forced))
                    if t.is_file()), None)
        if sub is None:
            raise HTTPException(404, "no CaptionizeRR translation to share")
        return video, sub

    def os_client():
        from ..opensubtitles import Client
        return Client(s.opensubtitles_user, s.opensubtitles_password,
                      s.opensubtitles_user_agent or f"Captionizerr v{__version__}")

    def safe_back(back: str) -> str:
        return local_path(back)

    @app.get("/share")
    def share_page(request: Request, path: str, lang: str, forced: str = "", back: str = ""):
        video, sub = share_target(path, lang, bool(forced))
        configured = bool(s.opensubtitles_user and s.opensubtitles_password)
        from ..opensubtitles import Guess
        guess, err = Guess(), ""
        if configured:
            try:
                c = os_client()
                c.login()
                guess = c.guess(video.name)
            except Exception as e:
                err = str(e)[:200]
        shared = store.share(str(sub))
        # credit the model that made this file, not whichever one is selected now
        used = store.model_for(path, lang, bool(forced)) or store.model_for(str(video), lang, bool(forced)) or "unknown model"
        return render(request, "share.html", path=path, lang=lang, forced=bool(forced), back=safe_back(back),
                      fname=sub.name, configured=configured, guess=guess, guess_error=err, release=video.stem,
                      translator=f"CaptionizeRR (AI: {used})", hd=bool(re.search(r"(720|1080|2160)p", video.name)),
                      comment=share_comment(used, store.jobs_for(path, lang) or store.jobs_for(str(video), lang)),
                      episode_file=bool(re.search(r"[Ss]\d{1,3}[Ee]\d{1,4}", video.name)),
                      shared=shared, shared_when=datetime.fromtimestamp(shared["at"]).strftime("%d %b %Y") if shared else "")

    @app.get("/share/download")
    def share_download(path: str, lang: str, forced: str = ""):
        """The translation as a file to keep or share by hand, named the way players and sites expect
        (<video>.<lang>[.forced].srt), without CaptionizeRR's own marker."""
        video, sub = share_target(path, lang, bool(forced))
        # exactly the name on disk; RFC 6266/5987 with every special character escaped (an apostrophe in
        # "Let's" otherwise ends the encoded value early in some browsers), plus an ASCII fallback
        name = sub.name
        ascii_name = re.sub(r'[^A-Za-z0-9._ +()\[\]-]', "_", name)
        cd = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name, safe="")}'
        return FileResponse(sub, media_type="application/x-subrip" if sub.suffix == ".srt" else "text/plain",
                            headers={"Content-Disposition": cd})

    @app.post("/share")
    async def share_upload(request: Request):
        _same_origin(request)
        f = await request.form()
        path, lang, forced = str(f.get("path", "")), str(f.get("lang", "")), bool(f.get("forced"))
        back = f"/share?{urllib.parse.urlencode({'path': path, 'lang': lang, 'forced': '1' if forced else '', 'back': safe_back(str(f.get('back', '')))})}"
        if not (f.get("reviewed") and f.get("rights")):
            return _redirect(back, "Tick both confirmations first.", "warn")
        video, sub = share_target(path, lang, forced)
        imdb = str(f.get("imdb_id", "")).strip()
        if not re.fullmatch(r"(tt)?\d{5,10}", imdb):
            return _redirect(back, "The IMDb ID looks like tt1234567.", "warn")
        episode_file = bool(re.search(r"[Ss]\d{1,3}[Ee]\d{1,4}", video.name))

        def upload():
            c = os_client()
            c.login()
            if episode_file:     # the show's ID files it under the whole series: OpenSubtitles refuses that
                g = c.guess(video.name)
                if g.series_imdb_id and re.sub(r"\D", "", imdb) == g.series_imdb_id:
                    raise ValueError(f"tt{imdb.lstrip('t')} is the whole series; use this episode's own IMDb ID"
                                     + (f" (OpenSubtitles suggests tt{g.imdb_id})" if g.imdb_id else ""))
            return c.upload(sub, video, lang, imdb, str(f.get("release", ""))[:200], str(f.get("comment", ""))[:500],
                         str(f.get("translator", ""))[:100], forced=forced,
                         hd=bool(re.search(r"(720|1080|2160)p", video.name)))
        try:
            r = await run_in_threadpool(upload)
        except Exception as e:
            log.warning("OpenSubtitles upload of %s failed: %s", sub.name, e)
            return _redirect(back, f"Not uploaded: {str(e)[:300]}", "warn")
        store.record_share(str(sub), r["url"], r["already"])
        return _redirect(back, "OpenSubtitles already has this exact file." if r["already"] else "Uploaded to OpenSubtitles. Thank you!")

    # ------------------------------------------------------------------ settings + setup wizard
    from .settings_ui import register_settings
    register_settings(app, render, _redirect, _same_origin)
