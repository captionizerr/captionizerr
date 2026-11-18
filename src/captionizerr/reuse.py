"""Re-timing instead of re-translating after an upgrade.

When Sonarr/Radarr replace a release (WEB-DL -> Blu-ray), the dialogue is almost always the
same; only timings shift, and a few lines may be split or merged differently. The previous
translation (our orphaned sidecar, often with the viewer's edits) is matched onto the new
source: by text against the stored copy of the old source when we have it, otherwise by
timing after estimating the global offset. Matched lines are reused as-is; only the rest is
sent to the model.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import shutil
import statistics
from dataclasses import dataclass
from pathlib import Path

from . import cues as cuesmod
from .writer import _SIDECAR

EPISODE = re.compile(r"[Ss](\d{1,3})[Ee](\d{1,3})")
MIN_COVERAGE = 0.85


def source_copy_path(sources_dir: Path, output: Path) -> Path:
    return Path(sources_dir) / hashlib.sha1(str(output).encode()).hexdigest()[:20]


def store_source(sources_dir: Path, output: Path, source: Path, lang: str = "en") -> None:
    """Keep the source a translation was made from (small text file), keyed by the output path:
    `<key>.<lang>.<srt|ass>` plus `<key>.path` naming the translation it belongs to."""
    base = source_copy_path(sources_dir, output)
    base.parent.mkdir(parents=True, exist_ok=True)
    for old in base.parent.glob(base.name + ".*.*"):
        old.unlink(missing_ok=True)
    shutil.copyfile(source, base.parent / f"{base.name}.{lang}{Path(source).suffix}")
    base.with_suffix(".path").write_text(str(output), encoding="utf-8")


def follow_rename(sources_dir: Path, old_output: Path, new_output: Path) -> None:
    """Move the stored source, its .path note and the skipped-line record to the renamed
    translation's key."""
    old, new = source_copy_path(sources_dir, old_output), source_copy_path(sources_dir, new_output)
    if not old.parent.is_dir():
        return
    for f in list(old.parent.glob(old.name + ".*")):
        f.rename(new.parent / (new.name + f.name[len(old.name):]))
    meta = new.with_suffix(".path")
    if meta.is_file():
        meta.write_text(str(new_output), encoding="utf-8")


def kept_path(kept_dir: Path, output: Path) -> Path:
    return Path(kept_dir) / (hashlib.sha1(str(output).encode()).hexdigest()[:20] + Path(output).suffix)


def keep(kept_dir: Path, output: Path) -> None:
    """A copy of our translation outside the library, keyed by its path. If the *arr deletes the
    file (an upgrade, an upgrade loop) and the same video comes back, it is re-aligned from here
    instead of paid for again. Never raises: losing the copy only costs a re-translation."""
    try:
        dst = kept_path(kept_dir, output)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(output, tmp)
        tmp.replace(dst)
    except OSError:
        pass


def kept(kept_dir: Path, output: Path) -> Path | None:
    p = kept_path(kept_dir, output)
    return p if p.is_file() else None


def follow_rename_kept(kept_dir: Path, old_output: Path, new_output: Path) -> None:
    old, new = kept_path(kept_dir, old_output), kept_path(kept_dir, new_output)
    if old.is_file():
        old.replace(new)


def store_drops(sources_dir: Path, output: Path, starts: list[int]) -> None:
    """Start times (ms) of source lines the translation leaves out on purpose (TV promos, adverts the
    model confirmed): the editor shows them as skipped instead of as errors. `<key>.drops`."""
    import json
    base = source_copy_path(sources_dir, output)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".drops").write_text(json.dumps(sorted(set(starts))), encoding="utf-8")


def stored_drops(sources_dir: Path, output: Path) -> set[int] | None:
    """None when nothing was recorded (translations made before 0.2.9)."""
    import json
    f = source_copy_path(sources_dir, output).with_suffix(".drops")
    try:
        return {int(x) for x in json.loads(f.read_text(encoding="utf-8"))}
    except (OSError, ValueError, TypeError):
        return None


def stored_source(sources_dir: Path, output: Path) -> tuple[Path, str] | None:
    """(file, language) of the stored source for this translation, if any."""
    base = source_copy_path(sources_dir, output)
    for p in sorted(base.parent.glob(base.name + ".*.*")):
        lang = p.name[len(base.name) + 1:].split(".")[0]
        if p.suffix in (".srt", ".ass"):
            return p, lang
    return None


def predecessor(video: Path, lang: str, video_ext: tuple) -> Path | None:
    """Our translation in the same folder whose video is gone and which is the same episode
    (same SxxEyy), or, for a film folder holding one video, the single orphan there."""
    folder = video.parent
    try:
        names = [p.name for p in folder.iterdir()]
    except OSError:
        return None
    stems = {Path(n).stem for n in names if n.lower().endswith(video_ext)}
    ep = EPISODE.search(video.stem)
    orphans = []
    for n in names:
        m = _SIDECAR.match(n)
        if not m or m.group("forced") or m.group("lang") != lang or m.group("stem") in stems:
            continue
        if ep:
            e2 = EPISODE.search(m.group("stem"))
            if not e2 or (int(e2.group(1)), int(e2.group(2))) != (int(ep.group(1)), int(ep.group(2))):
                continue
        orphans.append(folder / n)
    if not ep and len(stems) != 1:
        return None
    return orphans[0] if len(orphans) == 1 else None


def _norm(t: str) -> str:
    return re.sub(r"[^\w]+", " ", re.sub(r"<[^>]+>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)", " ", t)).strip().lower()


@dataclass
class Reuse:
    translations: dict[int, str]       # new cue index -> reused translation
    coverage: float
    method: str                        # "text" | "timing"


def align(new_src: cuesmod.SubtitleDoc, old_tr: cuesmod.SubtitleDoc,
          old_src: cuesmod.SubtitleDoc | None) -> Reuse:
    new = [c for c in new_src.cues if not c.is_tag_only]
    old_t = [c for c in old_tr.cues if not c.is_tag_only]
    if not new or not old_t:
        return Reuse({}, 0.0, "none")
    by_start = {c.start_ms: c for c in old_t}
    out: dict[int, str] = {}
    if old_src is not None:
        old_s = [c for c in old_src.cues if not c.is_tag_only]
        sm = difflib.SequenceMatcher(a=[_norm(c.text) for c in old_s], b=[_norm(c.text) for c in new], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                continue
            for a, b in zip(range(i1, i2), range(j1, j2)):
                t = by_start.get(old_s[a].start_ms)
                if t is not None:
                    out[new[b].index] = t.text          # positioning tags come from the new source cue
        return Reuse(out, len(out) / len(new), "text")
    # no stored source: match by time after removing the global offset between the releases
    starts = sorted(by_start)
    import bisect

    def nearest(ms):
        i = bisect.bisect_left(starts, ms)
        cands = [starts[k] for k in (i - 1, i) if 0 <= k < len(starts)]
        return min(cands, key=lambda s: abs(s - ms)) if cands else None

    deltas = [nearest(c.start_ms) - c.start_ms for c in new]
    deltas = [d for d in deltas if d is not None and abs(d) < 5000]
    if not deltas:
        return Reuse({}, 0.0, "timing")
    off = statistics.median(deltas)
    used = set()
    for c in new:
        s = nearest(c.start_ms + off)
        if s is not None and abs(s - (c.start_ms + off)) <= 300 and s not in used:
            used.add(s)
            t = by_start[s]
            out[c.index] = t.text
    return Reuse(out, len(out) / len(new), "timing")
