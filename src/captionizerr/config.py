"""Service settings, read from environment variables (CAPTIONIZERR_*)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"CAPTIONIZERR_{name}", default).strip()


@dataclass
class Settings:
    data_dir: Path = Path("/config")
    webhook_key: str = ""
    ui_user: str = "admin"
    ui_password: str = ""        # set it to require a login for the web UI (HTTP Basic); empty = open (LAN only!)
    languages: list[str] = field(default_factory=list)   # no default: chosen at first run
    model: str = "claude-opus-5-5"
    effort: str = "low"
    monthly_cap_usd: float = 20.0
    alert_at: float = 0.8
    ntfy_url: str = ""            # e.g. https://ntfy.example.com
    ntfy_topic: str = ""
    public_url: str = ""          # link shown in notifications
    path_map: list[tuple[str, str]] = field(default_factory=list)   # (arr path prefix, local prefix)
    workers: int = 1
    library_roots: list[str] = field(default_factory=lambda: ["/downloads"])
    tone: str = "faithful"
    songs: str = "verse"
    auto_notes: bool = True
    max_line: int = 42
    target_cps: float = 20.0
    sonarr_url: str = ""
    sonarr_key: str = ""
    radarr_url: str = ""
    radarr_key: str = ""
    jellyfin_url: str = ""
    jellyfin_key: str = ""
    jellyfin_path_map: list[tuple[str, str]] = field(default_factory=list)   # (our path prefix, Jellyfin's)
    # sources
    source_langs: list[str] = field(default_factory=lambda: ["en"])   # translate FROM, in order of preference
    any_source: bool = True       # fall back to any other text track/sidecar language
    bazarr_url: str = ""
    bazarr_key: str = ""
    bazarr_wait_hours: float = 3.0
    # AI provider
    provider: str = "anthropic"   # anthropic | ollama (local) | openai (any OpenAI-compatible endpoint)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = ""
    ollama_num_ctx: int = 16384   # context window; Ollama's own default is far too small for subtitles
    ollama_max_output: int = 8000
    openai_base_url: str = ""     # e.g. https://api.openai.com/v1, http://ollama:11434/v1
    openai_key: str = ""
    openai_model: str = ""
    openai_price_in: float = 0.0  # $ per 1M tokens; 0 for local models
    openai_price_out: float = 0.0
    openai_max_output: int = 16000
    # batch backlog
    batch_min_wait_s: int = 120   # collect backlog jobs this long before sending one batch
    # housekeeping
    keep_history: int = 10        # versions per edited file
    keep_days: int = 180          # history/orphans/finished-job retention

    @classmethod
    def from_env(cls) -> "Settings":
        def pairs(name):
            out = []
            for pair in filter(None, _env(name).split(";")):
                src, _, dst = pair.partition("=")
                if src and dst:
                    out.append((src, dst))
            return out
        pm = pairs("PATH_MAP")
        return cls(
            data_dir=Path(_env("DATA_DIR", "/config")),
            webhook_key=_env("WEBHOOK_KEY"),
            ui_user=_env("UI_USER", "admin"),
            ui_password=_env("UI_PASSWORD"),
            languages=[x for x in _env("LANGUAGES").replace(" ", "").split(",") if x],
            model=_env("MODEL", "claude-opus-5-5"),
            effort=_env("EFFORT", "low"),
            monthly_cap_usd=float(_env("MONTHLY_CAP_USD", "20") or 20),
            alert_at=float(_env("ALERT_AT", "0.8") or 0.8),
            ntfy_url=_env("NTFY_URL"),
            ntfy_topic=_env("NTFY_TOPIC"),
            public_url=_env("PUBLIC_URL"),
            path_map=pm,
            workers=max(1, int(_env("WORKERS", "1") or 1)),
            library_roots=[x for x in _env("LIBRARY_ROOTS", "/downloads").split(";") if x],
            tone=_env("TONE", "faithful"),
            songs=_env("SONGS", "verse"),
            auto_notes=_env("AUTO_NOTES", "1").lower() not in ("0", "false", "no", "off"),
            max_line=int(_env("MAX_LINE", "42") or 42),
            target_cps=float(_env("TARGET_CPS", "20") or 20),
            sonarr_url=_env("SONARR_URL"),
            sonarr_key=_env("SONARR_KEY"),
            radarr_url=_env("RADARR_URL"),
            radarr_key=_env("RADARR_KEY"),
            jellyfin_url=_env("JELLYFIN_URL"),
            jellyfin_key=_env("JELLYFIN_KEY"),
            jellyfin_path_map=pairs("JELLYFIN_PATH_MAP"),
            source_langs=[x for x in _env("SOURCE_LANGS", "en").replace(" ", "").split(",") if x],
            any_source=_env("ANY_SOURCE", "1").lower() not in ("0", "false", "no", "off"),
            bazarr_url=_env("BAZARR_URL"),
            bazarr_key=_env("BAZARR_KEY"),
            provider=_env("PROVIDER", "anthropic"),
            ollama_url=_env("OLLAMA_URL", "http://localhost:11434"),
            ollama_model=_env("OLLAMA_MODEL"),
            ollama_num_ctx=int(_env("OLLAMA_NUM_CTX", "16384") or 16384),
            openai_base_url=_env("OPENAI_BASE_URL"),
            openai_key=_env("OPENAI_API_KEY"),
            openai_model=_env("OPENAI_MODEL"),
            openai_price_in=float(_env("OPENAI_PRICE_IN", "0") or 0),
            openai_price_out=float(_env("OPENAI_PRICE_OUT", "0") or 0),
            openai_max_output=int(_env("OPENAI_MAX_OUTPUT", "16000") or 16000),
        )

    def profile(self, target: str, source: str = "en", tone: str | None = None):
        """Translation profile for one target language, with script-aware readability limits."""
        from .profile import LANG_SCRIPT, Profile
        max_line, cps = self.max_line, self.target_cps
        if LANG_SCRIPT.get(target) in ("cjk", "hangul"):   # full-width scripts: ~16 per line, ~9 per second
            max_line, cps = min(max_line, 16), min(cps, 9.0)
        return Profile(target_lang=target, source_lang=source, tone=tone or self.tone, songs=self.songs,
                       max_line=max_line, target_cps=cps)

    @property
    def active_model(self) -> str:
        return {"openai": self.openai_model, "ollama": self.ollama_model}.get(self.provider, self.model)

    # settings the web UI may change (secrets never: they stay in the environment)
    EDITABLE = {
        "languages": "list", "model": "str", "effort": "str", "monthly_cap_usd": "float", "alert_at": "float",
        "tone": "str", "songs": "str", "max_line": "int", "target_cps": "float",
        "ntfy_url": "str", "ntfy_topic": "str", "public_url": "str", "jellyfin_url": "str",
        "jellyfin_path_map": "pairs", "sonarr_url": "str", "radarr_url": "str",
        "source_langs": "list", "any_source": "bool", "bazarr_url": "str", "auto_notes": "bool",
        "provider": "str", "ollama_url": "str", "ollama_model": "str", "ollama_num_ctx": "int",
        "ollama_max_output": "int", "openai_base_url": "str", "openai_model": "str", "openai_price_in": "float",
        "openai_price_out": "float", "openai_max_output": "int",
    }

    def apply(self, values: dict) -> None:
        for k, v in values.items():
            if k in self.EDITABLE and v is not None:
                setattr(self, k, [tuple(x) for x in v] if self.EDITABLE[k] == "pairs" else v)

    def editable_values(self) -> dict:
        return {k: getattr(self, k) for k in self.EDITABLE}

    @property
    def cache_dir(self) -> Path:
        return Path(self.data_dir) / "cache"

    @property
    def history_dir(self) -> Path:
        return Path(self.data_dir) / "history"

    @property
    def feedback_dir(self) -> Path:
        return Path(self.data_dir) / "feedback"

    @property
    def secrets_status(self) -> dict[str, bool]:
        return {"Anthropic API key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "Webhook key": bool(self.webhook_key), "Sonarr API key": bool(self.sonarr_key),
                "Radarr API key": bool(self.radarr_key), "Jellyfin API key": bool(self.jellyfin_key),
                "Bazarr API key": bool(self.bazarr_key), "OpenAI-compatible API key": bool(self.openai_key)}

    VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm")

    def in_library(self, p: Path) -> bool:
        """Only real video files under a configured library root are ever processed."""
        try:
            rp = p.resolve()
        except OSError:
            return False
        return p.suffix.lower() in self.VIDEO_EXT and any(
            rp.is_relative_to(Path(r).resolve()) for r in self.library_roots)

    def map_path(self, p: str) -> Path:
        for src, dst in self.path_map:
            if p.startswith(src):
                return Path(dst + p[len(src):])
        return Path(p)

    @property
    def context_dir(self) -> Path:
        return self.data_dir / "context"
