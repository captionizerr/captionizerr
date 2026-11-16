"""Loading and saving subtitle cues (SRT and ASS) through pysubs2.

A Cue keeps the timing and the plain text the model sees. For ASS sources the
original event is kept so styling and leading override tags survive a round trip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pysubs2

# A cue that carries no dialogue: only bracketed/parenthesised sound
# descriptions, music notes or whitespace. These never reach the model.
_TAG_ONLY = re.compile(r"^(?:\s|\[[^\]]*\]|\([^)]*\)|♪|♫|<[^>]+>|\{[^}]*\})*$")
_ASS_LEAD = re.compile(r"^(\{[^}]*\})+")
_ASS_DRAWING = re.compile(r"\\p[1-9]")          # {\p1} vector drawing: shapes, not words
_ASS_KARAOKE = re.compile(r"\\[kK][fo]?\d")     # {\k20} syllable timing: romaji/kanji song lines


@dataclass
class Cue:
    index: int            # 1-based position in the source file
    start_ms: int
    end_ms: int
    text: str             # plain text, "\n" between lines; SRT <i> tags kept
    lead_tags: str = ""   # ASS override block(s) at the start, e.g. "{\\an8}"
    style: str = "Default"
    event: pysubs2.SSAEvent | None = field(default=None, repr=False)
    verbatim: bool = False  # ASS drawing / karaoke: never translated, copied to the output unchanged

    @property
    def duration(self) -> float:
        return max(0.001, (self.end_ms - self.start_ms) / 1000)

    @property
    def is_tag_only(self) -> bool:
        return self.verbatim or bool(_TAG_ONLY.fullmatch(self.text))


def is_mostly_caps(cues: list["Cue"], threshold: float = 0.8) -> bool:
    letters = [ch for c in cues for ch in c.text if ch.isalpha()]
    return bool(letters) and sum(ch.isupper() for ch in letters) / len(letters) >= threshold


def sentence_case(text: str) -> str:
    """ALL-CAPS closed captions cost ~1.5x the tokens of normal text; the model
    only needs readable input. Capitalise sentence starts and the pronoun I."""
    s = text.lower()
    s = re.sub(r"(^|[.!?…]\s+|^-\s*|\n-\s*|♪\s*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)
    return re.sub(r"\bi\b", "I", s)


@dataclass
class SubtitleDoc:
    path: Path
    fmt: str              # "srt" or "ass"
    cues: list[Cue]
    ssa: pysubs2.SSAFile = field(repr=False)


def _to_plain(event: pysubs2.SSAEvent, fmt: str) -> tuple[str, str]:
    # italics become <i>…</i> first, so they are never mistaken for a leading tag
    raw = event.text.replace("{\\i1}", "<i>").replace("{\\i0}", "</i>")
    lead = ""
    m = _ASS_LEAD.match(raw)
    if m:
        lead, raw = m.group(0), raw[m.end():]
    if fmt == "ass":
        raw = re.sub(r"\{[^}]*\}", "", raw)   # other inline overrides are not sent to the model
    text = raw.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ").strip()
    return text, lead


def load(path: str | Path) -> SubtitleDoc:
    path = Path(path)
    ssa = pysubs2.load(str(path), encoding="utf-8")
    fmt = "ass" if path.suffix.lower() in (".ass", ".ssa") else "srt"
    cues = []
    for i, ev in enumerate(e for e in ssa.events if not e.is_comment):
        text, lead = _to_plain(ev, fmt)
        verbatim = fmt == "ass" and bool(_ASS_DRAWING.search(ev.text) or _ASS_KARAOKE.search(ev.text))
        cues.append(Cue(index=i + 1, start_ms=ev.start, end_ms=ev.end, text=text,
                        lead_tags=lead, style=ev.style, event=ev, verbatim=verbatim))
    return SubtitleDoc(path=path, fmt=fmt, cues=cues, ssa=ssa)


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def render(doc: SubtitleDoc, translations: dict[int, str]) -> str:
    """Build the output file text. Cues missing from `translations` or mapped
    to "" are dropped (sound tags, promos). Timing and styles come from the source."""
    if doc.fmt == "srt":
        # written directly: pysubs2's SRT writer drops {\\an8}, which players honour
        blocks, n = [], 0
        for cue in doc.cues:
            text = translations.get(cue.index, "").strip()
            if not text:
                continue
            n += 1
            blocks.append(f"{n}\n{_ts(cue.start_ms)} --> {_ts(cue.end_ms)}\n{cue.lead_tags}{text}\n")
        return "\n".join(blocks)
    # ASS: start from a copy of the source so script info, styles and any extra sections
    # survive; dialogue events get new text, drawings/karaoke stay as they were, comments too.
    import copy as _copy
    out = _copy.deepcopy(doc.ssa)
    by_event = {id(c.event): c for c in doc.cues if c.event is not None}
    src_events = [e for e in doc.ssa.events]
    events = []
    for orig, ev in zip(src_events, out.events):
        cue = by_event.get(id(orig))
        if cue is None or cue.verbatim:          # comment line or drawing/karaoke
            events.append(ev)
            continue
        text = translations.get(cue.index, "").strip()
        if not text:
            continue
        ev.text = cue.lead_tags + to_ass_text(text)
        events.append(ev)
    out.events = events
    return out.to_string("ass")


def to_ass_text(text: str) -> str:
    return text.replace("\n", "\\N").replace("<i>", "{\\i1}").replace("</i>", "{\\i0}")
