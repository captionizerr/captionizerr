"""Side-by-side editing of a translated subtitle file against its source track.

Rows come from the source's dialogue cues, matched to translated cues by start
time (our outputs keep source timings). Translated cues with no source match
(e.g. a scene translated from another file) become 'extra' rows and are kept.
Saving writes SRT from the rows, so nothing unmatched is ever lost.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .cues import Cue, SubtitleDoc, _ts
from .profile import Profile
from .prompt import context_block, system_prompt
from .validate import plain

MATCH_MS = 600
# subtitle-site credits and adverts, which translations rightly drop
CREDITS = re.compile(r"www\.|\.(com|org|net|tv)\b|subtitles? by|synced|sync by|corrected by|ripped by|opensubtitles", re.I)   # translated cue start within this of a source start = same cue


@dataclass
class Row:
    id: int
    start_ms: int
    end_ms: int
    src: str             # source text ("" for extra rows)
    tr: str              # translation ("" = not translated / dropped)
    lead: str = ""       # positioning tags kept outside the editable text
    extra: bool = False  # no source cue matched
    ref: int | None = None   # index of the translated cue this row edits (None = new cue)
    hidden: bool = False     # not shown or edited (music-only, drawings); saved unchanged
    style: str = ""          # source cue's ASS style, for new cues

    @property
    def duration(self) -> float:
        return max(0.001, (self.end_ms - self.start_ms) / 1000)

    def flags(self, p: Profile) -> list[str]:
        """Same verdict as the pipeline's validator, for this row alone (no neighbour checks)."""
        return flags_for([self], p).get(self.id, [])


FLAG_LABEL = {"dropped": "empty", "leftover": "untranslated?", "label_left": "speaker label?",
              "italics_lost": "italics lost", "tag_lost": "position tag lost", "boundary": "line split?"}


def _geometry(text: str, duration: float, p: Profile) -> list[str]:
    from .validate import LINE_TOLERANCE, SHORT_CUE_SLACK
    body = plain(text)
    lines = body.split("\n")
    out = []
    if len(lines) > p.max_lines:
        out.append(f"{len(lines)} lines")
    longest = max(len(l) for l in lines)
    if longest > p.max_line + LINE_TOLERANCE:
        out.append(f"{longest} chars")
    slack = max(1.25, SHORT_CUE_SLACK) if duration < 2.0 else 1.25
    cps = len(body.replace("\n", "")) / duration
    if cps > p.target_cps * slack and len(body) > 12:
        out.append(f"{cps:.0f} cps")
    return out


def flags_for(rows: list["Row"], p: Profile) -> dict[int, list[str]]:
    """Per-row flags from the pipeline's own validator: the editor's "Flagged" list and a job's
    "needs review" can never disagree. Geometry labels are computed here; other kinds are named."""
    from .validate import check
    out: dict[int, list[str]] = {r.id: [] for r in rows}
    cues, tr = [], {}
    for r in rows:
        if r.hidden:
            continue
        if r.src:
            cues.append(Cue(r.id, r.start_ms, r.end_ms, r.src))
            tr[r.id] = r.tr
        elif r.tr.strip():
            out[r.id] = _geometry(r.tr, r.duration, p)         # translation-only rows: geometry only
    by_id = {r.id: r for r in rows}
    for i in check(cues, tr, p):
        if i.kind in ("missing",) or i.index not in out:
            continue
        r = by_id[i.index]
        if i.kind in ("too_long", "too_many_lines", "too_fast"):
            for g in _geometry(r.tr, r.duration, p):
                if g not in out[i.index]:
                    out[i.index].append(g)
        elif r.tr.strip() == "" and i.kind == "dropped" and CREDITS.search(r.src):
            continue
        else:
            label = FLAG_LABEL.get(i.kind, i.kind)
            if label not in out[i.index]:
                out[i.index].append(label)
    return out


