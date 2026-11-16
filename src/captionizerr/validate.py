"""Checks on a translated episode. Each issue names the cue and the reason.

Fixable issues go to one fix pass (flagged cues only, with neighbours as context).
Report-only metrics (casing) are counted but not auto-fixed, because a capital
after a comma can be a legitimate name.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from .cues import Cue
from .profile import LANG_SCRIPT, Profile

_TAGS = re.compile(r"\{[^}]*\}|<[^>]+>")
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
_CYR_WORD = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]{3,}")
_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_DIGITS = re.compile(r"\d+")
_SPEAKER = re.compile(r"^\s*[A-Za-z][A-Za-z .'-]{0,24}:\s*")
# a speaker label in any script at the start of a line: "Man #2:", "Мужчина:", "ANNA:"
_LABEL_ANY = re.compile(r"(^|\n)\s*-?\s*[^\W\d_][\w .'#-]{0,24}:\s", re.UNICODE)
# Latin tokens that legitimately appear in Cyrillic subtitles (Roman numerals, initialisms)
_ALLOWED_LATIN = re.compile(r"^(?:[IVXLCDM]+|OK|DNA|FBI|CIA|USA|TV|DJ|PhD|vs)$")

FIXABLE = {"label_left", "italics_lost", "leftover", "too_long", "too_many_lines", "too_fast", "tag_lost", "dropped", "boundary"}


@dataclass
class Issue:
    index: int
    kind: str      # missing | dropped | boundary | leftover | too_long | too_many_lines | too_fast | tag_lost | label_left | italics_lost
    detail: str

    @property
    def fixable(self) -> bool:
        return self.kind in FIXABLE


SHORT_CUE_SLACK = 1.6


def plain(text: str) -> str:
    return _TAGS.sub("", text)


def _letters(text: str) -> int:
    return len(re.sub(r"\W|\d|_", "", plain(text)))


def has_dialogue(cue: Cue) -> bool:
    """Real words left after removing sound tags and a leading speaker label."""
    body = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", plain(cue.text))
    body = _SPEAKER.sub("", body)
    return len(_WORD.findall(body)) >= 2


def check(cues: list[Cue], tr: dict[int, str | None], p: Profile,
          confirmed_drops: set[int] | frozenset = frozenset(), cps_slack: float = 1.25) -> list[Issue]:
    issues: list[Issue] = []
    script = LANG_SCRIPT.get(p.target_lang, "latin")
    dia = [c for c in cues if not c.is_tag_only]

    # per-cue checks
    for c in dia:
        t = tr.get(c.index)
        if t is None:
            issues.append(Issue(c.index, "missing", "no line returned for this cue"))
            continue
        if not t.strip():
            if has_dialogue(c) and c.index not in confirmed_drops:
                issues.append(Issue(c.index, "dropped",
                                    "left empty although the source has dialogue; translate it, or return it empty "
                                    "again only if it is an advert or promo for another programme"))
            continue
        body = plain(t)
        lines = body.split("\n")
        if len(lines) > p.max_lines:
            issues.append(Issue(c.index, "too_many_lines", f"{len(lines)} lines > {p.max_lines}"))
        longest = max(len(l) for l in lines)
        if longest > p.max_line + 4:   # small tolerance before forcing a rewrite
            issues.append(Issue(c.index, "too_long", f"line of {longest} chars > {p.max_line}"))
        cps = len(body.replace("\n", "")) / c.duration
        # short cues: a brief burst of speed beats losing words (the prompt allows 1.5x under 2 s)
        slack = max(cps_slack, SHORT_CUE_SLACK) if c.duration < 2.0 else cps_slack
        if cps > p.target_cps * slack and len(body) > 12:
            issues.append(Issue(c.index, "too_fast", f"{cps:.0f} chars/sec > {p.target_cps:.0f}"))
        if script == "cyrillic":
            bad = [w for w in _LATIN_WORD.findall(body) if not _ALLOWED_LATIN.match(w)]
            if bad and not _CYR_WORD.search(body):
                issues.append(Issue(c.index, "leftover", f"looks untranslated: {' '.join(bad[:4])}"))
        elif p.target_lang != p.source_lang and plain(c.text).strip().lower() == body.strip().lower() and len(body) > 15:
            issues.append(Issue(c.index, "leftover", "identical to the source"))
        if p.drop_sdh and _LABEL_ANY.search(plain(c.text)) and _LABEL_ANY.search(body) \
                and not re.search(r"\d:\d", body):
            issues.append(Issue(c.index, "label_left", "speaker label kept; drop it (it's a hearing-impaired tag)"))
        src_plain = c.text.strip()
        if src_plain.startswith("<i>") and src_plain.endswith("</i>") and "<i>" not in t:
            issues.append(Issue(c.index, "italics_lost", "the source line is in italics (<i>…</i>); keep them"))
        if c.text.lstrip().startswith("{\\an8}") and not (c.lead_tags or t.lstrip().startswith("{\\an8}")):
            issues.append(Issue(c.index, "tag_lost", "positioning tag {\\an8} missing"))

    issues += boundary_issues(dia, tr)
    return issues


def boundary_issues(dia: list[Cue], tr: dict[int, str | None]) -> list[Issue]:
    """Signs that text moved between neighbouring cues (it then shows before or after it is spoken)."""
    out: list[Issue] = []
    ratios: dict[int, float] = {}
    for c in dia:
        t = tr.get(c.index)
        if t and _letters(c.text) >= 8:
            ratios[c.index] = _letters(t) / _letters(c.text)
    if len(ratios) < 20:
        return out
    med = statistics.median(ratios.values())
    flagged: set[int] = set()

    def flag(a: Cue, b: Cue, why: str):
        for c in (a, b):
            if c.index not in flagged:
                flagged.add(c.index)
                out.append(Issue(c.index, "boundary",
                                 f"{why}; make cue {c.index} carry exactly the meaning of its own source cue "
                                 f"(text may have moved between cues {a.index} and {b.index})"))

    for a, b in zip(dia, dia[1:]):
        ta, tb = tr.get(a.index) or "", tr.get(b.index) or ""
        ra, rb = ratios.get(a.index), ratios.get(b.index)
        # 1. a long cue next to a gap: content merged into the neighbour
        if not tb.strip() and has_dialogue(b) and ra and ra > 1.6 * med:
            flag(a, b, f"cue {a.index} is unusually long while cue {b.index} is empty")
        elif not ta.strip() and has_dialogue(a) and rb and rb > 1.6 * med:
            flag(a, b, f"cue {b.index} is unusually long while cue {a.index} is empty")
        # 2. lopsided neighbours
        elif ra and rb and ((ra > 2.0 * med and rb < 0.45 * med) or (rb > 2.0 * med and ra < 0.45 * med)):
            flag(a, b, "lengths of neighbouring cues are lopsided compared with their sources")
        # 3. near-duplicate neighbours: the same content rendered twice (a shift went wrong)
        wa = {w.lower() for w in _WORD.findall(plain(ta)) if len(w) > 2}
        wb = {w.lower() for w in _WORD.findall(plain(tb)) if len(w) > 2}
        sa = {w.lower() for w in _WORD.findall(plain(a.text)) if len(w) > 2}
        sb = {w.lower() for w in _WORD.findall(plain(b.text)) if len(w) > 2}
        if len(wa) >= 3 and len(wb) >= 3:
            overlap = len(wa & wb) / min(len(wa), len(wb))
            src_overlap = len(sa & sb) / max(1, min(len(sa), len(sb)))
            if overlap >= 0.6 and src_overlap < 0.4:
                flag(a, b, f"cues {a.index} and {b.index} say nearly the same thing but their sources differ")
        # 4. a number from one cue's source shows up in the neighbour instead
        for x, y, tx, ty in ((a, b, ta, tb), (b, a, tb, ta)):
            for num in _DIGITS.findall(plain(x.text)):
                if num not in plain(tx) and num in plain(ty) and num not in plain(y.text):
                    flag(a, b, f"the number {num} from cue {x.index} appears in cue {y.index}")
    return out


def casing_metrics(cues_out: list[tuple[int, str]]) -> dict[str, int]:
    """Count casing slips between consecutive output cues (report-only)."""
    lower_after_stop = cap_after_comma = 0
    for (_, prev), (_, cur) in zip(cues_out, cues_out[1:]):
        p = plain(prev).rstrip()
        c = re.sub(r"^[—–-]\s*", "", plain(cur).lstrip())
        if not p or not c or not c[0].isalpha():
            continue
        ends_sentence = p.endswith((".", "!", "?", "»", "…")) and not p.endswith("...")
        if ends_sentence and c[0].islower():
            lower_after_stop += 1
        if p[-1] in ",—:" and c[0].isupper():
            cap_after_comma += 1
    return {"lower_after_sentence_end": lower_after_stop, "capital_after_comma": cap_after_comma}
