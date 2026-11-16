"""Anthropic adapter (reference implementation).

- Streams (one episode is ~10k output tokens plus thinking).
- Caches the system prompt and series context; the episode's cues are uncached.
- Opts into server-side refusal fallback ("default" routing), so a rare
  safety-classifier false positive on crime-drama dialogue is re-run on
  Anthropic's recommended fallback model instead of failing the job.
"""
from __future__ import annotations

import anthropic

from ..pricing import Usage
from .base import ProviderError, ProviderResult, RefusalError

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"
    supports_batch = True
    max_cues = None          # whole episode in one request

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low",
                 max_tokens: int = 64000, client: anthropic.Anthropic | None = None,
                 fallbacks: bool = True, api_key: str | None = None):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        if client is None:
            from ..config import read_secret
            key = api_key or read_secret("ANTHROPIC_API_KEY") or None   # also ANTHROPIC_API_KEY_FILE
            client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.client = client

    def count_tokens(self, system: str, context: str, work: str) -> int:
        """Free endpoint; used for the pre-job cost estimate."""
        content = [{"type": "text", "text": t} for t in (context, work) if t]
        kw = dict(model=self.model, messages=[{"role": "user", "content": content}])
        if system:
            kw["system"] = system
        return self.client.messages.count_tokens(**kw).input_tokens

    @staticmethod
    def content(context: str, work: str) -> list[dict]:
        """The cache marker goes on the context block; an empty block can't carry one (API 400)."""
        out = []
        if context.strip():
            out.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
        out.append({"type": "text", "text": work})
        return out

    def complete(self, system: str, context: str, work: str) -> ProviderResult:
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": self.content(context, work)}],
        )
        if system.strip():
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if self.fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            with self.client.beta.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        except anthropic.APIStatusError as e:
            raise ProviderError(f"{e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"connection error: {e}") from e

        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        )
        notes = []
        if any(getattr(b, "type", None) == "fallback" for b in msg.content):
            notes.append(f"served by fallback model {msg.model}")
        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise RefusalError(f"model declined (category: {cat})")
        if msg.stop_reason == "max_tokens":
            notes.append("hit max_tokens: output truncated")
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return ProviderResult(text=text, usage=usage, model=msg.model, stop_reason=msg.stop_reason,
                              request_id=getattr(msg, "_request_id", None), notes=notes)

    # ------------------------------------------------------------ Message Batches (backlog, -50%)
    def batch_params(self, system: str, context: str, work: str) -> dict:
        """Same request as complete(), minus streaming and fallbacks (not allowed in a batch)."""
        params = dict(model=self.model, max_tokens=self.max_tokens, output_config={"effort": self.effort},
                      messages=[{"role": "user", "content": self.content(context, work)}])
        if system.strip():
            params["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        return params

    def submit_batch(self, items: list[tuple[str, dict]]) -> str:
        try:
            b = self.client.messages.batches.create(
                requests=[{"custom_id": cid, "params": params} for cid, params in items])
        except anthropic.APIStatusError as e:
            raise ProviderError(f"{e.status_code}: {e.message}") from e
        return b.id

    def batch_status(self, batch_id: str) -> str:
        """'in_progress' | 'canceling' | 'ended'."""
        return self.client.messages.batches.retrieve(batch_id).processing_status

    def batch_results(self, batch_id: str) -> dict[str, "ProviderResult | Exception"]:
        out: dict = {}
        for r in self.client.messages.batches.results(batch_id):
            res = r.result
            if res.type != "succeeded":
                detail = getattr(getattr(res, "error", None), "type", "") or res.type
                out[r.custom_id] = ProviderError(f"batch request {res.type}: {detail}")
                continue
            msg = res.message
            if msg.stop_reason == "refusal":
                out[r.custom_id] = RefusalError("model declined in batch (no fallback there)")
                continue
            u = msg.usage
            usage = Usage(u.input_tokens or 0, u.output_tokens or 0,
                          getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0)
            notes = ["hit max_tokens: output truncated"] if msg.stop_reason == "max_tokens" else []
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            out[r.custom_id] = ProviderResult(text, usage, msg.model, msg.stop_reason, None, notes)
        return out
