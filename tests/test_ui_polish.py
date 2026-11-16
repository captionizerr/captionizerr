"""0.2.9 UI polish: plain-language details, history, skipped promo lines, share comment, sizes."""
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from captionizerr import cues as cuesmod, reuse
from captionizerr.app import create_app
from captionizerr.config import Settings
from captionizerr.web.ui import human_detail, human_size, share_comment, short_title

from test_service import KEY, FakeProvider, make_video, HAS_FFMPEG
import pytest

ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")


def test_details_in_plain_words():
    assert human_detail("755 cues from en (embedded + sidecar ending); 23 flagged, 0 open") == \
        "755 lines from the built-in English track, its missing ending from a downloaded subtitle; 23 lines checked and fixed"
    assert human_detail("40 cues from en (sidecar, synced to speech); 2 flagged, 1 open; open: #1 too fast") == \
        "40 lines from a downloaded English subtitle, synced to the speech; 2 lines checked, 1 still needs a look; open: #1 too fast"
    assert human_detail("10 cues from en (sidecar) via Batch; 0 flagged, 0 open").endswith("via the Batch API (half price); no lines needed fixing")
    assert human_detail("something else entirely") == "something else entirely" and human_detail("") == ""


def test_sizes_and_short_titles():
    assert human_size(0) == "empty" and human_size(40960) == "40 KB" and human_size(1153433) == "1.1 MB"
    assert short_title("Northwind - S02E05 - Second Wind WEBDL-1080p.captionizerr.ru.srt") == "Northwind S02E05"
    assert short_title("/x/Dune.2021.mkv") == "Dune.2021"


def test_share_comment_follows_the_source():
    def job(src, created, detail, state="done"):
        return NS(source=src, state=state, created=created, detail=detail)
    own = [job("sonarr", 1, "700 cues from en (embedded); 3 flagged, 0 open")]
    assert "this release's own subtitles, timed to the file" in share_comment("m", own)
    spliced = [job("manual", 1, "755 cues from en (embedded + sidecar ending); 3 flagged, 0 open")]
    assert "missing ending completed from another subtitle" in share_comment("m", spliced)
    assert "checked against the speech" not in share_comment("m", spliced)
    synced = spliced + [job("sync", 2, "54 of 809 lines moved to the speech")]
    assert "missing ending" in share_comment("m", synced) and "timing checked against the speech" in share_comment("m", synced)
    side = [job("sonarr", 1, "40 cues from en (sidecar, synced to speech); 0 flagged, 0 open")]
    assert "downloaded subtitle, re-timed" in share_comment("m", side) and "speech" in share_comment("m", side)


def _app(tmp_path):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], library_roots=[str(tmp_path / "lib")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    app.state.store.save_settings({"_setup_done": True})
    return app


def test_sync_job_page_is_free_and_history_links_the_file(tmp_path):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    a = st.enqueue("manual", "Show S01E01", "/x/a.mkv", "ru", None); st.finish(a.id, "done", "10 cues from en (embedded); 0 flagged, 0 open", "/x/a.captionizerr.ru.srt", 0.3, {}, "m")
    b = st.enqueue("sync", "Show S01E01: re-sync to speech", "/x/a.mkv", "ru", None); st.finish(b.id, "done", "3 of 10 lines moved to the speech", "/x/a.captionizerr.ru.srt", 0.0, {}, "whisper")
    page = c.get(f"/jobs/{b.id}").text
    assert ">Free<" in page and "Input tokens" not in page and "re-sync to speech" in page
    assert "History of this file" in page and f'href="/jobs/{a.id}"' in page and "(this job)" in page
    assert "10 lines from the built-in English track" in c.get(f"/jobs/{a.id}").text


def test_dashboard_has_phone_layout_and_other_activity(tmp_path):
    app = _app(tmp_path)
    page = TestClient(app).get("/").text
    assert 'class="jobs"' in page and "Other activity" in page and "Other spend" not in page


@ffmpeg
def test_editor_shows_promo_lines_as_skipped_not_flagged(tmp_path):
    app = _app(tmp_path)
    st, w, c = app.state.store, app.state.worker, TestClient(app)
    v = make_video(tmp_path / "lib/Show", "Show S01E01.mkv")
    job = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    out = st.get(job.id).output_path
    src = reuse.stored_source(app.state.settings.data_dir / "sources", __import__("pathlib").Path(out))
    first = cuesmod.load(src[0]).cues[0]
    reuse.store_drops(app.state.settings.data_dir / "sources", __import__("pathlib").Path(out), [first.start_ms])
    doc = cuesmod.load(out)                        # make that line empty in the file, as a dropped promo is
    text = cuesmod.render(doc, {x.index: x.text for x in doc.cues if x.start_ms != first.start_ms})
    __import__("pathlib").Path(out).write_text(text, encoding="utf-8")
    page = c.get("/edit", params={"path": str(v), "lang": "ru"}).text
    import json, re
    rows = json.loads(re.search(r"const ROWS = (\[.*?\]);", page, re.S).group(1))
    skipped = [r for r in rows if r["skip"]]
    assert len(skipped) == 1 and skipped[0]["flags"] == [] and skipped[0]["tr"] == ""
    assert "skipped on purpose" in page
