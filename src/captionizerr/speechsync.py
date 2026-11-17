"""Sync subtitles to the speech in the audio, through a Whisper speech-to-text service.

The service speaks the whisper-asr-webservice API (`POST /asr`, word timestamps); `deploy/whisper/`
has a small one built on faster-whisper. Whisper writes down the spoken words with their times;
each subtitle line's words are found in that transcript, which tells where the line is really said.

The shifts that come out are smoothed (a line matched to the wrong place is an outlier, not a
reason to move its neighbours) and keep the track's own habit of how early a line appears before
the speech, so a well-timed track is left alone and only the parts that are off move.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import subprocess
import urllib.request
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger("captionizerr.speechsync")

CHUNK_MS = 300_000          # audio per request
MIN_ANCHORED = 0.35         # share of lines that must be found in the transcript to trust the result
MIN_ANCHORS = 8
OUTLIER_MS = 1500           # an anchor this far from its neighbours' shift is a mismatch
NEIGHBOURS = 4              # anchors on each side used for the local shift
LEAD_RANGE = (-600, 300)    # a sane "line appears before speech" habit (ms); outside it the track is just off
MOVE_MIN_MS = 500           # smaller corrections are within Whisper's own timing jitter and not noticeable


class SpeechError(RuntimeError):
    pass


@dataclass
class Word:
    start_ms: int
    end_ms: int
    token: str


# ------------------------------------------------------------------ the service
def _post(url: str, wav: bytes, lang: str, timeout: float, vad: bool = True) -> dict:
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"a.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{b}--\r\n".encode()
    q = f"language={lang}&" if lang else ""
    req = urllib.request.Request(f"{url.rstrip('/')}/asr?{q}output=json&word_timestamps=true&vad_filter={'true' if vad else 'false'}&encode=true",
                                 data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        raise SpeechError(f"Whisper at {url}: {e}") from e


def health(url: str, timeout: float = 10) -> str:
    """The model name the service reports; raises SpeechError when it can't be reached."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            return str(json.loads(r.read()).get("model") or "ok")
    except Exception as e:
        raise SpeechError(f"Whisper at {url}: {e}") from e


def audio_stream(video: Path, lang: str) -> tuple[int, int] | None:
    """(index among the audio streams, channels) of the track in `lang`, else the first one."""
    from . import langs
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=channels:stream_tags=language", "-of", "json", str(video)],
                         capture_output=True, text=True)
    try:
        streams = json.loads(out.stdout).get("streams", [])
    except ValueError:
        return None
    if not streams:
        return None
    pick = next((i for i, s in enumerate(streams) if langs.same((s.get("tags") or {}).get("language"), lang)), 0)
    return pick, int(streams[pick].get("channels") or 2)


def _pan(ch: int) -> str:
    return "pan=mono|c0=FC" if ch >= 3 else "pan=mono|c0=0.5*c0+0.5*c1" if ch == 2 else "pan=mono|c0=c0"


def _pieces(url: str, video: Path, idx: int, ch: int, lang: str, start_ms: int, end_ms: int, timeout: float,
            vad: bool = True):
    """(offset ms, Whisper JSON) for each CHUNK_MS piece of one audio stream."""
    t = max(0, start_ms)
    while t < end_ms:
        n = min(CHUNK_MS, end_ms - t)
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t / 1000:.3f}", "-t", f"{n / 1000:.3f}",
                            "-i", str(video), "-map", f"0:a:{idx}", "-af", _pan(ch), "-ar", "16000", "-ac", "1",
                            "-f", "wav", "-"], capture_output=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            raise SpeechError(f"can't read the audio: {r.stderr.decode(errors='replace').strip()[-160:]}")
        if len(r.stdout) > 32_000:
            yield t, _post(url, r.stdout, lang, timeout, vad)
        t += n


def transcribe(url: str, video: Path, lang: str, start_ms: int, end_ms: int, timeout: float = 900) -> list[Word]:
    """Spoken words from `start_ms` to `end_ms` of the `lang` audio track, with absolute times
    (the centre channel of a surround track: that's where the dialogue is)."""
    st = audio_stream(video, lang)
    if st is None:
        raise SpeechError("the video has no audio track")
    words: list[Word] = []
    for t, d in _pieces(url, video, st[0], st[1], lang, start_ms, end_ms, timeout):
        for seg in d.get("segments", []):
            for w in seg.get("words") or []:
                for tok in _tokens(w.get("word", "")):
                    words.append(Word(t + round(w["start"] * 1000), t + round(w["end"] * 1000), tok))
    return words


# ------------------------------------------------------------------ a subtitle made from the audio
@dataclass
class Spoken:
    start_ms: int
    end_ms: int
    text: str
    prob: float = 1.0


