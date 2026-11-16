"""Settings pages (General, AI provider, Connections, Security, System) and the first-run setup
wizard. Everything a new install needs can be done here, including API keys:

- Secrets typed here go to `config/secrets.json` (mode 600) via secret_store, never into the
  database, and are never shown back (write-only fields: "saved · replace · remove").
- Secrets provided by the server (`*_FILE` or environment) win and are shown read-only.
- Every connection has a Test button (read-only or free calls), and Sonarr/Radarr webhooks can
  be created with one click.
"""
from __future__ import annotations

import secrets as pysecrets
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import connections, secret_store
from ..pricing import PRICES
from ..profile import SONG_RULES, TONE_RULES

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
SECTIONS = [("general", "General"), ("ai", "AI provider"), ("connections", "Connections"),
            ("security", "Security"), ("system", "System")]
STEPS = [("welcome", "Welcome"), ("ai", "AI provider"), ("languages", "Languages"),
         ("arr", "Sonarr & Radarr"), ("media", "Media server"), ("done", "Done")]


def _iso_list(raw: str, what: str) -> list[str]:
    vals = [x for x in str(raw or "").replace(" ", "").split(",") if x]
    if any(not x.replace("-", "").isalpha() or len(x.split("-")[0]) not in (2, 3) for x in vals):
        raise ValueError(f"{what} must be ISO codes such as ru, uk, de")
    return vals


def _pairs(raw: str) -> list[list[str]]:
    out = []
    for pair in filter(None, (x.strip() for x in str(raw or "").split(";"))):
        a, _, b = pair.partition("=")
        if not a or not b:
            raise ValueError("path maps look like /downloads/=/mnt/media/")
        out.append([a, b])
    return out


def _url(raw, what: str) -> str:
    u = str(raw or "").strip().rstrip("/")
    if u and not u.startswith(("http://", "https://")):
        raise ValueError(f"{what} must start with http:// or https://")
    return u


OPENAI_EFFORTS = ("", "minimal", "low", "medium", "high")
OLLAMA_EFFORTS = ("off", "low", "medium", "high")


def _pick(v, allowed, default):
    v = str(v if v is not None else default).strip().lower()
    return v if v in allowed else default


