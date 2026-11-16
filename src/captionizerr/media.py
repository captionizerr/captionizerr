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


LONG_CUE_MS = 60_000        # no real subtitle line stays on screen this long
MIN_TAIL_GAP_MS = 600_000   # 10 min without a single line at the end...
TAIL_GAP_SHARE = 0.25       # ...and over a quarter of the video: the track was cut short


def clock(ms: float) -> str:
    """50:41 or 1:01:16."""
    s = int(ms // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _dialogue(cues) -> list:
    return [c for c in cues if not c.is_tag_only and c.text.strip()]


def cut_off(cues, duration_s: float | None) -> int | None:
    """Where a full subtitle track stops early (ms), or None when it looks complete.

    Two shapes seen in real releases: the last line stretched to the end of the video
    (one cue lasting many minutes), and a track that simply stops long before the video does.
    Short tracks (forced, signs) are never judged."""
    dialog = _dialogue(cues)
    if len(dialog) < 20:
        return None
    last = dialog[-1]
    if last.end_ms - last.start_ms > LONG_CUE_MS:
        return last.start_ms
    if duration_s:
        tail = duration_s * 1000 - last.end_ms
        if tail > MIN_TAIL_GAP_MS and tail > TAIL_GAP_SHARE * duration_s * 1000:
            return last.end_ms
    return None


def last_line_ms(cues) -> int:
    dialog = _dialogue(cues)
    return dialog[-1].start_ms if dialog else 0


def clamp_long_cues(doc, max_ms: int = LONG_CUE_MS) -> int:
    """Shorten cues that stay on screen over a minute to a readable few seconds. Returns how many."""
    n = 0
    for c in doc.cues:
        if c.end_ms - c.start_ms > max_ms:
            c.end_ms = c.start_ms + min(7000, max(2000, 1000 + 60 * len(c.text)))
            if c.event is not None:
                c.event.end = c.end_ms
            n += 1
    return n


_NORM_DROP = re.compile(r"\[.*?\]|\(.*?\)|<.*?>|\{.*?\}")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w ]+", " ", _NORM_DROP.sub(" ", text.lower())).split())


def fit_timing(reference, other, min_pairs: int = 30, max_error_ms: int = 400) -> tuple[float, float] | None:
    """Map `other`'s times onto `reference`'s (t_ref = a * t_other + b) from the lines both share.

    A subtitle made for another cut or frame rate (25 fps PAL vs 23.976 WEB, an extra intro) is
    shifted and/or stretched; a straight-line fit through matching lines undoes both. Returns None
    when too few lines match or the fit is poor (different edit): such a file is not used."""
    def unique(cues):
        seen: dict[str, int | None] = {}
        for c in cues:
            k = _norm(c.text)
            if len(k) >= 12:
                seen[k] = None if k in seen else c.start_ms
        return {k: v for k, v in seen.items() if v is not None}
    ref, oth = unique(reference), unique(other)
    pts = [(oth[k], ref[k]) for k in oth.keys() & ref.keys()]
    if len(pts) < min_pairs:
        return None

    def lsq(ps):
        n = len(ps)
        mx = sum(x for x, _ in ps) / n
        my = sum(y for _, y in ps) / n
        vx = sum((x - mx) ** 2 for x, _ in ps)
        a = sum((x - mx) * (y - my) for x, y in ps) / vx if vx else 1.0
        return a, my - a * mx
    a, b = lsq(pts)
    for limit in (5000, 1500, 700):                       # drop lines that matched by accident, refit
        kept = [(x, y) for x, y in pts if abs(a * x + b - y) <= limit]
        if len(kept) < min_pairs:
            return None
        a, b = lsq(kept)
    errs = sorted(abs(a * x + b - y) for x, y in kept)
    if errs[len(errs) // 2] > max_error_ms or not 0.9 < a < 1.1:
        return None
    return a, b


def _retimed(path: Path, a: float, b: float, cache_dir: Path, lang: str) -> Path:
    """A copy of `path` with every time mapped through (a, b), kept in the cache (the original is never touched)."""
    import hashlib
    import pysubs2
    if abs(a - 1) < 1e-4 and abs(b) < 150:
        return path
    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}|{a:.6f}|{b:.0f}".encode()).hexdigest()[:20]
    out = cache_dir / f"{key}.{lang}.aligned{path.suffix.lower()}"
    if out.is_file():
        os.utime(out)
        return out
    subs = pysubs2.load(str(path), encoding="utf-8")
    for ev in subs.events:
        ev.start, ev.end = max(0, round(a * ev.start + b)), max(0, round(a * ev.end + b))
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    subs.save(str(tmp), format_="ass" if out.suffix == ".ass" else "srt")
    os.replace(tmp, out)
    return out