def build_rows(source: SubtitleDoc | None, translated: SubtitleDoc) -> list[Row]:
    rows: list[Row] = []
    tr_cues = [c for c in translated.cues if not c.is_tag_only]
    used: set[int] = set()
    if source:
        import bisect
        starts = sorted((c.start_ms, i) for i, c in enumerate(tr_cues))
        keys = [s for s, _ in starts]
        for c in source.cues:
            if c.is_tag_only:
                continue
            j = bisect.bisect_left(keys, c.start_ms - MATCH_MS)
            match = None
            while j < len(keys) and keys[j] <= c.start_ms + MATCH_MS:
                i = starts[j][1]
                if i not in used:
                    match = i
                    break
                j += 1
            if match is not None:
                used.add(match)
                t = tr_cues[match]
                rows.append(Row(0, t.start_ms, t.end_ms, c.text, t.text, t.lead_tags, ref=t.index, style=t.style))
            else:
                rows.append(Row(0, c.start_ms, c.end_ms, c.text, "", c.lead_tags, style=c.style))
    for i, t in enumerate(tr_cues):
        if i not in used:
            rows.append(Row(0, t.start_ms, t.end_ms, "", t.text, t.lead_tags, extra=True, ref=t.index, style=t.style))
    for t in translated.cues:
        if t.is_tag_only:
            rows.append(Row(0, t.start_ms, t.end_ms, "", t.text, t.lead_tags, extra=True, ref=t.index, hidden=True,
                            style=t.style))
    rows.sort(key=lambda r: (r.start_ms, r.extra))
    for n, r in enumerate(rows, 1):
        r.id = n
    return rows


def save_text(translated: SubtitleDoc, rows: list[Row]) -> str:
    """The file content after an edit, in the translated file's own format."""
    if translated.fmt == "srt":
        return to_srt(rows)
    return to_ass(translated, rows)


def to_ass(translated: SubtitleDoc, rows: list[Row]) -> str:
    import copy
    import pysubs2
    from .cues import to_ass_text
    by_ref = {r.ref: r for r in rows if r.ref is not None}
    out = copy.deepcopy(translated.ssa)
    by_event = {id(c.event): c for c in translated.cues if c.event is not None}
    events = []
    for orig, ev in zip(translated.ssa.events, out.events):
        cue = by_event.get(id(orig))
        r = by_ref.get(cue.index) if cue else None
        if cue is None or cue.verbatim or r is None or r.hidden:
            events.append(ev)
            continue
        if r.tr.strip():
            ev.text = (r.lead or "") + to_ass_text(r.tr.strip())
            events.append(ev)
    for r in rows:                      # lines typed for source cues the translation lacked
        if r.ref is None and r.tr.strip():
            style = r.style if r.style in out.styles else next(iter(out.styles), "Default")
            events.append(pysubs2.SSAEvent(start=r.start_ms, end=r.end_ms, style=style,
                                           text=(r.lead or "") + to_ass_text(r.tr.strip())))
    out.events = sorted(events, key=lambda e: (e.start, e.layer))
    return out.to_string("ass")


def to_srt(rows: list[Row]) -> str:
    blocks, n = [], 0
    for r in rows:
        text = r.tr.strip()
        if not text:
            continue
        n += 1
        blocks.append(f"{n}\n{_ts(r.start_ms)} --> {_ts(r.end_ms)}\n{r.lead}{text}\n")
    return "\n".join(blocks)


def retranslate(provider, profile: Profile, series_context: str | None, rows: list[Row], row_id: int,
                instruction: str = "", window: int = 6) -> tuple[str, object]:
    """Ask for one new translation of one row, with neighbouring rows as context. Returns (text, usage)."""
    idx = next(i for i, r in enumerate(rows) if r.id == row_id)
    lo, hi = max(0, idx - window), min(len(rows), idx + window + 1)
    tgt = rows[idx]
    lines = ["Re-translate ONE cue of this episode. The surrounding cues (source → current translation) are context only.",
             "", "CONTEXT:"]
    for r in rows[lo:hi]:
        mark = ">>" if r.id == row_id else "  "
        lines.append(f"{mark} #{r.id} ({r.duration:.1f}s) {r.src.replace(chr(10), ' / ') or '(no source)'}  →  "
                     f"{r.tr.replace(chr(10), ' / ') or '(empty)'}")
    lines += ["", "TARGET:", f"{tgt.id}|{tgt.duration:.1f}|{tgt.src.replace(chr(10), ' / ') or tgt.tr.replace(chr(10), ' / ')}"]
    if instruction.strip():
        lines += ["", f"The viewer's instruction for this cue: {instruction.strip()}"]
    lines += ["", f"Return exactly one line: `{row_id}|translation` (use ` / ` for a line break)."]
    res = provider.complete(system_prompt(profile), context_block(series_context), "\n".join(lines))
    m = re.search(rf"^\s*#?{row_id}\s*\|(.*)$", res.text, re.M)
    text = (m.group(1) if m else res.text.strip().splitlines()[-1] if res.text.strip() else "").strip()
    text = re.sub(r"^\d+(?:\.\d+)?s?\s*\|", "", text).strip()
    return text.replace(" / ", "\n"), res.usage