def transcript(url: str, video: Path, prefer: list[str], duration_s: float, timeout: float = 900,
               on_progress=None) -> tuple[list[Spoken], str]:
    """Every spoken word of the video with its time, and the language heard. The audio track in
    the first preferred source language is used; with none, the first track, language detected."""
    from . import langs as L
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=channels:stream_tags=language", "-of", "json", str(video)],
                         capture_output=True, text=True)
    try:
        streams = json.loads(out.stdout).get("streams", [])
    except ValueError:
        streams = []
    if not streams:
        raise SpeechError("the video has no audio track" if out.returncode == 0 else
                          f"can't read the audio: {out.stderr.strip()[-160:]}")
    pick, hint = 0, ""
    for lang in prefer:
        i = next((i for i, s in enumerate(streams) if L.same((s.get("tags") or {}).get("language"), lang)), None)
        if i is not None:
            pick, hint = i, L.norm(lang) or ""
            break
    ch = int(streams[pick].get("channels") or 2)
    words: list[Spoken] = []
    heard: dict[str, int] = {}
    end = int(duration_s * 1000)
    # no voice-activity filter: it drops quiet lines (measured: "You gotta help me out. I've done something
    # bad." vanished with it); words_to_cues removes what Whisper invents over silence instead
    for t, d in _pieces(url, video, pick, ch, hint, 0, end, timeout, vad=False):
        lang = L.norm(d.get("language") or "") or ""
        if lang:
            heard[lang] = heard.get(lang, 0) + 1
        for seg in d.get("segments", []):
            for w in seg.get("words") or []:
                text = (w.get("word") or "").strip()
                if text:
                    words.append(Spoken(t + round(w["start"] * 1000), t + round(w["end"] * 1000), text,
                                        float(w.get("probability", 1.0))))
        if on_progress:
            on_progress(min(t + CHUNK_MS, end), end)
    lang = hint or (max(heard, key=heard.get) if heard else "")
    return words, lang


_SENTENCE_END = re.compile(r"[.!?…]['\"»”)]*$")
# what Whisper writes over silence, music or credits (from its training subtitles)
_INVENTED = re.compile(r"thanks? (you )?for watching|subtitles? (by|created)|amara\.org|please subscribe|"
                       r"transcri(bed|ption) by|captions? by|www\.|\.com\b|♪", re.I)


_WEAK_END = {"a", "an", "the", "to", "of", "and", "or", "but", "let", "my", "your", "his", "her", "our", "their",
             "in", "on", "at", "for", "with", "from", "i", "you", "we", "he", "she", "it", "they", "is", "was"}


def _two_lines(text: str, max_line: int) -> str:
    """Break into two lines that both fit, preferring a break after punctuation, never after a
    word that leans on the next one ("let / me"), then the most even split."""
    if len(text) <= max_line:
        return text
    best = None
    for i, ch in enumerate(text):
        if ch != " ":
            continue
        left, right = text[:i], text[i + 1:]
        if len(left) > max_line or len(right) > max_line:
            continue
        last = left.rsplit(" ", 1)[-1]
        score = (0 if left[-1] in ",.;:!?…" else 1 if last.lower().strip("'\"") not in _WEAK_END else 2,
                 abs(len(left) - len(right)))
        if best is None or score < best[0]:
            best = (score, i)
    if best is None:                      # can't fit two lines: the most even split
        cands = [i for i, ch in enumerate(text) if ch == " "]
        if not cands:
            return text
        i = min(cands, key=lambda i: abs(i - len(text) / 2))
        return text[:i] + "\n" + text[i + 1:]
    return text[:best[1]] + "\n" + text[best[1] + 1:]


def words_to_cues(words: list[Spoken], max_line: int = 42, max_lines: int = 2, max_ms: int = 6000,
                  gap_ms: int = 700) -> list[tuple[int, int, str]]:
    """Group spoken words into subtitle lines: a new line after a pause, a sentence end, or when the
    text would no longer fit two lines or stay on screen too long. Whisper's habit of repeating a
    phrase over silence or music ("Thank you." x 5) is cut back to one, and near-silent guesses dropped."""
    cues: list[tuple[int, int, str]] = []
    cur: list[Spoken] = []

    def flush():
        if not cur:
            return
        text = " ".join(w.text for w in cur).strip()
        conf = sum(w.prob for w in cur) / len(cur)
        if text and conf >= 0.25 and re.search(r"\w", text) and not _INVENTED.search(text):
            cues.append((cur[0].start_ms, cur[-1].end_ms, text))
        cur.clear()

    def fits(text: str) -> bool:
        if len(text) <= max_line:
            return True
        if max_lines < 2:
            return False
        return all(len(x) <= max_line for x in _two_lines(text, max_line).split("\n"))

    for w in words:
        if cur:
            joined = " ".join(x.text for x in cur)
            if (w.start_ms - cur[-1].end_ms > gap_ms or not fits(joined + " " + w.text)
                    or w.end_ms - cur[0].start_ms > max_ms
                    or (_SENTENCE_END.search(cur[-1].text) and len(joined) >= 8)):
                flush()
        cur.append(w)
    flush()
    out: list[tuple[int, int, str]] = []
    for i, (a, b, text) in enumerate(cues):
        if out and len(text) <= 40 and text.lower() == out[-1][2].replace("\n", " ").lower():
            continue
        nxt = cues[i + 1][0] if i + 1 < len(cues) else b + 2000
        end = max(min(b + 400, nxt - 80), min(a + 900, nxt - 80), a + 300)
        out.append((a, end, _two_lines(text, max_line)))
    return out


