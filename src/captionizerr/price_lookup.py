"""Find a model's price automatically, so users pick a model instead of typing prices.

Sources, in order:
  1. local servers (Ollama, LM Studio, localhost...)  -> $0
  2. Captionizerr's own table (Anthropic models)
  3. OpenRouter's public model list, when the base URL is OpenRouter (live prices)
  4. LiteLLM's community price list (github.com/BerriAI/litellm), for OpenAI and most others
Vendor model APIs (OpenAI, Anthropic) don't publish prices. The LiteLLM list is maintained by a
community project, not the vendors, so results always carry their source and stay editable.
Downloads are cached for 24 h in config/cache/ and a stale copy is used if a refresh fails.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from .pricing import PRICES

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
MAX_AGE = 24 * 3600
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "ollama", "lmstudio", "host.docker.internal")
EP_IN, EP_OUT = 16_000, 12_000            # a typical 45-60 min episode (measured on real episodes)


@dataclass
class Price:
    inp: float                 # $ per 1M input tokens
    out: float                 # $ per 1M output tokens
    cache_read: float          # $ per 1M cached input tokens
    source: str                # human-readable source
    updated: float = 0.0       # when the source list was fetched (0 = built in)

    @property
    def per_episode(self) -> float:
        return (EP_IN * self.inp + EP_OUT * self.out) / 1_000_000

    def as_dict(self) -> dict:
        d = asdict(self)
        d["per_episode"] = round(self.per_episode, 4)
        return d


def _fetch_json(url: str, cache: Path, timeout: float = 20) -> tuple[object, float] | None:
    """(data, fetched_at) from cache if fresh, else download; stale cache if the download fails."""
    cached = None
    if cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - cached["fetched"] < MAX_AGE:
                return cached["data"], cached["fetched"]
        except (ValueError, KeyError):
            cached = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Captionizerr"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fetched": time.time(), "data": data}), encoding="utf-8")
        return data, time.time()
    except Exception:
        return (cached["data"], cached["fetched"]) if cached else None


def is_local(base_url: str) -> bool:
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith((".local", ".lan")) or host.startswith(("192.168.", "10.", "172."))


def litellm(model: str, cache_dir: Path, fetch=_fetch_json) -> Price | None:
    got = fetch(LITELLM_URL, Path(cache_dir) / "prices-litellm.json")
    if not got:
        return None
    data, when = got
    entry = data.get(model) if isinstance(data, dict) else None
    if entry is None and isinstance(data, dict):        # e.g. "deepseek/deepseek-chat" for "deepseek-chat"
        entry = next((v for k, v in data.items() if isinstance(v, dict) and k.endswith("/" + model)), None)
    if not isinstance(entry, dict) or entry.get("input_cost_per_token") is None:
        return None
    m = 1_000_000
    inp = float(entry["input_cost_per_token"]) * m
    cr = entry.get("cache_read_input_token_cost")
    return Price(inp, float(entry.get("output_cost_per_token") or 0) * m,
                 float(cr) * m if cr is not None else inp / 10, "LiteLLM community price list", when)


def openrouter(model: str, cache_dir: Path, fetch=_fetch_json) -> Price | None:
    got = fetch(OPENROUTER_URL, Path(cache_dir) / "prices-openrouter.json")
    if not got:
        return None
    data, when = got
    for m in (data or {}).get("data", []):
        if m.get("id") == model:
            p = m.get("pricing") or {}
            k = 1_000_000
            inp = float(p.get("prompt") or 0) * k
            return Price(inp, float(p.get("completion") or 0) * k,
                         float(p.get("input_cache_read") or 0) * k or inp / 10, "OpenRouter (live)", when)
    return None


def lookup(provider: str, base_url: str, model: str, cache_dir: Path, fetch=_fetch_json) -> Price | None:
    model = (model or "").strip()
    if not model:
        return None
    if provider == "ollama" or (provider == "openai" and is_local(base_url)):
        return Price(0, 0, 0, "local server: free")
    if provider == "anthropic" and model in PRICES:
        i, o, c = PRICES[model]
        return Price(i, o, c, "CaptionizeRR's built-in table")
    if "openrouter.ai" in (base_url or ""):
        p = openrouter(model, cache_dir, fetch)
        if p:
            return p
    return litellm(model, cache_dir, fetch)


def check_builtin(cache_dir: Path, fetch=_fetch_json) -> dict[str, dict]:
    """Compare the built-in Anthropic table with LiteLLM, so price changes get noticed."""
    out = {}
    for model, (i, o, _c) in PRICES.items():
        p = litellm(model, cache_dir, fetch)
        if p is None:
            out[model] = {"status": "unknown"}
        elif abs(p.inp - i) > 1e-9 or abs(p.out - o) > 1e-9:
            out[model] = {"status": "differs", "litellm": [round(p.inp, 4), round(p.out, 4)], "builtin": [i, o]}
        else:
            out[model] = {"status": "matches"}
    return out


def list_openai_models(base_url: str, key: str, timeout: float = 20) -> list[str]:
    """Model ids from an OpenAI-compatible /models endpoint (OpenAI, OpenRouter, vLLM, LM Studio...)."""
    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"User-Agent": "Captionizerr", **({"Authorization": f"Bearer {key}"} if key else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return sorted({m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")})
