"""Fixes from the Northwind S03 run: speaker-label false positives, reviews closed by saving, busy videos."""
import json
import re
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from captionizerr import media
from captionizerr.app import create_app
from captionizerr.config import Settings
from captionizerr.validate import label_kept

from test_service import KEY, FakeProvider, make_video, HAS_FFMPEG

ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
SAME = {"Origin": "http://testserver"}


@pytest.mark.parametrize("text, kept", [
    ("Да,\nя так и сказал: «Ох».", False), ("Мама просила передать:\n«Не звони».", False),
    ('Он сказал: "Нет".', False), ("Вот что я тебе скажу: хватит.", False),
    ("ДЭВИС: Да.", True), ("Мужчина по радио: Они не станут.", True), ("- GRACE: Ma says hi.", True)])
def test_speaker_label_vs_a_colon_before_quoted_speech(text, kept):
    assert label_kept(text) is kept


def _app(tmp_path):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], library_roots=[str(tmp_path / "lib")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    app.state.store.save_settings({"_setup_done": True})
    return app


@ffmpeg
def test_saving_in_the_editor_closes_the_review_when_nothing_serious_is_left(tmp_path):
    app = _app(tmp_path)
    st, w, c = app.state.store, app.state.worker, TestClient(app)
    v = make_video(tmp_path / "lib/Show", "Show S01E01.mkv")
    job = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    st._q("UPDATE jobs SET state='needs_review' WHERE id=?", (job.id,))          # as if one line were too fast
    page = c.get("/edit", params={"path": str(v), "lang": "ru"}).text
    assert "Mark as reviewed" in page and "waiting for your review" in page
    rows = json.loads(re.search(r"const ROWS = (\[.*?\]);", page, re.S).group(1))
    token = re.search(r"let token = (\"[^\"]*\")", page) or re.search(r"token\s*[:=]\s*(\"\d+\")", page)
    tok = json.loads(token.group(1))
    r = c.post("/api/edit/save", json={"path": str(v), "lang": "ru", "key": "", "token": tok,
                                       "edits": {str(rows[0]["id"]): rows[0]["tr"] + " "  + "да"}}, headers=SAME)
    assert r.status_code == 200 and r.json()["reviewed"] is True
    j = st.get(job.id)
    assert j.state == "done" and "reviewed in the editor" in j.detail


def test_mark_as_reviewed_accepts_a_file_as_it_is(tmp_path):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    j = st.enqueue("sonarr", "Show S01E02", "/x/a.mkv", "ru", None)
    st.finish(j.id, "needs_review", "10 cues from en (embedded); 1 flagged, 1 open; open: #3 too fast", "/x/a.captionizerr.ru.srt", 0.3, {}, "m")
    assert "Mark as reviewed" in c.get(f"/jobs/{j.id}").text
    c.post(f"/jobs/{j.id}/reviewed", data={"back": f"/jobs/{j.id}"}, headers=SAME)
    assert st.get(j.id).state == "done" and "marked as reviewed" in st.get(j.id).detail
    assert "Nothing to review" in c.post(f"/jobs/{j.id}/reviewed", data={}, headers=SAME).text


def test_a_video_that_cant_be_read_right_now_is_retried_then_given_up(tmp_path, monkeypatch):
    app = _app(tmp_path)
    st, w = app.state.store, app.state.worker
    v = tmp_path / "lib/Show/Show S01E03.mkv"
    v.parent.mkdir(parents=True)
    v.write_bytes(b"x")
    def busy(*a, **k):
        raise subprocess.CalledProcessError(1, ["ffprobe"], stderr=f"{v}: Invalid argument")
    monkeypatch.setattr(media, "find_source", busy)
    job = st.enqueue("sonarr", "Show S01E03", str(v), "ru", None)
    w.process(st.next_queued())
    j = st.get(job.id)
    assert j.state == "waiting_source" and j.detail.startswith("Video busy:")
    w.check_waiting()
    assert st.get(job.id).state == "waiting_source"                           # not yet: every 30 minutes
    st._q("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 1900, job.id))
    w.check_waiting()
    assert st.get(job.id).state == "queued"
    st._q("UPDATE jobs SET wait_since=? WHERE id=?", (time.time() - 25 * 3600, job.id))
    w.process(st.next_queued())
    j = st.get(job.id)
    assert j.state == "failed" and "can't read the video" in j.detail and "Invalid argument" in j.detail


def test_editor_page_lists_reviews_first_then_recent(tmp_path):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    a = st.enqueue("sonarr", "Show S01E01", "/x/a.mkv", "ru", "show"); st.finish(a.id, "done", "10 cues from en (embedded); 0 flagged, 0 open", "/x/a.captionizerr.ru.srt", 0.3, {}, "m")
    b = st.enqueue("sonarr", "Show S01E02", "/x/b.mkv", "ru", "show"); st.finish(b.id, "needs_review", "10 cues from en (embedded); 1 flagged, 1 open", "/x/b.captionizerr.ru.srt", 0.3, {}, "m")
    n = st.enqueue("notes", "Show: series notes drafted", "/x/a.mkv", "ru", "show"); st.finish(n.id, "done", "x", "", 0.05, {}, "m")
    page = c.get("/editor").text
    assert 'href="/editor"' in page and "Waiting for your review" in page and "series notes" not in page
    assert page.index("Show S01E02") < page.index("Show S01E01") and ">Review<" in page and ">Edit<" in page
    assert 'href="/editor"' in c.get("/").text                                  # in the top menu everywhere


def test_existing_target_subtitles_are_noted_or_skipped(tmp_path, monkeypatch):
    from captionizerr import media as M
    from captionizerr.media import SubStream
    app = _app(tmp_path)
    st, w = app.state.store, app.state.worker
    v = tmp_path / "lib/Film/Film.mkv"
    v.parent.mkdir(parents=True)
    v.write_bytes(b"x")
    tracks = [SubStream(3, "subrip", "rus", "Полные Blu-Ray", False, False),
              SubStream(4, "subrip", "rus", "Комментарии режиссёра", False, False)]
    monkeypatch.setattr(M, "probe", lambda video: (tracks, 7300.0))
    assert [t.index for t in M.target_tracks(tracks, "ru")] == [3]
    app.state.settings.existing_subs = "skip"
    j = st.enqueue("radarr", "Film (2008)", str(v), "ru", None)
    w.process(st.next_queued())
    assert st.get(j.id).state == "skipped" and "already has RU subtitles (Полные Blu-Ray)" in st.get(j.id).detail
    monkeypatch.setattr(M, "find_source", lambda *a, **k: None)          # manual start: goes on (here: no source)
    m = st.enqueue("manual", "Film (2008)", str(v), "ru", None)
    w.process(st.next_queued())
    assert st.get(m.id).state != "skipped"


def test_editor_has_the_same_actions_as_the_job_page(tmp_path):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    v = tmp_path / "lib/Show/Show S01E01.mkv"
    v.parent.mkdir(parents=True)
    v.write_bytes(b"x")
    out = v.with_name("Show S01E01.captionizerr.ru.srt")
    out.write_text("1\n00:00:01,000 --> 00:00:02,000\nПривет\n", encoding="utf-8")
    j = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None); st.finish(j.id, "done", "1 cues from en (embedded); 0 flagged, 0 open", str(out), 0.1, {}, "m")
    page = c.get("/edit", params={"path": str(v), "lang": "ru"}).text
    for label in (">Download<", ">Share…<", ">Re-translate<", ">Job &amp; history<"):
        assert label in page, label
    assert "Share on OpenSubtitles…</a>" not in page


def test_short_neighbours_sharing_two_common_words_are_not_a_line_split():
    from captionizerr.cues import Cue
    from captionizerr.validate import boundary_issues
    dia = [Cue(i, i * 3000, i * 3000 + 2000, f"Some ordinary line number {i} here.") for i in range(1, 30)]
    tr = {c.index: f"Обычная строка номер {c.index} здесь." for c in dia}
    dia += [Cue(30, 90000, 92000, "Marina has a brother."), Cue(31, 93000, 95000, "I mean, she had...\nhad a brother.")]
    tr.update({30: "У Марины есть брат.", 31: "То есть у неё был...\nбыл брат."})
    assert not [i for i in boundary_issues(dia, tr) if i.index in (30, 31)]
    tr[31] = "У Марины есть брат, понимаешь."            # a real shift: the same sentence twice
    assert [i for i in boundary_issues(dia, tr) if i.index in (30, 31)]