def transcript_file(url: str, video: Path, prefer: list[str], cache_dir: Path, duration_s: float,
                    max_line: int = 42, on_progress=None) -> tuple[Path, str, int]:
    """A subtitle file made from the audio (cached per video). Returns (file, language heard, lines)."""
    import hashlib
    import os
    from .cues import _ts
    st = video.stat()
    key = hashlib.sha1(f"{video}|{st.st_size}|{int(st.st_mtime)}|transcript2".encode()).hexdigest()[:20]
    cache_dir.mkdir(parents=True, exist_ok=True)
    for hit in cache_dir.glob(f"{key}.*.heard.srt"):
        os.utime(hit)
        return hit, hit.name.split(".")[1], sum(1 for line in hit.read_text(encoding="utf-8").splitlines() if " --> " in line)
    words, lang = transcript(url, video, prefer, duration_s, on_progress=on_progress)
    if not lang:
        raise SpeechError("couldn't tell which language is spoken")
    cues = words_to_cues(words, max_line=max_line)
    if len(cues) < 5:
        raise SpeechError("hardly any speech was recognised in the audio")
    out = cache_dir / f"{key}.{lang}.heard.srt"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("\n".join(f"{i}\n{_ts(a)} --> {_ts(b)}\n{x}\n" for i, (a, b, x) in enumerate(cues, 1)),
                   encoding="utf-8")
    os.replace(tmp, out)
    return out, lang, len(cues)


# ------------------------------------------------------------------ matching lines to words
_DROP = re.compile(r"\[.*?\]|\(.*?\)|<.*?>|\{.*?\}|♪.*?(♪|$)", re.S)
_SPEAKER = re.compile(r"^\s*-?\s*[A-Z][A-Z .'-]{1,30}:\s*", re.M)


def _tokens(text: str) -> list[str]:
    text = _SPEAKER.sub(" ", _DROP.sub(" ", text))
    return [t for t in re.split(r"[^\w']+", text.lower().replace("’", "'")) if t and t != "'"]


def anchors(cues, words: list[Word]) -> dict[int, int]:
    """Position in `cues` -> time (ms) the line's first word is spoken, for lines found in the transcript.

    A line counts when its first or second word sits in a run of at least two words that match
    the transcript in order, and at least half of its words (or two) are matched."""
    ct: list[str] = []
    owner: list[tuple[int, int]] = []
    for i, c in enumerate(cues):
        for k, tok in enumerate(_tokens(c.text)):
            ct.append(tok)
            owner.append((i, k))
    at = [w.token for w in words]
    hit: dict[int, int] = {}
    for blk in SequenceMatcher(None, ct, at, autojunk=False).get_matching_blocks():
        if blk.size >= 2:
            for k in range(blk.size):
                hit[blk.a + k] = blk.b + k
    per: dict[int, list[tuple[int, int]]] = {}
    for pos, wi in hit.items():
        i, k = owner[pos]
        per.setdefault(i, []).append((k, wi))
    out = {}
    for i, pairs in per.items():
        # the line's words must sit together in the transcript (wi - k nearly constant): a phrase
        # repeated from a neighbouring line can match elsewhere, and must not set this line's time
        diag = max((wi - k for k, wi in pairs), key=lambda d: sum(abs((w - k) - d) <= 2 for k, w in pairs))
        band = [(k, wi) for k, wi in pairs if abs((wi - k) - diag) <= 2]
        n = len(_tokens(cues[i].text))
        k, wi = min(band)
        if k > 1 or len(band) < min(2, n) or len(band) * 2 < n:
            continue
        t = words[wi].start_ms
        if k == 1:          # the first word wasn't matched: start where the word before it was said
            if wi == 0 or words[wi].start_ms - words[wi - 1].start_ms > 1200:
                continue
            t = words[wi - 1].start_ms
        out[i] = t
    return out