def _better_sidecar(embedded: Path, side: list[tuple[Path, str, bool]], lang: str, duration: float | None,
                    cache_dir: Path) -> Path | None:
    """The embedded track is cut short: a sidecar in the same language that is complete and reaches
    further wins (e.g. one Bazarr downloaded). It is first lined up with the embedded track on the
    lines both have, so a file timed for another frame rate or cut still lands on the right frames."""
    from . import cues as cuesmod
    try:
        emb = cuesmod.load(embedded).cues
    except Exception:
        return None
    cut = cut_off(emb, duration)
    if cut is None:
        return None
    for path, l, _sdh in side:
        if l != lang:
            continue
        try:
            sc = cuesmod.load(path).cues
        except Exception:
            continue
        fit = fit_timing(emb, sc)
        if fit is None:
            continue
        a, b = fit
        if cut_off(sc, duration) is None and a * last_line_ms(sc) + b > cut + 60_000:
            if embedded.suffix.lower() == ".srt" and path.suffix.lower() == ".srt":
                return _spliced(embedded, emb, path, sc, fit, cut, cache_dir, lang)
            return _retimed(path, a, b, cache_dir, lang)
    return None


# line the ending up on the last minutes before the cut (measured on Northwind S02: 5 min -> under 0.1 s,
# 15 min -> up to 1.7 s, because a 25 fps version's scene cuts differ); wider only when lines are few
SPLICE_WINDOWS = ((300_000, 10), (900_000, 15))


def _spliced(embedded: Path, emb, side_path: Path, side, fit: tuple[float, float], cut: int,
             cache_dir: Path, lang: str) -> Path:
    """The embedded track up to where it stops (exact timing), then the sidecar's remaining lines,
    lined up on the few minutes just before the cut (a sidecar from another cut can drift a second or
    two elsewhere, so a whole-file fit is only the fallback)."""
    import hashlib
    import pysubs2
    a, b = fit
    la, lb = fit
    for window, pairs in SPLICE_WINDOWS:          # narrowest that has enough shared lines
        near_ref = [c for c in emb if c.start_ms >= cut - window]
        near_side = [c for c in side if a * c.start_ms + b >= cut - window - 60_000]
        local = fit_timing(near_ref, near_side, min_pairs=pairs)
        if local:
            la, lb = local
            break
    st1, st2 = embedded.stat(), side_path.stat()
    key = hashlib.sha1(f"{embedded}|{st1.st_size}|{side_path}|{st2.st_size}|{int(st2.st_mtime)}|splice2"
                       .encode()).hexdigest()[:20]
    out = cache_dir / f"{key}.{lang}.spliced.srt"
    if out.is_file():
        os.utime(out)
        return out
    subs = pysubs2.SSAFile()
    for c in emb:
        if c.start_ms < cut and c.end_ms - c.start_ms <= LONG_CUE_MS:
            subs.append(pysubs2.SSAEvent(start=c.start_ms, end=c.end_ms, text=c.event.text if c.event else c.text))
    for c in side:
        s = round(la * c.start_ms + lb)
        if s >= cut - 500:
            subs.append(pysubs2.SSAEvent(start=s, end=round(la * c.end_ms + lb), text=c.event.text if c.event else c.text))
    subs.sort()
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    subs.save(str(tmp), format_="srt")
    os.replace(tmp, out)
    return out


def end_confirmed(video: Path, lang: str, cues, cut_ms: int, duration: float | None) -> bool:
    """A complete sidecar in the same language, lined up with the embedded track `cues`, ends about
    where that track does: the dialogue really ends there (the last line was stretched over the credits)."""
    from . import cues as cuesmod
    for path, l, _sdh in sidecars(video):
        if l != lang:
            continue
        try:
            sc = cuesmod.load(path).cues
        except Exception:
            continue
        fit = fit_timing(cues, sc)
        if fit and cut_off(sc, duration) is None and abs(fit[0] * last_line_ms(sc) + fit[1] - cut_ms) <= 60_000:
            return True
    return False


def find_source(video: Path, cache_dir: Path, prefer: list[str], target: str,
                any_language: bool = True) -> Source | None:
    """Pick the subtitle to translate FROM, never in the target language.

    Order: embedded text track in a preferred language (timing is exact; a complete sidecar in the
    same language wins over a track that stops early, see cut_off) -> sidecar in a
    preferred language (e.g. downloaded and synced by Bazarr) -> if `any_language`, any other
    embedded text track, then any other sidecar. Embedded tracks are extracted once into cache_dir.
    """
    prefer = [l for l in (langs.norm(x) for x in prefer) if l and l != langs.norm(target)]
    streams, duration = probe(video)
    side = [x for x in sidecars(video) if x[1] != langs.norm(target)]
    for lang in prefer:
        st = pick_source(streams, lang)
        if st:
            emb = _cache_extract(video, st, cache_dir, lang)
            better = _better_sidecar(emb, side, lang, duration, cache_dir)
            if better is None:
                return Source(emb, lang, "embedded")
            return Source(better, lang, "embedded + sidecar ending" if ".spliced." in better.name else "sidecar, re-timed")
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
