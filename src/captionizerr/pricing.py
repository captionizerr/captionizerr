"""List prices (USD per 1M tokens) and cost accounting from API usage.

Prices: Anthropic API list prices as of 2026-11. Cache writes (5-minute TTL)
cost 1.25x input; cache reads use the listed rate. Batch API halves everything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# model: (input, output, cache_read)
PRICES = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, o: "Usage") -> "Usage":
        return Usage(self.input_tokens + o.input_tokens, self.output_tokens + o.output_tokens,
                     self.cache_write_tokens + o.cache_write_tokens, self.cache_read_tokens + o.cache_read_tokens)


# user-priced models (OpenAI-compatible endpoints, local models at 0)
CUSTOM_PRICES: dict[str, tuple[float, float, float]] = {}


def register(model: str, p_in: float, p_out: float) -> None:
    CUSTOM_PRICES[model] = (p_in, p_out, p_in * 0.5)


def price(model: str) -> tuple[float, float, float] | None:
    """Exact name first; then the same model without a dated snapshot suffix
    (gpt-x-2026-08-06, claude-y-20260806), so a provider reporting its snapshot still gets priced."""
    known = {**PRICES, **CUSTOM_PRICES}
    if model in known:
        return known[model]
    base = re.sub(r"-(\d{4}-\d{2}-\d{2}|\d{8})$", "", model or "")
    if base in known:
        return known[base]
    prefix = max((k for k in known if (model or "").startswith(k + "-")), key=len, default=None)
    return known[prefix] if prefix else None


def cost(model: str, u: Usage, batch: bool = False) -> float | None:
    p = price(model)
    if p is None:
        return None
    inp, out, cread = p
    total = (u.input_tokens * inp + u.cache_write_tokens * inp * 1.25
             + u.cache_read_tokens * cread + u.output_tokens * out) / 1_000_000
    return total / 2 if batch else total