def register_settings(app: FastAPI, render, redirect, same_origin) -> None:
    store, s = app.state.store, app.state.settings
    secrets = app.state.secrets

    # ---------------------------------------------------------------- helpers
    def secret_info() -> dict:
        """For each secret: where it comes from and whether a value exists (never the value)."""
        out = {}
        for field, (var, label) in secret_store.SECRETS.items():
            src = s.secret_sources.get(field)
            has = bool(getattr(s, field, "")) or (field == "ui_password" and bool(s.ui_password_hash))
            val = str(getattr(s, field, "") or "")
            # last 4 characters of an API key, like provider dashboards show; never for passwords or short values
            tail = val[-4:] if field.endswith("_key") and field != "webhook_key" and len(val) >= 20 else ""
            out[field] = {"label": label, "source": src, "set": has, "locked": src in ("file", "env"), "var": var,
                          "tail": tail, "saved_at": secrets.saved_at(field) if src == "ui" else 0}
        return out

    def save(values: dict) -> None:
        store.save_settings(values)
        s.apply(values)
        app.state.worker._batch_cap = None          # provider may have changed

    def save_secrets(form) -> list[str]:
        """Write-only secret fields are named `secret:<field>`; empty = keep the current value."""
        changed = []
        for field in secret_store.SECRETS:
            v = str(form.get(f"secret:{field}", "") or "").strip()
            if not v or s.secret_sources.get(field) in ("file", "env"):
                continue
            secrets.set(field, v)
            if field == "ui_password":
                s.ui_password_hash = secrets.get(field)
            else:
                setattr(s, field, v)
            s.secret_sources[field] = "ui"
            changed.append(secret_store.SECRETS[field][1])
        return changed

    def effective(form, field: str) -> str:
        """A secret for a Test: what was just typed, else the saved value."""
        return str(form.get(f"secret:{field}", "") or "").strip() or getattr(s, field, "")

    def page(request: Request, section: str, **ctx):
        return render(request, f"settings_{section}.html", section=section, sections=SECTIONS,
                      secrets=secret_info(), v=s.editable_values(), **ctx)

    def hook_base(request: Request) -> str:
        return (s.public_url or str(request.base_url)).rstrip("/")

    def setup_needed() -> bool:
        if store.load_settings().get("_setup_done"):
            return False
        has_ai = bool(s.anthropic_key) or (s.provider == "ollama" and s.ollama_model) or \
            (s.provider == "openai" and s.openai_model)
        return not (has_ai and s.languages)

    app.state.setup_needed = setup_needed

    # ---------------------------------------------------------------- pages
    @app.get("/settings")
    def settings_root():
        return RedirectResponse("/settings/general", 307)

    @app.get("/providers")
    def providers_moved():
        return RedirectResponse("/settings/ai", 307)

    @app.get("/settings/general")
    def general(request: Request):
        return page(request, "general", tones=list(TONE_RULES), songs=list(SONG_RULES))

    @app.post("/settings/general")
    async def general_save(request: Request):
        same_origin(request)
        f = await request.form()
        nxt = _next(f, "/settings/general")
        try:
            vals = {"languages": _iso_list(f.get("languages"), "target languages")}
            if not vals["languages"]:
                raise ValueError("choose at least one language to translate into")
            if "source_langs" in f:
                vals["source_langs"] = _iso_list(f.get("source_langs"), "source languages") or ["en"]
                vals["any_source"] = bool(f.get("any_source"))
            if "tone" in f:
                tone, songs = str(f.get("tone")), str(f.get("songs"))
                if tone not in TONE_RULES or songs not in SONG_RULES:
                    raise ValueError("unknown option")
                vals.update(tone=tone, songs=songs, auto_notes=bool(f.get("auto_notes")),
                            max_line=max(20, min(60, int(f.get("max_line") or 42))),
                            target_cps=max(10.0, min(30.0, float(f.get("target_cps") or 20))))
            if "monthly_cap_usd" in f:
                vals.update(monthly_cap_usd=max(0.0, float(f.get("monthly_cap_usd") or 0)),
                            alert_at=min(1.0, max(0.1, float(f.get("alert_pct") or 80) / 100)))
        except ValueError as e:
            return redirect(nxt if "/setup" in nxt else "/settings/general", f"Not saved: {e}.", "warn")
        save(vals)
        return redirect(nxt, "Saved." if "/setup" not in nxt else "")

    @app.get("/settings/ai")
    def ai(request: Request):
        models = [{"id": m, "in": p[0], "out": p[1], "ep": (16_000 * p[0] + 12_000 * p[1]) / 1e6} for m, p in PRICES.items()]
        return page(request, "ai", models=models, efforts=EFFORTS, openai_efforts=OPENAI_EFFORTS, ollama_efforts=OLLAMA_EFFORTS)

    @app.post("/settings/ai")
    async def ai_save(request: Request):
        same_origin(request)
        f = await request.form()
        nxt = _next(f, "/settings/ai")
        try:
            p = str(f.get("provider") or s.provider)
            if p not in ("anthropic", "ollama", "openai"):
                raise ValueError("unknown provider")
            vals: dict = {"provider": p}
            if p == "anthropic":
                m, e = str(f.get("model") or s.model), str(f.get("effort") or s.effort)
                if m not in PRICES or e not in EFFORTS:
                    raise ValueError("unknown model or effort")
                vals.update(model=m, effort=e)
            elif p == "ollama":
                vals.update(ollama_url=_url(f.get("ollama_url"), "the Ollama address") or "http://localhost:11434",
                            ollama_model=str(f.get("ollama_model") or "").strip(),
                            ollama_num_ctx=max(4096, min(131072, int(f.get("ollama_num_ctx") or 16384))),
                            ollama_effort=_pick(f.get("ollama_effort"), OLLAMA_EFFORTS, "off"))
                if not vals["ollama_model"]:
                    raise ValueError("click Find models and pick one")
            else:
                vals.update(openai_base_url=_url(f.get("openai_base_url"), "the base URL"),
                            openai_model=str(f.get("openai_model") or "").strip(),
                            openai_price_in=max(0.0, float(f.get("openai_price_in") or 0)),
                            openai_price_out=max(0.0, float(f.get("openai_price_out") or 0)),
                            openai_price_source=str(f.get("openai_price_source") or "entered by hand")[:80],
                            openai_max_output=max(1000, min(200000, int(f.get("openai_max_output") or 16000))),
                            openai_effort=_pick(f.get("openai_effort"), OPENAI_EFFORTS, ""))
                if not (vals["openai_base_url"] and vals["openai_model"]):
                    raise ValueError("enter the base URL and the model")
        except ValueError as e:
            return redirect(nxt if "/setup" in nxt else "/settings/ai", f"Not saved: {e}.", "warn")
        save_secrets(f)
        save(vals)
        if p == "anthropic" and not s.anthropic_key:
            return redirect(nxt, "Saved, but there is no Anthropic API key yet.", "warn")
        from ..price_lookup import is_local
        if p == "openai" and not (vals["openai_price_in"] or vals["openai_price_out"]) and not is_local(vals["openai_base_url"]):
            return redirect(nxt, "Saved, but the prices are $0: jobs will count as free and the monthly cap can't "
                                 "protect you. Use Find models or Test to fill them in, or enter them.", "warn")
        return redirect(nxt, "Saved." if "/setup" not in nxt else "")

    @app.get("/settings/connections")
    def conns(request: Request):
        hooks = {k: connections.arr_webhook_state(k, getattr(s, f"{k}_url"), getattr(s, f"{k}_key"))
                 for k in ("sonarr", "radarr")}
        return page(request, "connections", hooks=hooks, hook_base=hook_base(request),
                    path_map="; ".join(f"{a}={b}" for a, b in s.jellyfin_path_map),
                    plex_map="; ".join(f"{a}={b}" for a, b in s.plex_path_map))

    @app.post("/settings/connections")
    async def conns_save(request: Request):
        same_origin(request)
        f = await request.form()
        nxt = _next(f, "/settings/connections")
        try:
            vals = {}
            for k, what in (("sonarr_url", "Sonarr"), ("radarr_url", "Radarr"), ("bazarr_url", "Bazarr"),
                            ("jellyfin_url", "Jellyfin"), ("plex_url", "Plex"), ("ntfy_url", "ntfy")):
                if k in f:
                    vals[k] = _url(f.get(k), f"the {what} address")
            if "ntfy_topic" in f:
                vals["ntfy_topic"] = str(f.get("ntfy_topic") or "").strip()
            if "jellyfin_path_map" in f:
                vals["jellyfin_path_map"] = _pairs(f.get("jellyfin_path_map"))
            if "plex_path_map" in f:
                vals["plex_path_map"] = _pairs(f.get("plex_path_map"))
            if "opensubtitles_user" in f:
                vals["opensubtitles_user"] = str(f.get("opensubtitles_user") or "").strip()
        except ValueError as e:
            return redirect(nxt if "/setup" in nxt else "/settings/connections", f"Not saved: {e}.", "warn")
        changed = save_secrets(f)
        save(vals)
        msg = "Saved" + (f" (and {', '.join(changed)})" if changed else "") + "."
        return redirect(nxt, msg if "/setup" not in nxt else "")

    @app.post("/settings/test/{service}")
    async def test(request: Request, service: str):
        same_origin(request)
        f = await request.form()
        return await run_in_threadpool(run_test, f, service)

    def run_test(f, service: str) -> dict:
        """Blocking network calls: run in a worker thread, never on the event loop (a Sonarr
        webhook test calls back into this server while we wait)."""
        g = lambda k, d="": str(f.get(k) or d).strip()  # noqa: E731
        t0 = time.monotonic()
        if service in ("sonarr", "radarr"):
            ok, msg = connections.test_arr(service, g(f"{service}_url", getattr(s, f"{service}_url")), effective(f, f"{service}_key"))
        elif service == "bazarr":
            ok, msg = connections.test_bazarr(g("bazarr_url", s.bazarr_url), effective(f, "bazarr_key"))
        elif service == "jellyfin":
            ok, msg = connections.test_jellyfin(g("jellyfin_url", s.jellyfin_url), effective(f, "jellyfin_key"))
        elif service == "plex":
            ok, msg = connections.test_plex(g("plex_url", s.plex_url), effective(f, "plex_token"))
        elif service == "ntfy":
            ok, msg = connections.test_ntfy(g("ntfy_url", s.ntfy_url), g("ntfy_topic", s.ntfy_topic))
        elif service == "opensubtitles":
            from .. import __version__
            ok, msg = connections.test_opensubtitles(g("opensubtitles_user", s.opensubtitles_user),
                                                     effective(f, "opensubtitles_password"),
                                                     s.opensubtitles_user_agent or f"Captionizerr v{__version__}")
        elif service == "anthropic":
            try:
                from ..providers.anthropic_provider import AnthropicProvider
                key = effective(f, "anthropic_key")
                if not key:
                    raise ValueError("enter the API key")
                n = AnthropicProvider(model=g("model", s.model), api_key=key).count_tokens("", "", "Captionizerr test")
                ok, msg = True, f"key works ({n} tokens counted, free)"
            except Exception as e:
                ok, msg = False, str(e)[:200]
        elif service == "ollama":
            try:
                from ..providers.ollama import OllamaProvider
                m = g("ollama_model", s.ollama_model)
                r = OllamaProvider(g("ollama_url", s.ollama_url), m, 4096, 50, timeout=300, effort=_pick(f.get("ollama_effort"), OLLAMA_EFFORTS, "off")).complete("", "", "Reply with OK.")
                ok, msg = True, f"{m} answered “{r.text.strip()[:30]}”"
            except Exception as e:
                ok, msg = False, str(e)[:200]
        elif service == "openai":
            try:
                from ..providers.openai_compat import OpenAICompatProvider
                base, model = g("openai_base_url", s.openai_base_url), g("openai_model", s.openai_model)
                r = OpenAICompatProvider(base, model, effective(f, "openai_key"), 2000, timeout=60, effort=_pick(f.get("openai_effort"), OPENAI_EFFORTS, "")).complete("", "", "Reply with OK.")
                ok, msg = True, f"model answered “{r.text.strip()[:30]}”"
                from ..price_lookup import lookup
                pr = lookup("openai", base, model, s.cache_dir)
                if pr:
                    return {"ok": ok, "message": msg + f" · ≈ ${pr.per_episode:.3f} per episode ({pr.source})",
                            "seconds": round(time.monotonic() - t0, 1), "price": pr.as_dict()}
            except Exception as e:
                ok, msg = False, str(e)[:200]
        else:
            raise HTTPException(404)
        return {"ok": ok, "message": msg, "seconds": round(time.monotonic() - t0, 1)}

    @app.post("/settings/models/openai")
    async def openai_models(request: Request):
        same_origin(request)
        f = await request.form()
        from ..price_lookup import list_openai_models
        try:
            base = _url(f.get("openai_base_url") or s.openai_base_url, "the base URL")
            return {"models": await run_in_threadpool(list_openai_models, base, effective(f, "openai_key"))}
        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, 502)

    @app.post("/settings/price")
    async def price(request: Request):
        """Look a model's price up (built-in table, OpenRouter, LiteLLM; local servers are free)."""
        same_origin(request)
        f = await request.form()
        from ..price_lookup import lookup
        p = await run_in_threadpool(lookup, str(f.get("provider") or s.provider), str(f.get("base_url") or ""),
                                    str(f.get("model") or ""), s.cache_dir)
        return {"found": False} if p is None else {"found": True, **p.as_dict()}

    @app.post("/settings/price/check")
    async def price_check(request: Request):
        same_origin(request)
        from ..price_lookup import check_builtin
        return await run_in_threadpool(check_builtin, s.cache_dir)

    @app.post("/settings/ollama/models")
    async def ollama_models(request: Request):
        same_origin(request)
        from ..providers import ollama
        b = await request.json()
        url = str((b or {}).get("url") or s.ollama_url).strip()
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"error": "the address must start with http://"}, 400)
        try:
            return {"models": await run_in_threadpool(ollama.list_models, url)}
        except Exception as e:
            return JSONResponse({"error": str(e)[:200]}, 502)

    @app.post("/settings/connect/{kind}")
    async def connect(request: Request, kind: str):
        same_origin(request)
        if kind not in ("sonarr", "radarr"):
            raise HTTPException(404)
        url, key = getattr(s, f"{kind}_url"), getattr(s, f"{kind}_key")
        if not (url and key):
            return {"ok": False, "message": f"save the {kind.title()} address and API key first"}
        ok, msg = await run_in_threadpool(connections.connect_arr, kind, url, key,
                                          f"{hook_base(request)}/api/hooks/{kind}", s.webhook_key)
        if ok and not s.public_url:
            save({"public_url": hook_base(request)})     # notifications link here too
        return {"ok": ok, "message": msg}

    @app.post("/settings/secret/{field}/remove")
    async def secret_remove(request: Request, field: str):
        same_origin(request)
        f = await request.form()
        if field not in secret_store.SECRETS or s.secret_sources.get(field) != "ui":
            raise HTTPException(400, "only secrets saved in the UI can be removed here")
        if field == "webhook_key":
            raise HTTPException(400, "the webhook key can be regenerated, not removed")
        secrets.delete(field)
        if field == "ui_password":
            s.ui_password_hash = ""
        else:
            setattr(s, field, "")
        s.secret_sources[field] = None
        return redirect(_next(f, "/settings/connections"), f"Removed the {secret_store.SECRETS[field][1]}.")

    @app.get("/settings/security")
    def security(request: Request, reveal: str = ""):
        return page(request, "security", hook_base=hook_base(request),
                    webhook_shown=s.webhook_key if reveal == "webhook" and s.secret_sources.get("webhook_key") == "ui" else "",
                    login=secret_store.login_enabled(s))

    @app.post("/settings/security")
    async def security_save(request: Request):
        same_origin(request)
        f = await request.form()
        nxt = _next(f, "/settings/security")
        vals = {}
        if "public_url" in f:
            try:
                vals["public_url"] = _url(f.get("public_url"), "this page's address")
            except ValueError as e:
                return redirect("/settings/security", f"Not saved: {e}.", "warn")
        if "ui_user" in f and str(f.get("ui_user")).strip():
            vals["ui_user"] = str(f.get("ui_user")).strip()[:64]
        pw, pw2 = str(f.get("secret:ui_password") or ""), str(f.get("password_again") or "")
        if pw and (pw != pw2 or len(pw) < 8):
            return redirect(nxt, "Password not set: the two entries must match and be at least 8 characters.", "warn")
        save(vals)
        changed = save_secrets(f)
        msg = "Saved." + (" The browser will now ask for the password." if "Web UI password" in changed else "")
        return redirect(nxt, msg if "/setup" not in nxt else "")

    @app.post("/settings/webhook/regenerate")
    async def webhook_regen(request: Request):
        same_origin(request)
        if s.secret_sources.get("webhook_key") in ("file", "env"):
            return redirect("/settings/security", "The webhook key is set on the server; change it there.", "warn")
        new = pysecrets.token_urlsafe(24)
        secrets.set("webhook_key", new)
        s.webhook_key = new
        base = hook_base(request)

        def reconnect() -> list[str]:
            done = []
            for kind in ("sonarr", "radarr"):
                if connections.arr_webhook_state(kind, getattr(s, f"{kind}_url"), getattr(s, f"{kind}_key")) == "ok":
                    ok, _ = connections.connect_arr(kind, getattr(s, f"{kind}_url"), getattr(s, f"{kind}_key"),
                                                    f"{base}/api/hooks/{kind}", new)
                    done.append(f"{kind.title()} {'updated' if ok else 'NOT updated'}")
            return done
        done = await run_in_threadpool(reconnect)
        return redirect("/settings/security", "New webhook key. " + ("; ".join(done) + "." if done else
                                                                    "Update it wherever you pasted the old one."))

    @app.get("/settings/system")
    def system(request: Request):
        from .. import __version__, maintenance
        last = store.load_settings().get("_maintenance_last") or {}
        return page(request, "system", storage=maintenance.storage(s), last=last, version=__version__,
                    keep={"history": s.keep_history, "days": s.keep_days}, overridden=overrides(),
                    last_when=datetime.fromtimestamp(last["at"]).strftime("%d %b %H:%M") if last.get("at") else "")

    LABELS = {
        "alert_at": "Budget warning (% of cap)", "any_source": "Translate from any language", "auto_notes": "Draft series notes",
        "bazarr_url": "Bazarr address", "effort": "Claude effort", "jellyfin_path_map": "Jellyfin path mapping",
        "jellyfin_url": "Jellyfin address", "languages": "Translate into", "max_line": "Characters per line",
        "model": "Claude model", "monthly_cap_usd": "Monthly cap (USD)", "ntfy_topic": "ntfy topic", "ntfy_url": "ntfy server",
        "ollama_max_output": "Ollama max output", "ollama_model": "Ollama model", "ollama_num_ctx": "Ollama context size",
        "ollama_url": "Ollama address", "openai_base_url": "OpenAI-compatible base URL", "openai_max_output": "OpenAI-compatible max output",
        "openai_model": "OpenAI-compatible model", "openai_price_in": "Input price ($/1M tokens)",
        "openai_price_out": "Output price ($/1M tokens)", "openai_price_source": "Price source",
        "opensubtitles_user": "OpenSubtitles user", "plex_path_map": "Plex path mapping", "plex_url": "Plex address",
        "provider": "AI provider", "public_url": "Public address", "radarr_url": "Radarr address", "sonarr_url": "Sonarr address",
        "songs": "Songs and poems", "source_langs": "Translate from", "target_cps": "Reading speed", "tone": "Swearing",
        "ui_user": "Login user", "openai_effort": "OpenAI-compatible effort", "ollama_effort": "Ollama thinking"}

    def overrides() -> list[dict]:
        env, saved = app.state.env_defaults, store.load_settings()
        out = []
        for k in sorted(saved):
            if k not in s.EDITABLE:
                continue
            norm = (lambda v: [list(x) for x in v]) if s.EDITABLE[k] == "pairs" else (lambda v: v)
            if norm(getattr(s, k)) != norm(getattr(env, k)):
                out.append({"key": k, "label": LABELS.get(k, k.replace("_", " ")), "value": getattr(s, k), "env": getattr(env, k)})
        return out

    @app.post("/settings/reset")
    async def settings_reset(request: Request):
        same_origin(request)
        f = await request.form()
        key = str(f.get("key", ""))
        keys = [o["key"] for o in overrides()] if key == "all" else [key]
        keys = [k for k in keys if k in s.EDITABLE]
        if not keys:
            return redirect("/settings/system", "Nothing to reset.", "warn")
        store.delete_settings(keys)
        for k in keys:
            setattr(s, k, getattr(app.state.env_defaults, k))
        return redirect("/settings/system", f"Back to the environment value: {', '.join(k.replace('_', ' ') for k in keys)}.")

    @app.post("/maintenance/run")
    def maintenance_run(request: Request):
        same_origin(request)
        from .. import maintenance
        rep = maintenance.run(s, store)
        return redirect("/settings/system", "Cleanup done: " + ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in rep.items() if k != "at") + ".")

    # ---------------------------------------------------------------- setup wizard
    @app.get("/setup")
    def setup(request: Request, step: str = "welcome"):
        keys = [k for k, _ in STEPS]
        if step not in keys:
            step = "welcome"
        i = keys.index(step)
        hooks = {k: connections.arr_webhook_state(k, getattr(s, f"{k}_url"), getattr(s, f"{k}_key"))
                 for k in ("sonarr", "radarr")} if step == "arr" else {}
        models = [{"id": m, "ep": (16_000 * p[0] + 12_000 * p[1]) / 1e6} for m, p in PRICES.items()]
        return render(request, "setup.html", step=step, steps=STEPS, index=i,
                      next_step=keys[min(i + 1, len(keys) - 1)], prev_step=keys[max(i - 1, 0)],
                      secrets=secret_info(), v=s.editable_values(), efforts=EFFORTS, models=models,
                      hooks=hooks, hook_base=hook_base(request), login=secret_store.login_enabled(s))

    @app.post("/setup/finish")
    def setup_finish(request: Request):
        same_origin(request)
        store.save_settings({"_setup_done": True})
        return redirect("/", "Setup complete. New imports will be translated automatically.")


def _next(form, default: str) -> str:
    from .ui import local_path
    return local_path(form.get("next"), default)
