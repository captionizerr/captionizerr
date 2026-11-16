"""Secrets entered in the web UI (the Seerr-style setup), and where each secret comes from.

Order of precedence for every secret:
  1. `<VAR>_FILE`  - a file, e.g. a compose secret (shown as "set on the server")
  2. `<VAR>`       - the environment                (shown as "set on the server")
  3. the UI        - `config/secrets.json`, mode 600, never in the database, never shown back
The UI can only replace or remove secrets it stored itself. The UI login password is stored
as a salted PBKDF2 hash, never in plain text.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import time
from pathlib import Path

# Settings field -> (environment variable, label)
SECRETS: dict[str, tuple[str, str]] = {
    "anthropic_key": ("ANTHROPIC_API_KEY", "Anthropic API key"),
    "openai_key": ("CAPTIONIZERR_OPENAI_API_KEY", "OpenAI-compatible API key"),
    "webhook_key": ("CAPTIONIZERR_WEBHOOK_KEY", "Webhook key"),
    "sonarr_key": ("CAPTIONIZERR_SONARR_KEY", "Sonarr API key"),
    "radarr_key": ("CAPTIONIZERR_RADARR_KEY", "Radarr API key"),
    "bazarr_key": ("CAPTIONIZERR_BAZARR_KEY", "Bazarr API key"),
    "jellyfin_key": ("CAPTIONIZERR_JELLYFIN_KEY", "Jellyfin / Emby API key"),
    "plex_token": ("CAPTIONIZERR_PLEX_TOKEN", "Plex token"),
    "opensubtitles_password": ("CAPTIONIZERR_OPENSUBTITLES_PASSWORD", "OpenSubtitles password"),
    "ui_password": ("CAPTIONIZERR_UI_PASSWORD", "Web UI password"),
}
PBKDF2_ROUNDS = 310_000


def server_source(field: str) -> str | None:
    """'file' or 'env' when the server provides this secret (the UI can't change it), else None."""
    var = SECRETS[field][0]
    if os.environ.get(f"{var}_FILE", "").strip():
        return "file"
    if os.environ.get(var, "").strip():
        return "env"
    return None


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, rounds, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(calc.hex(), dk)
    except (ValueError, TypeError):
        return False


class SecretStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def get(self, field: str) -> str:
        return self._load().get(field, "")

    def has(self, field: str) -> bool:
        return bool(self._load().get(field))

    def set(self, field: str, value: str) -> None:
        if field not in SECRETS:
            raise KeyError(field)
        data = self._load()
        data[field] = hash_password(value) if field == "ui_password" else value
        data.setdefault("_saved_at", {})[field] = int(time.time())
        self._save(data)

    def saved_at(self, field: str) -> int:
        return int((self._load().get("_saved_at") or {}).get(field) or 0)

    def delete(self, field: str) -> None:
        data = self._load()
        if data.pop(field, None) is not None:
            (data.get("_saved_at") or {}).pop(field, None)
            self._save(data)


def apply(settings, store: SecretStore) -> None:
    """Fill secrets the server doesn't provide from the UI store. Records each secret's source
    in `settings.secret_sources` (file | env | ui | None). Creates a webhook key on first run."""
    sources = {}
    for field in SECRETS:
        # a value already on the Settings object (environment, or passed in directly) counts as the server's
        src = server_source(field) or ("env" if getattr(settings, field, "") else None)
        if src is None and store.has(field):
            src = "ui"
            if field == "ui_password":
                settings.ui_password_hash = store.get(field)
            else:
                setattr(settings, field, store.get(field))
        sources[field] = src
    if sources["webhook_key"] is None:                   # webhooks must work out of the box
        store.set("webhook_key", _secrets.token_urlsafe(24))
        settings.webhook_key = store.get("webhook_key")
        sources["webhook_key"] = "ui"
    settings.secret_sources = sources


def login_enabled(settings) -> bool:
    return bool(settings.ui_password or getattr(settings, "ui_password_hash", ""))


def check_login(settings, user: str, pw: str) -> bool:
    if not hmac.compare_digest(user, settings.ui_user):
        return False
    if settings.ui_password:                              # set on the server, plain
        return hmac.compare_digest(pw, settings.ui_password)
    h = getattr(settings, "ui_password_hash", "")
    return bool(h) and verify_password(pw, h)
