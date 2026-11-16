"""Ollama (local models), through its native API.

Why not Ollama's OpenAI-compatible `/v1` endpoint: it can't set the context window, so a model
falls back to Ollama's small default and silently truncates a long prompt. `/api/chat` takes
`options.num_ctx`, so the window is sized for the chunk being translated.

Local models are free (price 0 in the budget) but slower and noticeably weaker translators than
the Anthropic models; episodes go in chunks sized to fit the context window.
"""
from __future__ import annotations

import httpx

from ..pricing import Usage, register
from .base import ProviderError, ProviderResult

DEFAULT_URL = "http://localhost:11434"


def root(url: str) -> str:
    """Accept http://host:11434, .../, or .../v1 (people paste the OpenAI-style URL)."""
    u = (url or DEFAULT_URL).strip().rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def list_models(url: str, timeout: float = 5) -> list[dict]:
    """Installed models: [{'name', 'size_gb', 'params', 'quant'}], largest first."""
    try:
        r = httpx.get(f"{root(url)}/api/tags", timeout=timeout)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise ProviderError(f"Ollama not reachable at {root(url)}: {e}") from e
    out = []
    for m in r.json().get("models", []):
        d = m.get("details") or {}
        out.append({"name": m.get("name") or m.get("model"), "size_gb": round((m.get("size") or 0) / 1e9, 1),
                    "params": d.get("parameter_size", ""), "quant": d.get("quantization_level", "")})
    return sorted(out, key=lambda x: -x["size_gb"])


class OllamaProvider:
    name = "ollama"
    supports_batch = False

    def __init__(self, url: str, model: str, num_ctx: int = 16384, max_output: int = 8000,
                 timeout: float = 1800, client: httpx.Client | None = None, effort: str = "off"):
        if not model:
            raise ProviderError("pick an Ollama model first")
        self.url, self.model = root(url), model
        self.num_ctx, self.max_output = num_ctx, min(max_output, num_ctx // 2)
        # per cue: ~30 input + ~40 output tokens; leave ~4k for the rules and series notes
        self.max_cues = max(30, min(self.max_output // 40, (num_ctx - 4000) // 70))
        self.client = client or httpx.Client(timeout=timeout)
        # Ollama's `think`: False, or a level for models that have one (gpt-oss); True for on/off-only models
        self.think = False if effort in ("", "off") else effort
        register(model, 0.0, 0.0)          # local: costs nothing, but the ledger still records usage

    def count_tokens(self, system: str, context: str, work: str) -> int:
        return int(len(system + context + work) / 3)

    def complete(self, system: str, context: str, work: str) -> ProviderResult:
        msgs = []
        if system.strip():
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": (context.strip() + "\n\n" if context.strip() else "") + work})
        body = {"model": self.model, "messages": msgs, "stream": False, "think": self.think,
                "options": {"num_ctx": self.num_ctx, "num_predict": self.max_output, "temperature": 0.3}}
        try:
            r = self.client.post(f"{self.url}/api/chat", json=body)
            if r.status_code == 400 and "think" in r.text and isinstance(body["think"], str):
                body["think"] = True                          # on/off-only thinking models: no levels
                r = self.client.post(f"{self.url}/api/chat", json=body)
            if r.status_code == 400 and "think" in r.text:      # models without a thinking switch
                body.pop("think")
                r = self.client.post(f"{self.url}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama not reachable at {self.url}: {e}") from e
        if r.status_code >= 400:
            raise ProviderError(f"Ollama {r.status_code}: {r.text[:300]}")
        data = r.json()
        text = (data.get("message") or {}).get("content") or ""
        usage = Usage(input_tokens=data.get("prompt_eval_count") or 0, output_tokens=data.get("eval_count") or 0)
        notes = []
        if data.get("done_reason") == "length":
            notes.append("hit num_predict: output truncated")
        if (data.get("prompt_eval_count") or 0) >= self.num_ctx - 16:
            notes.append(f"prompt filled the {self.num_ctx}-token context window; raise it or use a smaller chunk")
        return ProviderResult(text, usage, self.model, data.get("done_reason"), None, notes)
