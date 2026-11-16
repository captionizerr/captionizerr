"""Prompt construction.

The system prompt holds only stable text (profile rules), so it caches across
every episode with the same profile. Series context comes next as its own cached
block; the episode's cues are the volatile part at the end.
"""
from __future__ import annotations

from .cues import Cue
from .profile import LANG_STYLE, SONG_RULES, TONE_RULES, Profile

NAME_RULES = {
    "tradition": "Use established translations of names, places and terms in the target language "
                 "(official dubbing, literary tradition, e.g. established Tolkien names). A glossary in the context overrides.",
    "transliterate": "Transliterate personal names; translate descriptive titles and nicknames.",
    "original": "Keep names in their original form.",
}


def system_prompt(p: Profile) -> str:
    sdh = ("Drop sound and music descriptions in brackets or parentheses, and drop speaker labels like 'ANNA:' "
           "(but use them to know who speaks). Drop TV-channel promos and adverts for other shows."
           if p.drop_sdh else
           "Translate sound descriptions too, keeping their brackets.")
    return f"""You are a professional subtitle translator working from {p.source_name} into {p.target_name}.
You translate a whole episode or film at once, so you know every scene, speaker and running joke before writing a line.

INPUT: one cue per line as `N|seconds|text`. N is the cue number, seconds is how long the cue is on screen,
and ` / ` marks a line break inside the cue.

OUTPUT: exactly one line per input cue, in the same order, as `N|translation`, using ` / ` for line breaks.
Every N must appear exactly once. To drop a cue, output `N|` with nothing after the bar.
Output nothing else: no commentary, no headings, no code fences.

RULES
- Meaning and voice first: keep jokes (adapt wordplay so it still lands), idioms, irony, character voice and register.
  Never explain, add or censor content.
- Cue boundaries are fixed: each output cue carries only the meaning of its own source cue, because it is shown exactly
  while that line is spoken. Never move words into a neighbouring cue and never leave a cue with dialogue empty to make
  room; a sentence that spans several cues stays split across the same cues (reorder words inside a cue if the grammar needs it).
- Casing follows the target language's own rules, not the source's: a cue that continues a sentence from the previous
  cue starts with a lowercase letter (unless the word is a name), and a new sentence starts with a capital.
- {TONE_RULES[p.tone]}
- {SONG_RULES[p.songs]}
- {sdh}
- Work out who is speaking to whom from the scene. Grammatical gender, formal/informal address and verb forms must match
  the actual speaker and addressee, and stay consistent for each pair of characters through the episode.
- {NAME_RULES[p.names]}
- Keep positioning tags such as {{\\an8}} at the very start of a cue, and keep <i>…</i> italics where the source has them.
- Readability: at most {p.max_line} characters per line and {p.max_lines} lines per cue; aim for at most
  {p.target_cps:.0f} characters per second of screen time. When the source is fast, condense wording rather than dropping meaning.
  Cues shorter than 2 seconds may run up to 1.5x that speed: never drop a meaningful word or phrase just to fit a short cue.
- {LANG_STYLE.get(p.target_lang, "Follow the target language's subtitle conventions.")}
"""


def context_block(series_context: str | None) -> str:
    if not series_context or not series_context.strip():
        return "SERIES CONTEXT: none provided. Infer names, relationships and tone from the episode itself."
    return "SERIES CONTEXT (bible, glossary, character notes; follow it):\n" + series_context.strip()


def cue_line(cue: Cue) -> str:
    return f"{cue.index}|{cue.duration:.1f}|{cue.text.replace(chr(10), ' / ')}"


def episode_block(cues: list[Cue], title: str | None = None) -> str:
    head = f"EPISODE: {title}\n" if title else ""
    return head + "CUES:\n" + "\n".join(cue_line(c) for c in cues)


def fix_block(items: list[tuple[Cue, str, list[str]]], neighbours: dict[int, tuple[Cue, str]]) -> str:
    """Ask for revised lines for flagged cues only. `items` = (cue, current translation, reasons)."""
    lines = ["Some cues of your translation need revision. Neighbouring cues are shown for context only.",
             "Return revised lines ONLY for the flagged cue numbers, as `N|translation`.", ""]
    for cue, current, reasons in items:
        for n in (cue.index - 1, cue.index + 1):
            if n in neighbours and not any(n == c.index for c, _, _ in items):
                nc, nt = neighbours[n]
                lines.append(f"context {nc.index}|{nc.text.replace(chr(10), ' / ')}  →  {nt.replace(chr(10), ' / ')}")
        lines.append(f"FLAGGED {cue.index}|{cue.duration:.1f}s|source: {cue.text.replace(chr(10), ' / ')}")
        lines.append(f"  current: {current.replace(chr(10), ' / ')}")
        lines.append(f"  problems: {'; '.join(reasons)}")
        lines.append("")
    return "\n".join(lines)
