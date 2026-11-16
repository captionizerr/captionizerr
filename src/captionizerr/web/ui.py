"""Web UI: dashboard, library, per-series settings, global settings, providers.

Server-rendered (Jinja2), no JavaScript build. Every state-changing request is a
POST that must come from this UI's own pages (Origin/Referer check), because the
UI has no login on a trusted LAN and some actions spend money.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..arr import Arr, ArrError, radarr_movies, sonarr_episodes, sonarr_series
from ..hooks import slug
from ..pricing import PRICES
from ..profile import LANG_NAMES, SONG_RULES, TONE_RULES
from ..store import month_now
from .. import bible, cues as cuesmod, editor, media, reuse
from ..jellyfin import refresh_all
from ..pricing import cost as price_of
from ..profile import Profile
from ..writer import WriteRefused, replace_own, target_path

HERE = Path(__file__).parent
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
from ..store import STATES
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
    def dashboard(request: Request, state: str = ""):
        if getattr(app.state, "setup_needed", lambda: False)() and not request.query_params.get("msg"):
            return RedirectResponse("/setup", 307)
        all_jobs = store.list(300)
        side = ("edit", "notes", "forced")
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
        return render(request, "dashboard.html", jobs=jobs[:100], counts=counts, spent=spent, cap=cap,
                      reserved=reserved, pct=min(100, 100 * (spent + reserved) / cap) if cap else 0, month=month_now(),
                      months=store.spend_by_month(), view=view, avg=store.avg_cost(),
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
        return render(request, "job.html", job=job, usage=json.loads(job.usage or "{}"),
                      fmt_time=lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S"))

    @app.post("/jobs/{job_id}/retry")
    def job_retry(request: Request, job_id: int):
        _same_origin(request)
        ok = store.requeue(job_id)
        app.state.worker.wake()
        return _redirect("/", f"Job {job_id} queued again." if ok else f"Job {job_id} can't be retried.", "ok" if ok else "warn")

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
                m["status"] = status_for(m["path"], langs_for(slug(m["title"])))
        except ArrError as e:
            err.append(f"Radarr: {e}")
        for x in series:
            x["key"] = slug(x["title"])
            x["override"] = store.series(x["key"])
            x["translated"] = translated_count(x.get("path"), langs_for(x["key"]))
        configured = {"sonarr": bool(s.sonarr_url and s.sonarr_key), "radarr": bool(s.radarr_url and s.radarr_key)}
        return render(request, "library.html", series=series, movies=movies, errors=err, avg=store.avg_cost(),
                      configured=configured)

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
        for e in eps:
            e["status"] = status_for(e["path"], langs)
            e["missing"] = not all(e["status"].values())
            seasons.setdefault(e["season"], []).append(e)
        ctx_file = s.context_dir / f"{key}.md"
        saved = ctx_file.read_text(encoding="utf-8") if ctx_file.is_file() else ""
        resp = render(request, "series.html", ser=ser, key=key, seasons=seasons, langs=langs,
                      override=store.series(key) or {"enabled": True, "langs": [], "tone": ""},
                      context=proposed if proposed is not None else saved, proposed=proposed is not None,
                      corrections=len(bible.corrections(s.feedback_dir, key)), tones=list(TONE_RULES),
                      avg=store.avg_cost(), note=note)
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
                     "generated from the series page", c, vars(res.usage))
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
                     key, "learned from editor corrections", c, vars(res.usage))
        return series_view(request, series_id, proposed=text,
                           note=f"Updated notes from {len(items)} correction(s) (${c:.3f}), not saved yet. Review, then save.")

    @app.post("/library/series/{series_id}/settings")
    def series_save(request: Request, series_id: int, title: str = Form(...), enabled: str = Form(""),
                    langs: str = Form(""), tone: str = Form(""), context: str = Form("")):
        _same_origin(request)
        key = slug(title)
        lang_list = [x for x in langs.replace(" ", "").split(",") if x]
        bad = [x for x in lang_list if not x.isalpha() or len(x) not in (2, 3)]
        if bad:
            return _redirect(f"/library/series/{series_id}", f"Not a language code: {', '.join(bad)}", "warn")
        if tone and tone not in TONE_RULES:
            raise HTTPException(400, "unknown tone")
        store.save_series(key, title, bool(enabled), lang_list, tone)
        s.context_dir.mkdir(parents=True, exist_ok=True)
        f = s.context_dir / f"{key}.md"
        if context.strip():
            f.write_text(context.strip() + "\n", encoding="utf-8")
        elif f.exists():
            f.unlink()
        return _redirect(f"/library/series/{series_id}", "Series settings saved.")

    @app.post("/library/translate")
    async def translate_selected(request: Request):
        _same_origin(request)
        form = await request.form()
        paths = form.getlist("path")
        back = form.get("back") or "/library"
        key = form.get("series_key") or None
        title = form.get("title") or ""
        mode = "batch" if form.get("mode") == "batch" else "live"
        if not paths:
            return _redirect(back, "Nothing selected.", "warn")
        ids = []
        for p in paths:
            if not s.in_library(s.map_path(str(p))):
                continue
            label = form.get(f"label:{p}") or Path(str(p)).stem
            for lang in langs_for(key):
                job = store.enqueue("manual", f"{title} {label}".strip(), str(p), lang, key, mode)
                if job:
                    ids.append(job.id)
        app.state.worker.wake()
        if mode == "batch":
            how = ("sent as one batch in about two minutes; results within 24 h (usually under an hour)"
                   if app.state.worker.batch_capable() else "this provider has no Batch API, so they run live at full price")
            return _redirect("/", f"Added {len(ids)} job(s) to the backlog, about ${len(ids) * store.avg_cost() / 2:.2f}: {how}.")
        return _redirect("/", f"Queued {len(ids)} job(s), estimated ${len(ids) * store.avg_cost():.2f}.")

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
            raise HTTPException(404, "no Captionizerr translation for this file and language")
        src = editor_source(video, target, lang)
        tdoc = cuesmod.load(target)
        rows = editor.build_rows(cuesmod.load(src.path) if src else None, tdoc)
        ser = store.series(key) if key else None
        profile = s.profile(lang, src.lang if src else "en", (ser or {}).get("tone"))
        return video, target, tdoc, rows, profile, str(target.stat().st_mtime_ns)

    @app.get("/edit")
    def edit_page(request: Request, path: str, lang: str, key: str = "", back: str = ""):
        try:
            video, target, tdoc, rows, profile, token = load_editor(path, lang, key or None)
        except Preparing:
            return render(request, "preparing.html", back=back if back.startswith("/") and not back.startswith("//") else "/")
        data = [{"id": r.id, "t": r.start_ms, "d": round(r.duration, 2), "src": r.src, "tr": r.tr,
                 "extra": r.extra, "flags": r.flags(profile)} for r in rows if not r.hidden]
        stem = video.stem
        m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", stem)
        series = re.split(r"[\s._-]+[Ss]\d{1,2}[Ee]\d", stem)[0].replace(".", " ").strip(" -_") if m else ""
        ep_title = ""
        if m:
            rest = stem[m.end():].strip(" -")
            ep_title = re.sub(r"\s*(WEBDL|WEBRip|Bluray|HDTV|DVD|Remux|\d{3,4}p).*$", "", rest, flags=re.I).strip(" -")
        heading = (f"{series} · Season {int(m.group(1))}, Episode {int(m.group(2))}" if m else stem)
        return render(request, "edit.html", rows=data, path=path, lang=lang, key=key, token=token,
                      heading=heading, ep_title=ep_title,
                      back=back if back.startswith("/") and not back.startswith("//") else "/",
                      fname=target.name, profile=profile, flagged=sum(1 for d in data if d["flags"]),
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
        key = b.get("key") or None
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
        return {"saved": len(changed), "token": str(target.stat().st_mtime_ns)}

    @app.post("/api/edit/retranslate")
    async def edit_retranslate(request: Request):
        b = await edit_body(request)
        key = b.get("key") or None
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
            text, usage = editor.retranslate(app.state.worker.provider_factory(), profile,
                                             app.state.worker.context_for(key), rows, row_id, instruction)
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 502)
        c = price_of(s.model, usage) or 0.0
        store.record("edit", f"{target.name}: line {row_id} re-translated", str(b["path"]), str(b["lang"]), key,
                     instruction or "re-translate", c, vars(usage))
        r = next(r for r in rows if r.id == row_id)
        r.tr = text
        return {"text": text, "cost": round(c, 4), "flags": r.flags(profile)}

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
            raise HTTPException(404, "no Captionizerr translation to share")
        return video, sub

    def os_client():
        from ..opensubtitles import Client
        return Client(s.opensubtitles_user, s.opensubtitles_password,
                      s.opensubtitles_user_agent or f"Captionizerr v{__version__}")

    def safe_back(back: str) -> str:
        return back if back.startswith("/") and not back.startswith("//") else "/"

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
        return render(request, "share.html", path=path, lang=lang, forced=bool(forced), back=safe_back(back),
                      fname=sub.name, configured=configured, guess=guess, guess_error=err, release=video.stem,
                      translator=f"Captionizerr (AI: {s.active_model})", hd=bool(re.search(r"(720|1080|2160)p", video.name)),
                      comment=f"AI translation ({s.active_model}) of this release's own subtitles, timed to the file; "
                              "reviewed by the uploader.",
                      shared=shared, shared_when=datetime.fromtimestamp(shared["at"]).strftime("%d %b %Y") if shared else "")

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
        try:
            c = os_client()
            c.login()
            r = c.upload(sub, video, lang, imdb, str(f.get("release", ""))[:200], str(f.get("comment", ""))[:500],
                         str(f.get("translator", ""))[:100], forced=forced,
                         hd=bool(re.search(r"(720|1080|2160)p", video.name)))
        except Exception as e:
            return _redirect(back, f"Not uploaded: {str(e)[:200]}", "warn")
        store.record_share(str(sub), r["url"], r["already"])
        return _redirect(back, "OpenSubtitles already has this exact file." if r["already"] else "Uploaded to OpenSubtitles. Thank you!")

    # ------------------------------------------------------------------ settings + setup wizard
    from .settings_ui import register_settings
    register_settings(app, render, _redirect, _same_origin)
