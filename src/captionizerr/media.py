"""Reading subtitle streams out of video files with ffprobe/ffmpeg.

Library files are only ever ffmpeg INPUTS. Outputs go to a private temp
directory, ffmpeg runs with -n (never overwrite), and paths are passed as
argv items, never through a shell (lesson from the 2026-11-14 incident).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import langs

TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


@dataclass
class SubStream:
    index: int
    codec: str
    language: str | None
    title: str | None
    forced: bool
    default: bool

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_CODECS

    @property
    def looks_sdh(self) -> bool:
        t = (self.title or "").lower()
        return "sdh" in t or "cc" == t.strip() or "hearing" in t


def probe(video: Path) -> tuple[list[SubStream], float | None]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(video)],
        check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    subs = []
    for s in data.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        tags, disp = s.get("tags", {}), s.get("disposition", {})
        subs.append(SubStream(s["index"], s.get("codec_name", ""), tags.get("language"), tags.get("title"),
                              bool(disp.get("forced")), bool(disp.get("default"))))
    dur = data.get("format", {}).get("duration")
    return subs, float(dur) if dur else None


def _track_score(s: SubStream) -> tuple:
    """Lower sorts first: full dialogue before signs/songs-only, plain before SDH, default first."""
    t = (s.title or "").lower()
    signs = any(k in t for k in ("sign", "song", "forced", "s&s", "karaoke", "commentary"))
    return (signs, s.looks_sdh, not s.default)


def pick_source(streams: list[SubStream], lang: str = "en") -> SubStream | None:
    """A full, non-forced text track in `lang` (any ISO spelling)."""
    cands = [s for s in streams if s.is_text and not s.forced and langs.same(s.language, lang)]
    cands.sort(key=_track_score)
    return cands[0] if cands else None


@dataclass
class Source:
    path: Path              # a subtitle file to read (cache copy or an existing sidecar; never written)
    lang: str               # ISO 639-1
    origin: str             # "embedded" | "sidecar" | "bazarr"


_SIDE = re.compile(r"^\.(?P<lang>[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?)(?P<flags>(?:\.(?:hi|sdh|cc|default))*)\.(?P<ext>srt|ass|ssa)$", re.I)


def sidecars(video: Path) -> list[tuple[Path, str, bool]]:
    """Existing external subtitles next to the video: (path, lang, is_sdh). Ours and forced ones are skipped."""
    out = []
    try:
        names = os.listdir(video.parent)
    except OSError:
        return out
    stem = video.stem
    for n in names:
        if not n.startswith(stem + ".") or ".captionizerr." in n or ".forced." in n.lower():
            continue
        m = _SIDE.match(n[len(stem):])
        if m and langs.norm(m.group("lang")):
            out.append((video.parent / n, langs.norm(m.group("lang")), bool(m.group("flags"))))
    return sorted(out, key=lambda x: (x[2], x[0].suffix != ".srt", x[0].name))


def find_source(video: Path, cache_dir: Path, prefer: list[str], target: str,
                any_language: bool = True) -> Source | None:
    """Pick the subtitle to translate FROM, never in the target language.

    Order: embedded text track in a preferred language (timing is exact) -> sidecar in a
    preferred language (e.g. downloaded and synced by Bazarr) -> if `any_language`, any other
    embedded text track, then any other sidecar. Embedded tracks are extracted once into cache_dir.
    """
    prefer = [l for l in (langs.norm(x) for x in prefer) if l and l != langs.norm(target)]
    streams, _ = probe(video)
    side = [x for x in sidecars(video) if x[1] != langs.norm(target)]
    for lang in prefer:
        st = pick_source(streams, lang)
        if st:
            return Source(_cache_extract(video, st, cache_dir, lang), lang, "embedded")
        for path, l, _sdh in side:
            if l == lang:
                return Source(path, lang, "sidecar")
    if any_language:
        others = [s for s in streams if s.is_text and not s.forced and langs.norm(s.language)
                  and langs.norm(s.language) != langs.norm(target)]
        others.sort(key=lambda s: (not s.default,) + _track_score(s))
        if others:
            lang = langs.norm(others[0].language)
            return Source(_cache_extract(video, others[0], cache_dir, lang), lang, "embedded")
        if side:
            return Source(side[0][0], side[0][1], "sidecar")
    return None


def find_forced(video: Path, cache_dir: Path, prefer: list[str], target: str) -> Source | None:
    """A forced track in a source language: only the foreign-language lines and signs (e.g. Elvish
    dialogue in an English show). Embedded forced text track first, then `<video>.<lang>.forced.srt`."""
    prefer = [l for l in (langs.norm(x) for x in prefer) if l and l != langs.norm(target)]
    streams, _ = probe(video)
    def is_forced(x):
        return x.forced or "forced" in (x.title or "").lower()
    if any(is_forced(x) and langs.same(x.language, target) for x in streams):
        return None                     # the release already ships a forced track in the target language
    for lang in prefer:
        cands = [x for x in streams if x.is_text and x.forced and langs.same(x.language, lang)]
        if not cands:   # some releases only say so in the title
            cands = [x for x in streams if x.is_text and langs.same(x.language, lang) and "forced" in (x.title or "").lower()]
        if cands:
            return Source(_cache_extract(video, cands[0], cache_dir, lang), lang, "embedded")
        for ext in ("srt", "ass"):
            for code in (lang, *langs.ISO3.get(lang, ())):
                side = video.with_name(f"{video.stem}.{code}.forced.{ext}")
                if side.is_file():
                    return Source(side, lang, "sidecar")
    return None


def _cache_extract(video: Path, stream: SubStream, cache_dir: Path, lang: str) -> Path:
    import hashlib
    import shutil
    st = video.stat()
    key = hashlib.sha1(f"{video}|{st.st_size}|{int(st.st_mtime)}|{stream.index}".encode()).hexdigest()[:20]
    cache_dir.mkdir(parents=True, exist_ok=True)
    for ext in ("srt", "ass"):
        hit = cache_dir / f"{key}.{lang}.{ext}"
        if hit.is_file():
            os.utime(hit)
            return hit
    work = Path(tempfile.mkdtemp(prefix="src-", dir=str(cache_dir)))
    try:
        out = extract(video, stream, work)
        dest = cache_dir / f"{key}.{lang}.{out.suffix.lstrip('.')}"
        shutil.move(str(out), dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def extract(video: Path, stream: SubStream, workdir: Path | None = None) -> Path:
    workdir = Path(workdir or tempfile.mkdtemp(prefix="captionizerr-"))
    ext = "ass" if stream.codec in ("ass", "ssa") else "srt"
    out = workdir / f"stream{stream.index}.{ext}"
    if out.exists():
        raise FileExistsError(out)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-n", "-i", str(video),
                    "-map", f"0:{stream.index}", "-c:s", "ass" if ext == "ass" else "srt", str(out)],
                   check=True, stdin=subprocess.DEVNULL)
    return out


def cached_source(video: Path, cache_dir: Path, lang: str = "en") -> Path | None:
    """Back-compat helper: the source track in exactly `lang` (embedded or sidecar)."""
    src = find_source(video, cache_dir, [lang], target="__none__", any_language=False)
    return src.path if src else None


def prune_cache(cache_dir: Path, days: int = 60) -> int:
    """Drop cached source tracks not used for `days` (mtime is touched on every hit)."""
    import time
    if not cache_dir.is_dir():
        return 0
    cutoff, n = time.time() - days * 86400, 0
    for f in cache_dir.glob("*.*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            n += 1
    return n
