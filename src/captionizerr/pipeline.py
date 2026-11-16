"""Translate one subtitle document: one request for the episode, then targeted retries.

1. Send every dialogue cue (tag-only cues are dropped locally, never sent).
2. Parse `N|text` lines; cues the model forgot are re-requested once.
3. Validate; flagged cues (too long, too fast, leftovers) get one fix pass,
   sent with their neighbours for context. Only the flagged lines change.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import copy

from .cues import Cue, SubtitleDoc, is_mostly_caps, sentence_case
from .pricing import Usage, cost
from .profile import Profile
from .prompt import context_block, episode_block, fix_block, system_prompt
from .providers import Provider
from .validate import Issue, casing_metrics, check

_LINE = re.compile(r"^\s*(?:FLAGGED\s+)?(\d+)\s*\|(.*)$")


def parse_response(text: str, valid: set[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        n = int(m.group(1))
        if n not in valid or n in out:
            continue
        body = m.group(2).strip()
        # tolerate an echoed duration column: "N|2.4|text"
        dm = re.match(r"^\d+(?:\.\d+)?\s*\|(.*)$", body)
        if dm:
            body = dm.group(1).strip()
        out[n] = body.replace(" / ", "\n")
    return out


@dataclass
class Step:
    name: str
    model: str
    usage: Usage
    seconds: float
    notes: list[str] = field(default_factory=list)
    batch: bool = False          # served by the Batch API (half price)


@dataclass
class Result:
    translations: dict[int, str]
    issues_initial: list[Issue]
    issues_final: list[Issue]
    steps: list[Step]
    confirmed_drops: set[int] = field(default_factory=set)
    casing: dict[str, int] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        return bool(self.issues_final)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for s in self.steps:
            total = total + s.usage
        return total

    def cost(self) -> float | None:
        vals = [cost(s.model, s.usage, s.batch) for s in self.steps]
        return None if any(v is None for v in vals) else sum(vals)


def cues_to_send(doc: SubtitleDoc) -> list[Cue]:
    send = [c for c in doc.cues if not c.is_tag_only]
    if is_mostly_caps(send):
        # model-facing copy only; timing and indices are unchanged
        send = [copy.copy(c) for c in send]
        for c in send:
            c.text = sentence_case(c.text)
    return send


def first_request(doc: SubtitleDoc, profile: Profile, series_context: str | None, title: str | None):
    """(system, context, work) of the main request; used to prepare a Batch request."""
    return system_prompt(profile), context_block(series_context), episode_block(cues_to_send(doc), title)


def translate(doc: SubtitleDoc, profile: Profile, provider: Provider,
              series_context: str | None = None, title: str | None = None,
              log=print, max_fix_rounds: int = 2, first=None, prefilled: dict[int, str] | None = None) -> Result:
    """`first`: a ProviderResult for the main request that already arrived via the Batch API;
    the pipeline then only runs the (small, live) missing-cue and fix requests.
    `prefilled`: translations reused from a previous release; only the other cues are sent,
    and reused lines are never rewritten by fix rounds (they may carry the viewer's edits)."""
    send = cues_to_send(doc)
    valid = {c.index for c in send}
    by_index = {c.index: c for c in doc.cues}
    system = system_prompt(profile)
    context = context_block(series_context)
    steps: list[Step] = []

    def call(name: str, work: str, allowed: set[int] | None = None) -> dict[int, str]:
        t0 = time.monotonic()
        res = provider.complete(system, context, work)
        steps.append(Step(name, res.model, res.usage, time.monotonic() - t0, res.notes))
        log(f"  {name}: {res.usage.input_tokens}+{res.usage.cache_read_tokens}cr+{res.usage.cache_write_tokens}cw in, "
            f"{res.usage.output_tokens} out, {time.monotonic() - t0:.0f}s {' '.join(res.notes)}")
        return parse_response(res.text, allowed or valid)

    chunk = getattr(provider, "max_cues", None)

    def translate_cues(cues: list[Cue], name: str, head: str = "", done: dict | None = None) -> dict[int, str]:
        """One request, or several chunks for providers with a small output limit; each chunk
        sees the previous chunk's last lines (source -> translation) as context."""
        if not chunk or len(cues) <= chunk:
            return call(name, head + episode_block(cues, title if not head else None))
        out: dict[int, str] = {}
        for k in range(0, len(cues), chunk):
            part = cues[k:k + chunk]
            prev = [c for c in cues[max(0, k - 8):k]]
            ctx = ""
            if prev:
                lines = [f"#{c.index} {c.text.replace(chr(10), ' / ')} → {(out.get(c.index) or (done or {}).get(c.index) or '').replace(chr(10), ' / ')}"
                         for c in prev]
                ctx = "PREVIOUS CUES (already translated; context only, do not output them):\n" + "\n".join(lines) + "\n\n"
            out.update(call(f"{name}{k // chunk + 1}", head + ctx + episode_block(part, title if k == 0 else None),
                            {c.index for c in part}))
        return out

    log(f"translating {len(send)} cues ({len(doc.cues) - len(send)} sound-only cues dropped locally)"
        + (f" in chunks of {chunk}" if chunk and len(send) > chunk else ""))
    protected: set[int] = set()
    if prefilled is not None:
        tr: dict[int, str | None] = {n: t for n, t in prefilled.items() if n in valid}
        protected = set(tr)
        log(f"  reusing {len(tr)} lines from the previous release")
    elif first is not None:
        steps.append(Step("translate (batch)", first.model, first.usage, 0.0, first.notes, batch=True))
        tr: dict[int, str | None] = dict(parse_response(first.text, valid))
    else:
        tr = dict(translate_cues(send, "translate"))

    missing = [c for c in send if c.index not in tr]
    if missing:
        log(f"  {len(missing)} cues missing; re-requesting them")
        tr.update(translate_cues(missing, "missing", "Translate these cues of the same episode:\n", tr))

    issues0 = check(doc.cues, tr, profile)
    confirmed: set[int] = set()
    current = issues0
    for rnd in range(1, max_fix_rounds + 1):
        fixable = [i for i in current if i.fixable and i.index not in protected]
        if not fixable:
            break
        reasons: dict[int, list[str]] = {}
        for i in fixable:
            reasons.setdefault(i.index, []).append(i.detail)
        items = [(by_index[n], tr.get(n) or "", r) for n, r in sorted(reasons.items())]
        neigh = {n: (by_index[n], tr[n]) for n in tr if tr[n] and n in by_index}
        log(f"  fix round {rnd}: {len(items)} flagged cues ({', '.join(sorted({i.kind for i in fixable}))})")
        fixed = call(f"fix{rnd}", fix_block(items, neigh))
        dropped_asked = {i.index for i in fixable if i.kind == "dropped"}
        for n in reasons:
            if n not in fixed:
                continue
            if fixed[n]:
                tr[n] = fixed[n]
            elif n in dropped_asked:
                confirmed.add(n)          # model re-confirmed: an advert or promo
        current = check(doc.cues, tr, profile, confirmed_drops=confirmed)
    issues1 = check(doc.cues, tr, profile, confirmed_drops=confirmed)
    clean = {n: t for n, t in tr.items() if t is not None}
    out_seq = [(c.index, clean[c.index]) for c in doc.cues if clean.get(c.index)]
    return Result(clean, issues0, issues1, steps, confirmed, casing_metrics(out_seq))
