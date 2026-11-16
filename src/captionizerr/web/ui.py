"""Web UI: dashboard, library, per-series settings, global settings, providers.

Server-rendered (Jinja2), no JavaScript build. Every state-changing request is a
POST that must come from this UI's own pages (Origin/Referer check), because the
UI has no login on a trusted LAN and some actions spend money.
"""
from __future__ import annotations

import re
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
from .. import bible, cues as cuesmod, editor, media
from ..jellyfin import media_updated
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
        jobs = store.list(200)
        if state:
            jobs = [j for j in jobs if j.state == state]
        spent, cap = store.spent(), s.monthly_cap_usd
        return render(request, "dashboard.html", jobs=jobs[:100], counts=store.counts(), spent=spent, cap=cap,
                      pct=min(100, 100 * spent / cap) if cap else 0, month=month_now(), months=store.spend_by_month(),
                      filter_state=state, states=STATES, avg=store.avg_cost(),
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
        return render(request, "library.html", series=series, movies=movies, errors=err, avg=store.avg_cost())

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
        return store.spent() + need <= s.monthly_cap_usd

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
    def load_editor(path: str, lang: str, key: str | None):
        video = s.map_path(path)
        if not s.in_library(video) or not video.is_file():
            raise HTTPException(404, "video not in the library")
        if not lang.replace("-", "").isalpha():
            raise HTTPException(400, "bad language")
        target = next((t for t in (target_path(video, lang, "srt"), target_path(video, lang, "ass")) if t.is_file()), None)
        if target is None:
            raise HTTPException(404, "no Captionizerr translation for this file and language")
        src = media.find_source(video, s.cache_dir, s.source_langs, lang)
        tdoc = cuesmod.load(target)
        rows = editor.build_rows(cuesmod.load(src.path) if src else None, tdoc)
        ser = store.series(key) if key else None
        profile = s.profile(lang, src.lang if src else "en", (ser or {}).get("tone"))
        return video, target, tdoc, rows, profile, str(target.stat().st_mtime_ns)

    @app.get("/edit")
    def edit_page(request: Request, path: str, lang: str, key: str = "", back: str = ""):
        video, target, tdoc, rows, profile, token = load_editor(path, lang, key or None)
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
        media_updated(s.jellyfin_url, s.jellyfin_key, [str(b["path"])], s.jellyfin_path_map)
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

    # ------------------------------------------------------------------ settings
    @app.get("/settings")
    def settings_page(request: Request):
        from .. import maintenance
        last = store.load_settings().get("_maintenance_last") or {}
        return render(request, "settings.html", models=list(PRICES), efforts=EFFORTS, tones=list(TONE_RULES),
                      songs=list(SONG_RULES), v=s.editable_values(),
                      path_map="; ".join(f"{a}={b}" for a, b in s.jellyfin_path_map),
                      storage=maintenance.storage(s), last=last, v_keep={"history": s.keep_history, "days": s.keep_days},
                      last_when=datetime.fromtimestamp(last["at"]).strftime("%d %b %H:%M") if last.get("at") else "")

    @app.post("/maintenance/run")
    def maintenance_run(request: Request):
        _same_origin(request)
        from .. import maintenance
        rep = maintenance.run(s, store)
        return _redirect("/settings", "Cleanup done: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in rep.items() if k != "at") + ".")

    @app.post("/settings")
    async def settings_save(request: Request):
        _same_origin(request)
        f = await request.form()
        try:
            langs = [x for x in str(f.get("languages", "")).replace(" ", "").split(",") if x]
            if not langs or any(not x.isalpha() or len(x) not in (2, 3) for x in langs):
                raise ValueError("languages must be ISO codes such as ru, uk, de")
            model = str(f.get("model"))
            if model not in PRICES:
                raise ValueError("unknown model")
            effort = str(f.get("effort"))
            if effort not in EFFORTS:
                raise ValueError("unknown effort")
            tone, songs = str(f.get("tone")), str(f.get("songs"))
            if tone not in TONE_RULES or songs not in SONG_RULES:
                raise ValueError("unknown tone or songs option")
            pm = []
            for pair in filter(None, (x.strip() for x in str(f.get("jellyfin_path_map", "")).split(";"))):
                a, _, b = pair.partition("=")
                if not a or not b:
                    raise ValueError("path map pairs look like /downloads/=/mnt/media/")
                pm.append([a, b])
            src_vals = {}
            if "source_langs" in f:                    # the Sources card is part of this form
                srcl = [x for x in str(f.get("source_langs", "")).replace(" ", "").split(",") if x]
                if not srcl or any(not x.isalpha() or len(x) not in (2, 3) for x in srcl):
                    raise ValueError("source languages must be ISO codes such as en, ja")
                src_vals = {"source_langs": srcl, "any_source": bool(f.get("any_source")),
                            "auto_notes": bool(f.get("auto_notes")), "bazarr_url": str(f.get("bazarr_url", "")).strip()}
            vals = {
                **src_vals,
                "languages": langs, "model": model, "effort": effort, "tone": tone, "songs": songs,
                "monthly_cap_usd": max(0.0, float(f.get("monthly_cap_usd") or 0)),
                "alert_at": min(1.0, max(0.1, float(f.get("alert_pct") or 80) / 100)),
                "max_line": max(20, min(60, int(f.get("max_line") or 42))),
                "target_cps": max(10.0, min(30.0, float(f.get("target_cps") or 20))),
                "ntfy_url": str(f.get("ntfy_url", "")).strip(), "ntfy_topic": str(f.get("ntfy_topic", "")).strip(),
                "public_url": str(f.get("public_url", "")).strip(), "jellyfin_url": str(f.get("jellyfin_url", "")).strip(),
                "sonarr_url": str(f.get("sonarr_url", "")).strip(), "radarr_url": str(f.get("radarr_url", "")).strip(),
                "jellyfin_path_map": pm,
            }
        except ValueError as e:
            return _redirect("/settings", f"Not saved: {e}.", "warn")
        store.save_settings(vals)
        s.apply(vals)
        return _redirect("/settings", "Settings saved. New jobs use them right away.")

    # ------------------------------------------------------------------ providers
    @app.get("/providers")
    def providers(request: Request):
        models = [{"id": m, "in": p[0], "out": p[1], "ep": _ep_cost(m), "default": m == s.model and s.provider == "anthropic"}
                  for m, p in PRICES.items()]
        return render(request, "providers.html", models=models, secrets=s.secrets_status, v=s.editable_values())

    @app.post("/providers/use")
    async def provider_use(request: Request):
        _same_origin(request)
        f = await request.form()
        p = str(f.get("provider"))
        if p not in ("anthropic", "openai", "ollama"):
            raise HTTPException(400)
        if p == "openai" and not (s.openai_base_url and s.openai_model):
            return _redirect("/providers", "Set a base URL and model for the OpenAI-compatible provider first.", "warn")
        if p == "ollama" and not s.ollama_model:
            return _redirect("/providers", "Pick an Ollama model first.", "warn")
        store.save_settings({"provider": p})
        s.apply({"provider": p})
        return _redirect("/providers", f"Now translating with {'Anthropic' if p == 'anthropic' else s.active_model}.")

    @app.post("/providers/ollama/models")
    async def ollama_models(request: Request):
        _same_origin(request)
        from ..providers.ollama import list_models
        b = await request.json()
        url = str((b or {}).get("url") or s.ollama_url).strip()
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"error": "the address must start with http://"}, 400)
        try:
            return {"models": list_models(url)}
        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, 502)

    @app.post("/providers/ollama")
    async def ollama_save(request: Request):
        _same_origin(request)
        f = await request.form()
        url = str(f.get("ollama_url", "")).strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            return _redirect("/providers", "Not saved: the Ollama address must start with http://.", "warn")
        try:
            ctx = int(f.get("ollama_num_ctx") or 16384)
        except ValueError:
            ctx = 16384
        vals = {"ollama_url": url, "ollama_model": str(f.get("ollama_model", "")).strip(),
                "ollama_num_ctx": max(4096, min(131072, ctx))}
        p = str(f.get("provider") or s.provider)
        if p not in ("anthropic", "openai", "ollama"):
            raise HTTPException(400)
        if p == "ollama" and not vals["ollama_model"]:
            return _redirect("/providers", "Not saved: click Find models and pick one first.", "warn")
        vals["provider"] = p
        store.save_settings(vals)
        s.apply(vals)
        return _redirect("/providers", "Saved." + (f" Translating locally with {vals['ollama_model']} from now on."
                                                  if p == "ollama" else ""))

    @app.post("/providers/save")
    async def provider_save(request: Request):
        _same_origin(request)
        f = await request.form()
        try:
            url = str(f.get("openai_base_url", "")).strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("base URL must start with http:// or https://")
            vals = {"openai_base_url": url, "openai_model": str(f.get("openai_model", "")).strip(),
                    "openai_price_in": max(0.0, float(f.get("openai_price_in") or 0)),
                    "openai_price_out": max(0.0, float(f.get("openai_price_out") or 0)),
                    "openai_max_output": max(1000, min(200000, int(f.get("openai_max_output") or 16000)))}
            p = str(f.get("provider") or s.provider)
            if p not in ("anthropic", "openai", "ollama"):
                raise ValueError("unknown provider")
            if p == "openai" and not (vals["openai_base_url"] and vals["openai_model"]):
                raise ValueError("the OpenAI-compatible provider needs a base URL and a model")
            vals["provider"] = p
        except ValueError as e:
            return _redirect("/providers", f"Not saved: {e}.", "warn")
        store.save_settings(vals)
        s.apply(vals)
        return _redirect("/providers", "Saved." + (f" Translating with {vals['openai_model']} from now on." if p == "openai" else ""))

    @app.post("/providers/test")
    async def provider_test(request: Request):
        _same_origin(request)
        f = await request.form()
        which = str(f.get("which") or "anthropic")
        t0 = time.monotonic()
        try:
            if which == "ollama":
                from ..providers.ollama import OllamaProvider
                prov = OllamaProvider(s.ollama_url, s.ollama_model, 4096, 50, timeout=300)
                r = prov.complete("", "", "Reply with the single word OK.")
                return _redirect("/providers", f"{s.ollama_model} answered “{r.text.strip()[:40]}” in "
                                               f"{time.monotonic() - t0:.1f}s (includes loading the model).")
            if which == "openai":
                from ..providers.openai_compat import OpenAICompatProvider
                prov = OpenAICompatProvider(s.openai_base_url, s.openai_model, s.openai_key, 200, timeout=60)
                r = prov.complete("", "", "Reply with the single word OK.")
                return _redirect("/providers", f"{s.openai_model} answered “{r.text.strip()[:40]}” in "
                                               f"{time.monotonic() - t0:.1f}s ({r.usage.input_tokens}+{r.usage.output_tokens} tokens).")
            if s.provider == "anthropic":
                prov = app.state.worker.provider_factory()
            else:
                from ..providers.anthropic_provider import AnthropicProvider
                prov = AnthropicProvider(model=s.model, effort=s.effort)
            n = prov.count_tokens("", "", "Connection test from Captionizerr.")
        except Exception as e:
            return _redirect("/providers", f"{which} test failed: {type(e).__name__}: {str(e)[:160]}", "warn")
        return _redirect("/providers", f"Anthropic works ({s.model}): counted {n} tokens in {time.monotonic() - t0:.1f}s, at no cost.")
