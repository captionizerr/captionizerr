"""Connection tests for the settings pages and the setup wizard, and one-click webhook setup in
Sonarr/Radarr. Every test is read-only or free, returns (ok, human message), and never raises."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

UA = {"User-Agent": "Captionizerr"}


def _req(url: str, headers: dict | None = None, method: str = "GET", body=None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    h = {**UA, **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw)


def _why(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return {401: "the key was rejected (401)", 403: "access denied (403)",
                404: "not found at that address (404); check the URL"}.get(e.code, f"HTTP {e.code}")
    if isinstance(e, urllib.error.URLError):
        return f"can't reach it ({getattr(e, 'reason', e)})"
    return f"{type(e).__name__}: {str(e)[:120]}"


def test_arr(kind: str, url: str, key: str) -> tuple[bool, str]:
    if not url or not key:
        return False, "enter the address and the API key"
    try:
        _, st = _req(f"{url.rstrip('/')}/api/v3/system/status", {"X-Api-Key": key})
        name = st.get("appName") or kind.title()
        if name.lower() != kind:
            return False, f"that address is {name}, not {kind.title()}"
        _, items = _req(f"{url.rstrip('/')}/api/v3/{'series' if kind == 'sonarr' else 'movie'}", {"X-Api-Key": key}, timeout=30)
        return True, f"{name} {st.get('version', '')}: {len(items)} {'series' if kind == 'sonarr' else 'films'}"
    except Exception as e:
        return False, _why(e)


def test_bazarr(url: str, key: str) -> tuple[bool, str]:
    if not url or not key:
        return False, "enter the address and the API key"
    try:
        _, st = _req(f"{url.rstrip('/')}/api/system/status", {"X-API-KEY": key})
        return True, f"Bazarr {(st.get('data') or {}).get('bazarr_version', '')}"
    except Exception as e:
        return False, _why(e)


def test_jellyfin(url: str, key: str) -> tuple[bool, str]:
    if not url or not key:
        return False, "enter the address and the API key"
    try:
        _, st = _req(f"{url.rstrip('/')}/System/Info", {"X-Emby-Token": key})
        return True, f"{st.get('ProductName') or 'Server'} {st.get('Version', '')} ({st.get('ServerName', '')})"
    except Exception as e:
        return False, _why(e)


def test_plex(url: str, token: str) -> tuple[bool, str]:
    if not url or not token:
        return False, "enter the address and the token"
    try:
        _, st = _req(f"{url.rstrip('/')}/identity?X-Plex-Token={token}", {"Accept": "application/json"})
        mc = (st or {}).get("MediaContainer", {}) if isinstance(st, dict) else {}
        return True, f"Plex {mc.get('version', '')}"
    except Exception as e:
        return False, _why(e)


def test_ntfy(url: str, topic: str) -> tuple[bool, str]:
    if not url or not topic:
        return False, "enter the server and the topic"
    try:
        _req(url.rstrip("/"), method="POST", body={"topic": topic, "title": "Captionizerr",
                                                  "message": "Test notification: notifications work.",
                                                  "tags": ["white_check_mark"]})
        return True, f"sent a test notification to “{topic}”"
    except Exception as e:
        return False, _why(e)


def test_opensubtitles(user: str, password: str, ua: str) -> tuple[bool, str]:
    if not user or not password:
        return False, "enter the username and password"
    try:
        from .opensubtitles import Client
        c = Client(user, password, ua)
        c.login()
        return True, "logged in to OpenSubtitles"
    except Exception as e:
        return False, str(e)[:400]


# ------------------------------------------------------------------ Sonarr/Radarr webhook
def connect_arr(kind: str, url: str, key: str, hook_url: str, webhook_key: str) -> tuple[bool, str]:
    """Create or update a webhook named "Captionizerr" in Sonarr/Radarr (import, upgrade and rename
    events; the key as the Basic-auth password) and let the app send its test event."""
    base, h = f"{url.rstrip('/')}/api/v3", {"X-Api-Key": key}
    try:
        _, notes = _req(f"{base}/notification", h)
        ours = [n for n in notes if n.get("implementation") == "Webhook" and (
            n.get("name") == "Captionizerr" or any(f.get("name") == "url" and "/api/hooks/" in str(f.get("value"))
                                                    for f in n.get("fields", [])))]
        if ours:
            n = ours[0]
        else:
            _, schema = _req(f"{base}/notification/schema", h)
            n = next(x for x in schema if x.get("implementation") == "Webhook")
            n.pop("id", None)
        n["name"] = "Captionizerr"
        for flag in ("onDownload", "onUpgrade", "onRename"):
            if flag in n:
                n[flag] = True
        for f in n.get("fields", []):
            if f["name"] == "url":
                f["value"] = hook_url
            elif f["name"] == "method":
                f["value"] = 1                                 # POST
            elif f["name"] == "username":
                f["value"] = kind
            elif f["name"] == "password":
                f["value"] = webhook_key
        if n.get("id"):
            _req(f"{base}/notification/{n['id']}", h, "PUT", n)
            verb = "updated"
        else:
            _, created = _req(f"{base}/notification", h, "POST", n)
            n["id"] = created.get("id") if isinstance(created, dict) else None
            verb = "created"
        _req(f"{base}/notification/test", h, "POST", n, timeout=30)
        return True, f"webhook {verb} in {kind.title()}, and its test event reached CaptionizeRR"
    except StopIteration:
        return False, f"{kind.title()} has no Webhook connection type"
    except Exception as e:
        return False, _why(e)


def arr_webhook_state(kind: str, url: str, key: str) -> str | None:
    """'ok' if Sonarr/Radarr has our webhook, 'missing' if not, None if it can't be checked."""
    if not url or not key:
        return None
    try:
        _, notes = _req(f"{url.rstrip('/')}/api/v3/notification", {"X-Api-Key": key}, timeout=5)
    except Exception:
        return None
    return "ok" if any(n.get("implementation") == "Webhook" and any(
        f.get("name") == "url" and "/api/hooks/" in str(f.get("value")) for f in n.get("fields", [])) for n in notes) else "missing"
