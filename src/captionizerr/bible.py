"""Series notes ('bible'): generated once per series from the show's overview and one
episode's source dialogue, then edited by hand. They ride in the cached context block
of every later episode, so names, genders and formality stay consistent."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .cues import Cue
from .profile import LANG_NAMES

SYSTEM = """You prepare translator's notes for a TV series or film, used as standing context by the
subtitle translator for every episode. Write concise Markdown, no preamble, with these sections:

# <title>
One-paragraph synopsis (setting, period, genre, register of the dialogue).

## Characters
One bullet per recurring character: `Name` → rendering in the target language, gender, role,
and who they address formally or informally (for languages with a T–V distinction).

## Glossary
Places, organisations, invented terms, recurring phrases and catch-phrases, each with ONE
fixed rendering in the target language. Prefer an established official translation if the
work has one; otherwise transliterate names by the target language's usual conventions.

## Tone
How the show speaks: slang, swearing level, dialect, humour, anything a translator must keep.

Only state what the material supports; mark guesses with "(?)". Keep it under 450 words."""


def notes_prompt(title: str, overview: str, lang: str, cues: list[Cue], max_chars: int = 24000) -> str:
    dialogue, n = [], 0
    for c in cues:
        if c.is_tag_only:
            continue
        line = c.text.replace("\n", " ")
        n += len(line) + 1
        if n > max_chars:
            break
        dialogue.append(line)
    return (f"TITLE: {title}\nTARGET LANGUAGE: {LANG_NAMES.get(lang, lang)} ({lang})\n"
            f"OVERVIEW: {overview.strip() or '(none)'}\n\nDIALOGUE FROM ONE EPISODE (source language):\n"
            + "\n".join(dialogue))


def generate_notes(provider, title: str, overview: str, lang: str, cues: list[Cue]):
    """Returns (markdown, ProviderResult)."""
    res = provider.complete(SYSTEM, "", notes_prompt(title, overview, lang, cues))
    text = res.text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("markdown").strip()
    return text, res


# ------------------------------------------------------------------ feedback from edits
def log_corrections(feedback_dir: Path, series_key: str, items: list[dict]) -> None:
    """Append hand corrections (source, before, after) for later 'learn from corrections'."""
    if not items or not series_key:
        return
    feedback_dir.mkdir(parents=True, exist_ok=True)
    with (feedback_dir / f"{series_key}.jsonl").open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps({**it, "t": int(time.time())}, ensure_ascii=False) + "\n")


def corrections(feedback_dir: Path, series_key: str, limit: int = 200) -> list[dict]:
    f = feedback_dir / f"{series_key}.jsonl"
    if not f.is_file():
        return []
    rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-limit:]


LEARN_SYSTEM = """You maintain translator's notes for a series. The viewer corrected some machine
translations by hand. Infer the durable preferences behind the corrections (name renderings,
terms, formality between characters, register) and return the COMPLETE updated notes in
Markdown: keep everything still valid, add or change entries the corrections support, and
ignore one-off rewordings that teach nothing general. No preamble."""


def learn(provider, notes: str, items: list[dict]):
    lines = [f"- source: {it.get('src', '')!r}\n  machine: {it.get('before', '')!r}\n  viewer: {it.get('after', '')!r}"
             for it in items]
    work = f"CURRENT NOTES:\n{notes.strip() or '(none yet)'}\n\nCORRECTIONS:\n" + "\n".join(lines)
    res = provider.complete(LEARN_SYSTEM, "", work)
    return res.text.strip(), res
