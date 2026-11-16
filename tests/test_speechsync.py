"""Speech sync: lines found in a Whisper transcript, moved onto the speech."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from captionizerr import cues as cuesmod, media, speechsync
from captionizerr.app import create_app
from captionizerr.config import Settings
from captionizerr.cues import Cue

from test_m4 import _video_with, old
from test_service import HAS_FFMPEG, KEY, FakeProvider

ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
SAME = {"Origin": "http://testserver"}


def _irregular(n: int, seed: int = 7, shift_from: int | None = None, shift_ms: int = 0):
    """n lines with uneven lengths and gaps; lines from `shift_from` on moved by shift_ms."""
    import random
    rnd, t, out = random.Random(seed), 1000, []
    for i in range(n):
        d = rnd.randint(900, 3500)
        off = shift_ms if shift_from is not None and i >= shift_from else 0
        out.append((t + off, t + d + off, f"Line number {i + 1} of the dialogue."))
        t += d + rnd.randint(300, 2500)
    return out


def _srt_of(cues) -> str:
    def ts(ms):
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i + 1}\n{ts(a)} --> {ts(b)}\n{x}\n" for i, (a, b, x) in enumerate(cues))


def words_for(timeline, shift=lambda i, t: 0, lead=0):
    """What Whisper would hear: each line's words spread over its time (moved by shift(i, t))."""
    out = []
    for i, (a, b, text) in enumerate(timeline):
        toks = speechsync._tokens(text)
        d = (b - a) // max(1, len(toks))
        for k, tok in enumerate(toks):
            s = a + k * d + shift(i, a) - lead
            out.append(speechsync.Word(s, s + d - 20, tok))
    return out


def cues_of(timeline):
    return [Cue(i + 1, a, b, t) for i, (a, b, t) in enumerate(timeline)]


def test_tokens_drop_sound_tags_speakers_and_songs():
    assert speechsync._tokens("[SIGHS] SNOWBALL?") == ["snowball"]
    assert speechsync._tokens("HANK: Don't — go.\n♪ la la ♪") == ["don't", "go"]


def test_in_sync_track_is_left_alone():
    tl = _irregular(60)
    cs = cues_of(tl)
    anch = speechsync.anchors(cs, words_for(tl, lead=300))       # shown 0.3 s before the speech: its habit
    moves = speechsync.shifts(cs, anch, speechsync.lead_ms(cs, anch))
    assert moves is not None and not any(moves)


def test_a_late_stretch_is_moved_and_a_wrong_match_is_ignored():
    tl = _irregular(80)
    cs = cues_of(tl)
    w = words_for(tl, shift=lambda i, t: 3000 if i >= 50 else 0)  # from line 51 on, said 3 s later
    w[40] = speechsync.Word(w[40].start_ms + 20_000, w[40].end_ms + 20_000, w[40].token)
    anch = speechsync.anchors(cs, w)
    moves = speechsync.shifts(cs, anch, speechsync.lead_ms(cs, anch))
    assert all(m == 0 for m in moves[:45]) and all(abs(m - 3000) <= 50 for m in moves[55:])
    speechsync.apply(cs, moves)
    assert all(a.end_ms <= b.start_ms or b.start_ms <= a.start_ms for a, b in zip(cs, cs[1:]))


def test_too_little_found_changes_nothing():
    tl = _irregular(60)
    other = [(a, b, "something else entirely here") for a, b, _ in tl]
    assert speechsync.shifts(cues_of(tl), speechsync.anchors(cues_of(tl), words_for(other)), 0) is None


