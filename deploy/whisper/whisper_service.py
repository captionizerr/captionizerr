"""A small speech-to-text service for Captionizerr's audio sync, built on faster-whisper.

It speaks the subset of the whisper-asr-webservice API that Captionizerr (and Bazarr's Whisper
provider) use, so either can point at it:

    POST /asr?task=transcribe&language=en&output=json&word_timestamps=true&encode=true
         multipart field `audio_file` (any format ffmpeg reads; encode=false: raw s16le 16 kHz mono)
         -> json {text, language, segments: [{start, end, text, words: [{word, start, end, probability}]}]}
            or output=srt | vtt | txt
    POST /detect-language   multipart `audio_file` -> {detected_language, language_code, confidence}
    GET  /health

Settings (environment): WHISPER_MODEL (default "small"), WHISPER_THREADS (default: all cores),
WHISPER_COMPUTE (default "int8"). One request runs at a time; others wait.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from faster_whisper import WhisperModel

MODEL = os.environ.get("WHISPER_MODEL", "small")
model = WhisperModel(MODEL, device="cpu", compute_type=os.environ.get("WHISPER_COMPUTE", "int8"),
                     cpu_threads=int(os.environ.get("WHISPER_THREADS", "0") or 0))
lock = threading.Lock()
app = FastAPI(title="whisper", docs_url=None, redoc_url=None)


def _audio(upload: UploadFile, encode: bool) -> np.ndarray:
    """The upload as 16 kHz mono float32 samples (decoded by ffmpeg, not PyAV: PyAV releases keep
    breaking faster-whisper's own decoder)."""
    raw = tempfile.NamedTemporaryFile(delete=False, suffix=".in")
    raw.write(upload.file.read())
    raw.close()
    try:
        src = ["-i", raw.name] if encode else ["-f", "s16le", "-ar", "16000", "-ac", "1", "-i", raw.name]
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *src, "-ar", "16000", "-ac", "1",
                            "-f", "f32le", "-"], capture_output=True)
    finally:
        os.unlink(raw.name)
    if r.returncode:
        raise HTTPException(400, f"can't decode the audio: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def _ts(t: float, sep: str) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL}


@app.post("/asr")
def asr(audio_file: UploadFile = File(...), task: str = Query("transcribe"), language: str | None = Query(None),
        output: str = Query("json"), word_timestamps: bool = Query(False), encode: bool = Query(True),
        vad_filter: bool = Query(False), initial_prompt: str | None = Query(None)):
    audio = _audio(audio_file, encode)
    with lock:
        segs, info = model.transcribe(audio, task="translate" if task == "translate" else "transcribe",
                                      language=language or None, word_timestamps=word_timestamps,
                                      vad_filter=vad_filter, initial_prompt=initial_prompt, beam_size=5)
        segs = list(segs)
    if output == "json":
        return JSONResponse({"text": "".join(s.text for s in segs).strip(), "language": info.language,
                             "segments": [{"start": s.start, "end": s.end, "text": s.text.strip(),
                                           "words": [{"word": w.word, "start": w.start, "end": w.end,
                                                      "probability": w.probability} for w in (s.words or [])]}
                                          for s in segs]})
    if output in ("srt", "vtt"):
        sep = "," if output == "srt" else "."
        body = "".join(f"{i}\n{_ts(s.start, sep)} --> {_ts(s.end, sep)}\n{s.text.strip()}\n\n"
                       for i, s in enumerate(segs, 1))
        return PlainTextResponse(body if output == "srt" else "WEBVTT\n\n" + body)
    return PlainTextResponse("".join(s.text for s in segs).strip())


@app.post("/detect-language")
def detect_language(audio_file: UploadFile = File(...), encode: bool = Query(True)):
    audio = _audio(audio_file, encode)[: 16000 * 30]
    with lock:
        _, info = model.transcribe(audio, beam_size=1, without_timestamps=True)
    return {"detected_language": info.language, "language_code": info.language,
            "confidence": info.language_probability}
