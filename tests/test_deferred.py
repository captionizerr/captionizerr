"""0.3.0: film pages, per-episode markers, speech-sync modes, setup Whisper, running step, job actions."""
import pytest
from fastapi.testclient import TestClient

from captionizerr import speechsync
from captionizerr.app import create_app
from captionizerr.config import Settings
from captionizerr.web import ui as uimod

from test_service import KEY, FakeProvider, HAS_FFMPEG
from test_speechsync import _irregular, _srt_of, words_for

ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
SAME = {"Origin": "http://testserver"}


def _app(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], library_roots=[str(tmp_path / "lib")],
                 sonarr_url="http://sonarr", sonarr_key="k" * 32, radarr_url="http://radarr", radarr_key="k" * 32, **kw)
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    app.state.store.save_settings({"_setup_done": True})
    return app


def test_film_page_translate_history_and_settings(tmp_path, monkeypatch):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    film = tmp_path / "lib/Dune (2021)/Dune.2021.mkv"
    film.parent.mkdir(parents=True)
    film.write_bytes(b"x")
    movie = {"id": 7, "title": "Dune", "year": 2021, "overview": "Spice.", "movieFile": {"path": str(film)}}
    monkeypatch.setattr(uimod.Arr, "get", lambda self, path, **kw: [movie] if path == "/movie" else [] if path == "/series" else movie)
    lib = c.get("/library").text
    assert 'href="/library/film/7"' in lib
    page = c.get("/library/film/7").text
    assert "Dune" in page and ">Translate<" in page and "not yet" in page and "Film settings" in page
    j = st.enqueue("radarr", "Dune (2021)", str(film), "ru", "dune"); st.finish(j.id, "needs_review", "900 cues from en (embedded); 4 flagged, 1 open", str(film)[:-4] + ".captionizerr.ru.srt", 0.5, {}, "m")
    page = c.get("/library/film/7").text
    assert "needs review" in page and f'href="/jobs/{j.id}"' in page and "History" in page and "900 lines from the built-in English track" in page
    r = c.post("/library/film/7/settings", data={"title": "Dune", "enabled": "1", "langs": "ru, de", "tone": "", "context": "# Dune\nPaul"}, headers=SAME)
    assert "Film settings saved" in r.text and st.series("dune")["langs"] == ["ru", "de"]
    assert (app.state.settings.context_dir / "dune.md").read_text().startswith("# Dune")
    assert "Not a language code" in c.post("/library/film/7/settings", data={"title": "Dune", "langs": "r1"}, headers=SAME).text


def test_series_page_marks_each_episode(tmp_path, monkeypatch):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    eps = [{"season": 1, "episode": n, "title": f"Ep {n}", "path": f"/lib/Show/S01E0{n}.mkv"} for n in (1, 2, 3)]
    monkeypatch.setattr(uimod, "sonarr_series", lambda a: [{"id": 5, "title": "Show", "files": 3, "episodes": 3, "path": "/lib/Show", "overview": ""}])
    monkeypatch.setattr(uimod, "sonarr_episodes", lambda a, sid: [dict(e) for e in eps])
    a = st.enqueue("sonarr", "Show S01E01", eps[0]["path"], "ru", "show"); st.finish(a.id, "needs_review", "x", "", 0.3, {}, "m")
    b = st.enqueue("sonarr", "Show S01E02", eps[1]["path"], "ru", "show"); st.finish(b.id, "done", "Incomplete source: stops. 5 cues from en (embedded); 0 flagged, 0 open", "", 0.3, {}, "m")
    s2 = st.enqueue("sync", "Show S01E02: re-sync", eps[1]["path"], "ru", "show"); st.finish(s2.id, "done", "3 of 5 lines moved to the speech", "", 0, {}, "whisper")
    page = c.get("/library/series/5").text
    assert "needs review" in page and "source cut short" in page and "re-synced" in page
    assert f'href="/jobs/{a.id}"' in page and f'href="/jobs/{b.id}"' in page and 'class="eps"' in page and "<colgroup>" in page


