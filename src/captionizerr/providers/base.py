from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..pricing import Usage


class ProviderError(RuntimeError):
    """A request failed in a way the pipeline should report (not retry blindly)."""


class RefusalError(ProviderError):
    """The model (and any fallback) declined the request."""


@dataclass
class ProviderResult:
    text: str
    usage: Usage
    model: str                 # model that actually served the answer
    stop_reason: str | None
    request_id: str | None = None
    notes: list[str] = field(default_factory=list)


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, context: str, work: str) -> ProviderResult:
        """`system` and `context` are stable and may be cached; `work` is the per-request part."""
        ...