def lead_ms(cues, anch: dict[int, int]) -> int:
    """How early this track shows a line before it is spoken (median), clamped to a sane habit."""
    if len(anch) < MIN_ANCHORS:
        return 0
    m = round(statistics.median(cues[i].start_ms - t for i, t in anch.items()))
    return max(LEAD_RANGE[0], min(LEAD_RANGE[1], m))


def shifts(cues, anch: dict[int, int], lead: int, min_anchors: int = MIN_ANCHORS) -> list[int] | None:
    """A shift (ms) for every line, or None when too little of the track was found in the speech."""
    if len(anch) < min_anchors or len(anch) < MIN_ANCHORED * len(cues):
        return None
    idx = sorted(anch)
    raw = {i: anch[i] + lead - cues[i].start_ms for i in idx}

    def local(i_pos: int, keep: list[int]) -> float:
        lo, hi = max(0, i_pos - NEIGHBOURS), min(len(keep), i_pos + NEIGHBOURS + 1)
        return statistics.median(raw[keep[j]] for j in range(lo, hi))

    good = [i for p, i in enumerate(idx) if abs(raw[i] - local(p, idx)) <= OUTLIER_MS]
    if len(good) < min_anchors:
        return None
    smooth = {i: local(p, good) for p, i in enumerate(good)}
    out: list[int] = []
    j = 0
    for i, c in enumerate(cues):
        while j < len(good) - 1 and good[j + 1] <= i:
            j += 1
        a, b = good[j], good[min(j + 1, len(good) - 1)]
        if i <= good[0]:
            s = smooth[good[0]]
        elif i >= good[-1]:
            s = smooth[good[-1]]
        elif a == b or cues[b].start_ms == cues[a].start_ms:
            s = smooth[a]
        else:               # between two anchors: follow the line's time
            f = (c.start_ms - cues[a].start_ms) / (cues[b].start_ms - cues[a].start_ms)
            s = smooth[a] + max(0.0, min(1.0, f)) * (smooth[b] - smooth[a])
        out.append(round(s) if abs(s) >= MOVE_MIN_MS else 0)
    return out


def apply(cues, moves: list[int]) -> int:
    """Move the lines (start and end together) and stop any from running into the next. Returns how many moved."""
    n = 0
    for c, m in zip(cues, moves):
        if m:
            c.start_ms, c.end_ms = max(0, c.start_ms + m), max(0, c.end_ms + m)
            if c.event is not None:
                c.event.start, c.event.end = c.start_ms, c.end_ms
            n += 1
    for a, b in zip(cues, cues[1:]):
        if a.end_ms > b.start_ms > a.start_ms:
            a.end_ms = max(a.start_ms + 300, b.start_ms)
            if a.event is not None:
                a.event.end = a.end_ms
    return n


def summary(moves: list[int]) -> str:
    moved = [m for m in moves if m]
    if not moved:
        return "already in sync with the speech"
    return (f"{len(moved)} of {len(moves)} lines moved to the speech "
            f"(typical {statistics.median(abs(m) for m in moved) / 1000:.1f} s, at most {max(abs(m) for m in moved) / 1000:.1f} s)")


# ------------------------------------------------------------------ whole files
def sync_cues(url: str, video: Path, lang: str, cues, duration_s: float | None = None) -> list[int] | None:
    """Shifts that put every line on the speech (None: too little found). The cues are not changed."""
    if duration_s is None:
        from .media import probe
        duration_s = probe(video)[1] or (max(c.end_ms for c in cues) / 1000 + 30)
    words = transcribe(url, video, lang, 0, int(duration_s * 1000))
    anch = anchors(cues, words)
    return shifts(cues, anch, lead_ms(cues, anch))


def synced_copy(url: str, video: Path, src: Path, lang: str, cache_dir: Path) -> tuple[Path, str]:
    """A copy of the subtitle file `src` moved onto the speech, kept in the cache (src is never
    written). Returns (file to use, what happened)."""
    import hashlib
    import os
    from . import cues as cuesmod
    st = src.stat()
    key = hashlib.sha1(f"{src}|{st.st_size}|{int(st.st_mtime)}|{video}|speech1".encode()).hexdigest()[:20]
    out = cache_dir / f"{key}.{lang}.speech{src.suffix.lower()}"
    note = out.with_suffix(out.suffix + ".txt")
    if out.is_file():
        os.utime(out)
        return out, note.read_text(encoding="utf-8") if note.is_file() else "synced to speech"
    doc = cuesmod.load(src)
    moves = sync_cues(url, video, lang, doc.cues)
    if moves is None:
        return src, "speech sync: too few lines were found in the speech, timing kept"
    apply(doc.cues, moves)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    doc.ssa.save(str(tmp), format_="ass" if out.suffix == ".ass" else "srt")
    os.replace(tmp, out)
    what = summary(moves)
    note.write_text(what, encoding="utf-8")
    return out, what