@ffmpeg
@pytest.mark.parametrize("mode, synced", [("auto", True), ("off", False)])
def test_speech_sync_mode_decides_for_downloaded_subtitles(tmp_path, monkeypatch, mode, synced):
    from test_m4 import video_without_subs, old
    app = _app(tmp_path, whisper_url="http://whisper", speech_sync=mode)
    st, w = app.state.store, app.state.worker
    v = video_without_subs(tmp_path / "lib/Show", "Show S01E01.mkv")
    truth = [(a + 3000, b + 3000, x) for a, b, x in _irregular(30)]
    side = v.with_name("Show S01E01.en.srt")
    side.write_text(_srt_of([(a - 1500, b - 1500, x) for a, b, x in truth]), encoding="utf-8")
    old(side)
    monkeypatch.setattr(speechsync, "transcribe", lambda *a, **k: words_for(truth))
    job = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    assert ("synced to speech" in st.get(job.id).detail) is synced


def test_setup_offers_whisper_and_connections_saves_the_mode(tmp_path):
    app = _app(tmp_path)
    c = TestClient(app)
    assert 'name="whisper_url"' in c.get("/setup?step=media").text
    page = c.get("/settings/connections").text
    assert 'name="speech_sync"' in page and "Every translation, built-in tracks too" in page
    c.post("/settings/connections", data={"speech_sync": "all"}, headers=SAME)
    assert app.state.settings.speech_sync == "all"
    c.post("/settings/connections", data={"speech_sync": "bogus"}, headers=SAME)
    assert app.state.settings.speech_sync == "auto"


def test_dashboard_shows_the_running_step_and_job_actions_sit_in_one_row(tmp_path):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    j = st.enqueue("manual", "Show S01E04", "/x/a.mkv", "ru", None)
    st.next_queued()
    st.set_state(j.id, "running", "translating 700 cues from en (embedded), est $0.30")
    page = c.get("/").text
    assert "Show S01E04" in page and "translating 700 lines from the built-in English track" in page
    f = st.enqueue("manual", "Show S01E05", "/x/b.mkv", "ru", None); st.finish(f.id, "failed", "boom")
    jp = c.get(f"/jobs/{f.id}").text
    assert jp.index(">Retry<") < jp.index("<h2>Result</h2>")


def test_library_films_read_like_episodes_and_keep_their_own_settings(tmp_path, monkeypatch):
    app = _app(tmp_path)
    st, c = app.state.store, TestClient(app)
    lib = tmp_path / "lib"
    films = []
    for i, t in enumerate(("Dune", "Heat")):
        f = lib / t / f"{t}.2021.mkv"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        films.append({"id": i + 1, "title": t, "year": 2021, "movieFile": {"path": str(f)}})
    (lib / "Dune" / "Dune.2021.captionizerr.ru.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
    monkeypatch.setattr(uimod.Arr, "get", lambda self, path, **kw: films if path == "/movie" else [])
    st.save_series("heat", "Heat", True, ["ru", "de"], "")
    page = c.get("/library").text
    assert "not yet" not in page and "RU · Not translated" in page and "RU · Edit<" in page and page.count('name="path"') == 2
    assert "DE · Not translated" in page and "<th>Translation</th>" in page and "<th>ru</th>" not in page
    paths = [f["movieFile"]["path"] for f in films]
    form = {"back": "/library", "path": paths[1], f"key:{paths[1]}": "heat", f"label:{paths[1]}": "Heat (2021)"}
    c.post("/library/translate", data=form, headers=SAME)
    jobs = [j for j in st.list(10) if j.video_path == paths[1]]
    assert sorted(j.lang for j in jobs) == ["de", "ru"] and all(j.series_key == "heat" for j in jobs)