@ffmpeg
def test_spliced_ending_follows_the_speech(tmp_path, monkeypatch):
    truth = _irregular(80)
    cut_at = 45
    emb = list(truth[:cut_at])
    total = truth[-1][1] + 10_000
    emb[-1] = (emb[-1][0], total - 500, emb[-1][2])
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt_of(emb), seconds=total // 1000)
    (v.parent / "Show S01E01.en.srt").write_text(_srt_of(_irregular(80, shift_from=cut_at - 1, shift_ms=-2500)),
                                                 encoding="utf-8")
    monkeypatch.setattr(speechsync, "transcribe", lambda url, video, lang, a, b, timeout=900: words_for(truth))
    src = media.find_source(v, tmp_path / "cache", ["en"], "ru", speech_url="http://whisper")
    got = cuesmod.load(src.path).cues
    errs = [abs(c.start_ms - truth[int(c.text.split()[2]) - 1][0]) for c in got]
    assert max(errs) <= 100, [(c.text, e) for c, e in zip(got, errs) if e > 100]
    plain = media.find_source(v, tmp_path / "cache2", ["en"], "ru")          # without Whisper: 2.5 s off
    assert max(abs(c.start_ms - truth[int(c.text.split()[2]) - 1][0]) for c in cuesmod.load(plain.path).cues) >= 2000


def _app(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], library_roots=[str(tmp_path / "lib")], **kw)
    return create_app(s, provider_factory=FakeProvider, start_worker=False)


@ffmpeg
def test_downloaded_subtitle_is_put_on_the_speech_before_translating(tmp_path, monkeypatch):
    from test_m4 import video_without_subs
    app = _app(tmp_path, whisper_url="http://whisper")
    store, w = app.state.store, app.state.worker
    v = video_without_subs(tmp_path / "lib/Show", "Show S01E01.mkv")
    truth = [(a + 3000, b + 3000, x) for a, b, x in _irregular(40)]
    side = v.with_name("Show S01E01.en.srt")
    side.write_text(_srt_of([(a - 1500, b - 1500, t) for a, b, t in truth]), encoding="utf-8")   # 1.5 s early
    old(side)
    monkeypatch.setattr(speechsync, "transcribe", lambda url, video, lang, a, b, timeout=900: words_for(truth))
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state in ("done", "needs_review") and "(sidecar, synced to speech)" in j.detail and "40 of 40 lines moved to the speech" in j.detail, j.detail
    out = cuesmod.load(j.output_path).cues
    assert all(abs(c.start_ms - (t[0] - 600)) <= 60 for c, t in zip(out, truth))   # habit clamped to 0.6 s early
    assert side.read_text(encoding="utf-8").startswith("1\n")                       # the download itself untouched


@ffmpeg
def test_resync_moves_an_existing_translation_without_translating(tmp_path, monkeypatch):
    app = _app(tmp_path)
    store, w = app.state.store, app.state.worker
    store.save_settings({"_setup_done": True})
    truth = _irregular(40)
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt_of(truth), seconds=truth[-1][1] // 1000 + 5)
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    first = store.get(job.id)
    c = TestClient(app)
    assert "Re-sync to speech" not in c.get(f"/jobs/{job.id}").text                  # no Whisper set up yet
    app.state.settings.whisper_url = "http://whisper"
    assert "Re-sync to speech" in c.get(f"/jobs/{job.id}").text
    late = lambda i, t: 4000 if i >= 30 else 0                                          # noqa: E731
    monkeypatch.setattr(speechsync, "transcribe", lambda url, video, lang, a, b, timeout=900: words_for(truth, late))
    c.post(f"/jobs/{job.id}/resync", headers=SAME)
    sync = store.next_queued()
    assert sync.source == "sync"
    w.process(sync)
    j = store.get(sync.id)
    assert j.state == "done" and j.cost_usd == 0 and "moved to the speech" in j.detail, j.detail
    out = cuesmod.load(first.output_path).cues
    assert [x.text for x in out] == [x.text for x in cuesmod.load(first.output_path).cues]
    assert abs(out[35].start_ms - (truth[35][0] + 4000)) <= 60 and abs(out[5].start_ms - truth[5][0]) <= 60
    from captionizerr.writer import history
    assert history(Path(first.output_path), app.state.settings.history_dir)          # old timing kept for Undo
    assert "re-sync" in c.get("/?state=other").text.lower()


def test_whisper_connection_test(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(speechsync, "health", lambda url, timeout=10: "small")
    r = TestClient(app).post("/settings/test/whisper", data={"whisper_url": "http://whisper:9000"}, headers=SAME)
    assert r.json()["ok"] and "small" in r.json()["message"]
