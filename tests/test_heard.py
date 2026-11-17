"""Subtitles made from the audio when a video has none (Whisper)."""
import time

import pytest

from captionizerr import cues as cuesmod, speechsync
from captionizerr.app import create_app
from captionizerr.config import Settings
from captionizerr.speechsync import Spoken, words_to_cues

from test_service import KEY, FakeProvider, HAS_FFMPEG

ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")


def speech(n=40, start=2000):
    """n short sentences, ~2.5 s apart, as Whisper would return them."""
    out, t = [], start
    for i in range(n):
        for k, w in enumerate(f"This is spoken sentence number {i + 1}.".split()):
            out.append(Spoken(t + k * 250, t + k * 250 + 220, w, 0.9))
        t += 2500
    return out


def test_words_become_readable_subtitle_lines():
    cues = words_to_cues(speech(5))
    assert len(cues) == 5 and cues[0][2] == "This is spoken sentence number 1."
    long = [Spoken(1000 + i * 200, 1180 + i * 200, "word") for i in range(40)]
    out = words_to_cues(long)
    assert all(len(t.replace("\n", " ")) <= 84 and all(len(x) <= 42 for x in t.split("\n")) for _, _, t in out)
    assert all(b - a <= 6500 for a, b, _ in out)
    ghosts = [Spoken(10000 + i * 3000, 10600 + i * 3000, "Thank you.") for i in range(5)]
    assert [t for _, _, t in words_to_cues(ghosts)] == ["Thank you."]
    assert words_to_cues([Spoken(0, 500, "mumble", 0.05)]) == []


def _app(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], library_roots=[str(tmp_path / "lib")],
                 whisper_url="http://whisper", **kw)
    return create_app(s, provider_factory=FakeProvider, start_worker=False)


def _fake_whisper(monkeypatch, lang="en", calls=None):
    def tr(url, video, prefer, duration_s, timeout=900, on_progress=None):
        if calls is not None:
            calls.append(video)
        if on_progress:
            on_progress(duration_s * 1000, duration_s * 1000)
        return speech(40), lang
    monkeypatch.setattr(speechsync, "transcript", tr)


@ffmpeg
def test_no_subtitle_anywhere_is_made_from_the_audio_and_sent_to_review(tmp_path, monkeypatch):
    from test_m4 import video_without_subs
    app = _app(tmp_path)
    st, w = app.state.store, app.state.worker
    v = video_without_subs(tmp_path / "lib/Show", "Show S01E01.mkv")
    _fake_whisper(monkeypatch)
    job = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    j = st.get(job.id)
    assert j.state == "needs_review" and "40 cues from en (speech)" in j.detail and "speech recognition" in j.detail
    assert len(cuesmod.load(j.output_path).cues) == 40


@ffmpeg
def test_bazarr_gets_its_chance_first(tmp_path, monkeypatch):
    from test_m4 import video_without_subs
    app = _app(tmp_path, bazarr_url="http://bazarr", bazarr_key="k")
    st, w = app.state.store, app.state.worker
    v = video_without_subs(tmp_path / "lib/Show", "Show S01E01.mkv")
    calls = []
    _fake_whisper(monkeypatch, calls=calls)
    monkeypatch.setattr(w, "_bazarr_request", lambda bz, job: True)
    job = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    assert st.get(job.id).state == "waiting_source" and not calls
    st._q("UPDATE jobs SET wait_since=? WHERE id=?", (time.time() - 4 * 3600, job.id))
    w.check_waiting()
    assert st.get(job.id).state == "queued" and "making a subtitle from the audio" in st.get(job.id).detail
    w.process(st.next_queued())
    assert st.get(job.id).state == "needs_review" and calls


@ffmpeg
@pytest.mark.parametrize("on, lang, state, words", [(False, "en", "no_source", "Bazarr"),
                                                   (True, "ru", "no_source", "already in RU")])
def test_switched_off_or_audio_already_in_the_target_language(tmp_path, monkeypatch, on, lang, state, words):
    from test_m4 import video_without_subs
    app = _app(tmp_path, speech_source=on)
    st, w = app.state.store, app.state.worker
    v = video_without_subs(tmp_path / "lib/Show", "Show S01E01.mkv")
    _fake_whisper(monkeypatch, lang=lang)
    job = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    j = st.get(job.id)
    assert j.state == state and words in j.detail, j.detail


def test_lines_break_at_punctuation_and_short_sentences_stand_alone():
    from captionizerr.speechsync import _two_lines
    assert _two_lines("Okay, well, if you change your mind, then come here.", 42) == \
        "Okay, well, if you change your mind,\nthen come here."
    assert not _two_lines("I was going to let me know about the brochure", 42).split("\n")[0].endswith(" let")
    ws = [Spoken(1000 + i * 250, 1200 + i * 250, w) for i, w in enumerate("What is it, Dad? Yeah, Dad.".split())]
    assert [t for _, _, t in words_to_cues(ws)] == ["What is it, Dad?", "Yeah, Dad."]


def test_text_whisper_invents_over_silence_is_dropped():
    for phrase in ("Thanks for watching!", "Subtitles by the Amara.org community", "♪ ♪"):
        ws = [Spoken(1000 + i * 300, 1250 + i * 300, w) for i, w in enumerate(phrase.split())]
        assert words_to_cues(ws) == [], phrase
