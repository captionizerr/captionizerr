"""HTTP service: Sonarr/Radarr webhooks, a small JSON API, a status page and Prometheus metrics."""
from __future__ import annotations

import base64
import hmac
import html
import logging
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import __version__
from .config import Settings
from .hooks import parse_radarr, parse_sonarr, slug
from .jellyfin import refresh_all
from .store import STATES, Store, month_now
from .worker import Worker
from .writer import move_sidecars

log = logging.getLogger("captionizerr.app")


class Throttle:
    """Lock a client out after `limit` failed logins/keys within `window` seconds, for `lock` seconds.
    In memory (resets on restart). Behind a reverse proxy all clients share its address, so a
    flood of bad logins locks everyone out for a while: the safe direction."""

    def __init__(self, limit: int = 10, window: float = 600, lock: float = 900):
        self.limit, self.window, self.lock = limit, window, lock
        self.fails: dict[str, list[float]] = {}
        self.until: dict[str, float] = {}

    def locked(self, who: str) -> bool:
        return self.until.get(who, 0) > time.time()

    def fail(self, who: str) -> None:
        now = time.time()
        recent = [t for t in self.fails.get(who, []) if now - t < self.window] + [now]
        self.fails[who] = recent
        if len(recent) >= self.limit:
            self.until[who] = now + self.lock
            self.fails.pop(who, None)
            log.warning("auth: %s locked out for %d min after %d failures", who, self.lock // 60, len(recent))

    def ok(self, who: str) -> None:
        self.fails.pop(who, None)


def create_app(settings: Settings | None = None, provider_factory=None, start_worker: bool = True) -> FastAPI:
    s = settings or Settings.from_env()
    (s.data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    s.context_dir.mkdir(parents=True, exist_ok=True)
    store = Store(s.data_dir / "captionizerr.db")
    import copy
    env_defaults = copy.deepcopy(s)          # what the environment says, before UI overrides
    s.apply(store.load_settings())          # UI-saved settings override environment defaults
    from . import secret_store
    secrets = secret_store.SecretStore(s.data_dir / "secrets.json")
    secret_store.apply(s, secrets)          # UI-entered secrets, where the server sets none
    if provider_factory is None:
        from .providers import make_provider
        provider_factory = lambda: make_provider(s)  # noqa: E731
    worker = Worker(s, store, provider_factory)
    app = FastAPI(title="CaptionizeRR", version=__version__)
    app.state.store, app.state.worker, app.state.settings = store, worker, s
    app.state.env_defaults = env_defaults
    app.state.secrets = secrets

    if start_worker:
        @app.on_event("startup")
        def _start():
            worker.start()

    # Optional login. Webhooks and API clients use the API key instead; health and metrics stay open
    # for monitoring; static files carry nothing private.
    OPEN = ("/health", "/metrics", "/static/", "/api/hooks/")
    throttle = Throttle()

    def basic_auth(request: Request) -> tuple[str, str] | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return None
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            return ("", "")
        return user, pw

    def client_id(request: Request) -> str:
        return request.client.host if request.client else "?"

    @app.middleware("http")
    async def ui_login(request: Request, call_next):
        if not secret_store.login_enabled(s) or request.url.path.startswith(OPEN):
            return await call_next(request)
        who = client_id(request)
        if throttle.locked(who):
            return PlainTextResponse("too many failed logins; try again later", 429, headers={"Retry-After": "900"})
        given_key = request.query_params.get("apikey") or request.headers.get("X-Api-Key", "")
        if request.url.path.startswith("/api/") and s.webhook_key and given_key:
            if hmac.compare_digest(given_key, s.webhook_key):
                return await call_next(request)
            throttle.fail(who)
        creds = basic_auth(request)
        if creds is not None:
            if secret_store.check_login(s, creds[0], creds[1]):
                throttle.ok(who)
                return await call_next(request)
            throttle.fail(who)
        return PlainTextResponse("login required", 401, headers={"WWW-Authenticate": 'Basic realm="CaptionizeRR"'})

    def require_key(request: Request):
        """Webhooks and the JSON API: the key in the X-Api-Key header, as the password of HTTP Basic
        auth (Sonarr/Radarr webhook Username/Password fields; any username), or ?apikey= (legacy:
        URLs end up in logs)."""
        who = client_id(request)
        if throttle.locked(who):
            raise HTTPException(status_code=429, detail="too many failed attempts; try again later")
        creds = basic_auth(request)
        given = request.headers.get("X-Api-Key", "") or (creds[1] if creds else "") or request.query_params.get("apikey") or ""
        if not s.webhook_key or not hmac.compare_digest(given, s.webhook_key):
            throttle.fail(who)
            raise HTTPException(status_code=401, detail="invalid api key")
        throttle.ok(who)

    def enqueue_items(source: str, items) -> list[int]:
        ids = []
        for it in items:
            ser = store.series(it.series_key) if it.series_key else None
            if ser and not ser["enabled"]:
                log.info("%s: translation disabled for this series, skipped", it.title)
                continue
            for lang in (ser and ser["langs"]) or s.languages:
                job = store.enqueue(source, it.title, it.video_path, lang, it.series_key)
                if job:
                    ids.append(job.id)
        if ids:
            worker.wake()
        return ids

    def handle_renames(renames) -> int:
        moved, touched = 0, []
        for r in renames:
            try:
                n = len(move_sidecars(s.map_path(r.old_path), s.map_path(r.new_path)))
            except OSError as e:
                log.warning("rename of sidecars failed for %s: %s", r.new_path, e)
                continue
            moved += n
            if n:
                touched.append(r.new_path)
        refresh_all(s, touched)
        return moved

    @app.post("/api/hooks/{arr}")
    async def hook(arr: str, request: Request):
        require_key(request)
        payload = await request.json()
        if arr == "sonarr":
            ev, items, renames = parse_sonarr(payload)
        elif arr == "radarr":
            ev, items, renames = parse_radarr(payload)
        else:
            raise HTTPException(404, "unknown source")
        ids = enqueue_items(arr, items)
        moved = handle_renames(renames)
        log.info("%s %s: queued %s, moved %d sidecars", arr, ev, ids, moved)
        return {"event": ev, "queued": ids, "moved_sidecars": moved}

    @app.post("/api/jobs")
    async def add_job(request: Request):
        """Manual enqueue: {"path": "/downloads/...mkv", "title": "...", "series": "northwind", "langs": ["ru"]}"""
        require_key(request)
        b = await request.json()
        path = b.get("path")
        if not path:
            raise HTTPException(400, "path required")
        langs = b.get("langs") or s.languages
        ids = []
        for lang in langs:
            job = store.enqueue("manual", b.get("title") or Path(path).stem, path, lang,
                                b.get("series") or slug(Path(path).parent.parent.name))
            if job:
                ids.append(job.id)
        worker.wake()
        return {"queued": ids}

    @app.post("/api/jobs/resume")
    async def resume(request: Request):
        require_key(request)
        n = store.resume_paused()
        worker.wake()
        return {"resumed": n}

    @app.get("/api/jobs")
    def jobs(limit: int = 50):
        return [vars(j) for j in store.list(limit)]

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "spent_month": round(store.spent(), 4),
                "cap": s.monthly_cap_usd, "states": store.counts()}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        lines = ["# HELP captionizerr_jobs Jobs by state (all states always present).", "# TYPE captionizerr_jobs gauge"]
        counts = store.counts()
        for state in STATES:
            lines.append(f'captionizerr_jobs{{state="{state}"}} {counts.get(state, 0)}')
        lines += ["# TYPE captionizerr_spend_month_usd gauge", f"captionizerr_spend_month_usd {store.spent():.4f}",
                  "# TYPE captionizerr_budget_usd gauge", f"captionizerr_budget_usd {s.monthly_cap_usd:.2f}"]
        lines += ["# HELP captionizerr_storage_bytes Size of Captionizerr's own data by kind.",
                  "# TYPE captionizerr_storage_bytes gauge"]
        from . import maintenance
        for k, v in maintenance.storage(s).items():
            lines.append(f'captionizerr_storage_bytes{{kind="{k}"}} {v}')
        last = (store.load_settings().get("_maintenance_last") or {}).get("at", 0)
        lines += ["# TYPE captionizerr_maintenance_last_run_timestamp gauge",
                  f"captionizerr_maintenance_last_run_timestamp {last}"]
        return "\n".join(lines) + "\n"

    @app.get("/legacy-status", response_class=HTMLResponse)
    def index():
        spent, cap = store.spent(), s.monthly_cap_usd
        rows = []
        for j in store.list(100):
            when = datetime.fromtimestamp(j.updated).strftime("%Y-%m-%d %H:%M")
            rows.append(f"<tr class='st-{html.escape(j.state)}'><td>{j.id}</td><td>{when}</td><td>{html.escape(j.title)}</td>"
                        f"<td>{html.escape(j.lang)}</td><td><span class='pill'>{html.escape(j.state)}</span></td>"
                        f"<td class='num'>${j.cost_usd:.2f}</td><td class='d'>{html.escape(j.detail)}</td></tr>")
        pct = min(100, 100 * spent / cap) if cap else 0
        return PAGE.format(version=__version__, month=month_now(), spent=f"{spent:.2f}", cap=f"{cap:.2f}", pct=f"{pct:.0f}",
                           langs=html.escape(", ".join(s.languages)), model=html.escape(f"{s.model} · {s.effort}"),
                           rows="\n".join(rows) or "<tr><td colspan='7'>No jobs yet. They appear when Sonarr or Radarr imports something.</td></tr>",
                           now=time.strftime("%H:%M:%S"))

    from .web.ui import register
    register(app)
    return app


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>CaptionizeRR</title>
<style>
:root{{--bg:#f4f5f7;--fg:#14171c;--muted:#5b6370;--line:#dde1e7;--card:#fff;--accent:#a86f00;--bar:#f5c94a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--fg:#e7eaf0;--muted:#9aa3b2;--line:#2a2f38;--card:#171a20;--accent:#f2c14e}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px max(16px,env(safe-area-inset-left))}}
main{{max-width:1000px;margin:auto}} h1{{margin:0 0 4px;font-size:24px}} .muted{{color:var(--muted)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}}
.bar{{height:8px;background:var(--line);border-radius:99px;overflow:hidden}} .bar span{{display:block;height:100%;background:var(--bar)}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:640px}} th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;color:var(--muted);text-transform:uppercase}} .num{{text-align:right;font-variant-numeric:tabular-nums}} .d{{color:var(--muted);font-size:13px}}
.pill{{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}}
.st-done .pill{{color:#1f7a4d}} .st-failed .pill,.st-needs_review .pill{{color:#b4232a}} .st-paused_budget .pill{{color:var(--accent)}}
</style></head><body><main>
<h1>CaptionizeRR</h1><div class="muted">v{version} · target languages: {langs} · {model} · refreshed {now}</div>
<div class="card"><b>Budget {month}:</b> ${spent} of ${cap}<div class="bar" style="margin-top:8px"><span style="width:{pct}%"></span></div></div>
<div class="card tw"><table><thead><tr><th>#</th><th>Updated</th><th>Title</th><th>Lang</th><th>State</th><th class="num">Cost</th><th>Detail</th></tr></thead>
<tbody>{rows}</tbody></table></div></main></body></html>"""
