"""OpenAI-compatible adapter: OpenAI itself, OpenRouter, vLLM, LM Studio, Ollama (`/v1`), and others
that speak `POST {base_url}/chat/completions`.

- No batch here: backlog jobs run live with this provider.
- Smaller models get the episode in chunks (`max_cues` per request), each with the tail of
  the previous chunk's translation as context, so long episodes fit their output limit.
- Token counts for the pre-job estimate are approximate (characters / 3).
"""
from __future__ import annotations

import re

import httpx

from ..pricing import Usage
from .base import ProviderError, ProviderResult, RefusalError


class OpenAICompatProvider:
    name = "openai"
    supports_batch = False

    def __init__(self, base_url: str, model: str, api_key: str = "", max_output: int = 16000,
                 timeout: float = 900, client: httpx.Client | None = None):
        if not base_url or not model:
            raise ProviderError("OpenAI-compatible provider needs a base URL and a model name")
        self.base_url, self.model, self.key = base_url.rstrip("/"), model, api_key
        self.max_output = max_output
        # ~40 output tokens per translated cue incl. numbering; keep a margin for long lines
        self.max_cues = max(40, min(800, max_output // 40))
        self.client = client or httpx.Client(timeout=timeout)
        # OpenAI's newer models only accept max_completion_tokens (and some no custom temperature);
        # most other OpenAI-compatible servers still expect max_tokens. Start with the likely one,
        # adapt once if the server says otherwise, and remember for later requests.
        self.token_param = "max_completion_tokens" if "api.openai.com" in self.base_url else "max_tokens"
        self.send_temperature = True

    def count_tokens(self, system: str, context: str, work: str) -> int:
        return int(len(system + context + work) / 3)

    def complete(self, system: str, context: str, work: str) -> ProviderResult:
        msgs = []
        if system.strip():
            msgs.append({"role": "system", "content": system})
        user = (context.strip() + "\n\n" if context.strip() else "") + work
        msgs.append({"role": "user", "content": user})
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        tried: set[str] = set()
        for _ in range(4):
            body = {"model": self.model, "messages": msgs, self.token_param: self.max_output}
            if self.send_temperature:
                body["temperature"] = 0.3
            try:
                r = self.client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
            except httpx.HTTPError as e:
                raise ProviderError(f"connection error: {e}") from e
            if r.status_code != 400:
                break
            msg = _error_message(r).lower()
            if re.search(rf"\b{self.token_param}\b", msg) and self.token_param not in tried:
                tried.add(self.token_param)            # the server named the parameter we sent: use the other one
                self.token_param = "max_tokens" if self.token_param == "max_completion_tokens" else "max_completion_tokens"
            elif "temperature" in msg and self.send_temperature:
                self.send_temperature = False
            else:
                break
        if r.status_code >= 400:
            raise ProviderError(f"{r.status_code}: {_error_message(r)[:300]}")
        data = r.json()
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected response: {str(data)[:300]}") from e
        if choice.get("finish_reason") == "content_filter":
            raise RefusalError("provider content filter")
        u = data.get("usage") or {}
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        usage = Usage(input_tokens=max(0, (u.get("prompt_tokens") or 0) - cached),
                      output_tokens=u.get("completion_tokens") or 0, cache_read_tokens=cached)
        notes = ["hit max_tokens: output truncated"] if choice.get("finish_reason") == "length" else []
        return ProviderResult(text, usage, data.get("model") or self.model, choice.get("finish_reason"), data.get("id"), notes)


def _error_message(r: httpx.Response) -> str:
    """The human part of an API error ({"error": {"message": ...}}), else the raw body."""
    try:
        err = r.json().get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    except ValueError:
        pass
    return r.text
