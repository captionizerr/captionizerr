from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from captionizerr.app import create_app
from captionizerr.config import Settings

from test_service import FakeProvider, KEY, HAS_FFMPEG, make_video

SAME = {"Origin": "http://testserver"}


@pytest.fixture
def ui(tmp_path):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], library_roots=[str(tmp_path / "lib")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    return app, TestClient(app), tmp_path


@pytest.mark.parametrize("path", ["/", "/library", "/settings", "/providers", "/static/favicon.svg", "/static/mark.svg"])
def test_pages_render(ui, path):
    _, c, _ = ui
    r = c.get(path)
    assert r.status_code == 200, (path, r.text[:300])


def test_library_without_arrs_explains(ui):
    _, c, _ = ui
    t = c.get("/library").text
    assert "Sonarr: not configured" in t and "Radarr: not configured" in t


def test_cross_site_post_refused(ui):
    _, c, _ = ui
    assert c.post("/settings", data={"languages": "de"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/settings", data={"languages": "de"}).status_code == 403          # no Origin/Referer at all
    assert c.post("/jobs/resume", headers={"Origin": "http://evil.example"}).status_code == 403


def test_settings_save_and_validate(ui):
    app, c, _ = ui
    form = {"languages": "ru, uk", "model": "claude-sonnet-5-5", "effort": "low", "tone": "softened", "songs": "verse",
            "monthly_cap_usd": "15", "alert_pct": "70", "max_line": "40", "target_cps": "18",
            "jellyfin_path_map": "/downloads/=/mnt/media/"}
    r = c.post("/settings", data=form, headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and "saved" in r.headers["location"]
    s = app.state.settings
    assert s.languages == ["ru", "uk"] and s.model == "claude-sonnet-5-5" and s.monthly_cap_usd == 15
    assert s.alert_at == 0.7 and s.jellyfin_path_map == [("/downloads/", "/mnt/media/")]
    # persisted: a fresh app over the same data dir sees the saved values
    s2 = Settings(data_dir=s.data_dir, webhook_key=KEY)
    create_app(s2, provider_factory=FakeProvider, start_worker=False)
    assert s2.languages == ["ru", "uk"] and s2.tone == "softened"
    bad = dict(form, languages="russian!!")
    r = c.post("/settings", data=bad, headers=SAME, follow_redirects=False)
    assert "Not+saved" in r.headers["location"] and app.state.settings.languages == ["ru", "uk"]
    bad = dict(form, model="gpt-9")
    assert "Not+saved" in c.post("/settings", data=bad, headers=SAME, follow_redirects=False).headers["location"]


def test_series_settings_and_notes(ui):
    app, c, _ = ui
    r = c.post("/library/series/40/settings", data={"title": "Northwind", "enabled": "", "langs": "uk", "tone": "family",
               "context": "# Northwind\nLou = Анна"}, headers=SAME, follow_redirects=False)
    assert r.status_code == 303
    ser = app.state.store.series("northwind")
    assert ser == {"key": "northwind", "title": "Northwind", "enabled": False, "langs": ["uk"], "tone": "family"}
    assert (app.state.settings.context_dir / "northwind.md").read_text().startswith("# Northwind")
    # disabled series: webhooks are ignored
    p = "/x/Northwind/Season 2/Northwind - S02E01.mkv"
    hook = {"eventType": "Download", "series": {"title": "Northwind", "path": "/x/Northwind"},
            "episodes": [{"seasonNumber": 2, "episodeNumber": 1}], "episodeFile": {"path": p}}
    assert c.post(f"/api/hooks/sonarr?apikey={KEY}", json=hook).json()["queued"] == []


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
def test_translate_selected_only_library_videos(ui, tmp_path):
    app, c, _ = ui
    video = make_video(tmp_path / "lib/Show/Season 1", "Show - S01E01.mkv")
    outside = tmp_path / "elsewhere.mkv"
    outside.write_bytes(b"x")
    r = c.post("/library/translate", data={"path": [str(video), str(outside), "/etc/passwd"], "series_key": "show",
               "title": "Show", f"label:{video}": "S01E01"}, headers=SAME, follow_redirects=False)
    assert "Queued+1+job" in r.headers["location"]
    jobs = app.state.store.list()
    assert len(jobs) == 1 and jobs[0].title == "Show S01E01" and jobs[0].video_path == str(video)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
def test_worker_refuses_outside_library(ui, tmp_path):
    app, c, _ = ui
    outside = make_video(tmp_path / "elsewhere", "X.mkv")
    app.state.store.enqueue("manual", "X", str(outside), "ru", None)
    app.state.worker.process(app.state.store.next_queued())
    j = app.state.store.list()[0]
    assert j.state == "failed" and "library roots" in j.detail
    assert not list(outside.parent.glob("*.captionizerr.*"))


def test_retry_and_job_page(ui):
    app, c, _ = ui
    job = app.state.store.enqueue("manual", "T", "/lib/a.mkv", "ru", None)
    app.state.store.finish(job.id, "failed", "boom")
    assert "boom" in c.get(f"/jobs/{job.id}").text
    r = c.post(f"/jobs/{job.id}/retry", headers=SAME, follow_redirects=False)
    assert "queued+again" in r.headers["location"] and app.state.store.get(job.id).state == "queued"


def test_provider_test_button(ui):
    _, c, _ = ui
    r = c.post("/providers/test", headers=SAME, follow_redirects=False)
    assert "Anthropic+works" in r.headers["location"]


# ------------------------------------------------------------------ M3: editor, notes, feedback
from captionizerr import bible, editor
from captionizerr.cues import load as load_subs
from captionizerr.writer import WriteRefused, history, replace_own


def test_replace_own_guards_and_keeps_history(tmp_path):
    f = tmp_path / "Show S01E01.captionizerr.ru.srt"
    f.write_text("1\n00:00:01,000 --> 00:00:02,000\nстарое\n", encoding="utf-8")
    replace_own(f, "1\n00:00:01,000 --> 00:00:02,000\nновое\n", tmp_path / "hist")
    assert "новое" in f.read_text(encoding="utf-8")
    h = history(f, tmp_path / "hist")
    assert len(h) == 1 and "старое" in h[0].read_text(encoding="utf-8")
    other = tmp_path / "Show S01E01.ru.srt"
    other.write_text("x", encoding="utf-8")
    with pytest.raises(WriteRefused):
        replace_own(other, "y", tmp_path / "hist")
    with pytest.raises(WriteRefused):
        replace_own(tmp_path / "Missing.captionizerr.ru.srt", "y", tmp_path / "hist")
    assert other.read_text() == "x"


def _srt(tmp_path, name, items):
    p = tmp_path / name
    p.write_text("\n".join(f"{i}\n00:00:{s:02d},000 --> 00:00:{e:02d},000\n{t}\n" for i, (s, e, t) in enumerate(items, 1)),
                 encoding="utf-8")
    return load_subs(p)


def test_build_rows_matches_by_time_and_keeps_extras(tmp_path):
    src = _srt(tmp_path, "en.srt", [(1, 2, "Hello."), (3, 4, "Where are you?"), (5, 6, "Bye.")])
    tr = _srt(tmp_path, "ru.srt", [(1, 2, "Привет."), (5, 6, "Пока."), (8, 9, "Лишняя реплика.")])
    rows = editor.build_rows(src, tr)
    assert [(r.src, r.tr, r.extra) for r in rows] == [
        ("Hello.", "Привет.", False), ("Where are you?", "", False), ("Bye.", "Пока.", False), ("", "Лишняя реплика.", True)]
    out = editor.to_srt(rows)
    assert out.count("-->") == 3 and "Лишняя реплика." in out      # empty row skipped, extra kept


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_editor_save_and_retranslate(ui):
    app, c, tmp = ui
    video = make_video(tmp / "lib" / "Show" / "Season 1", "Show S01E01.mkv")
    target = video.with_name("Show S01E01.captionizerr.ru.srt")
    from test_service import SRT
    target.write_text(SRT.replace("Hello", "Привет"), encoding="utf-8")
    q = {"path": str(video), "lang": "ru", "key": "show"}
    r = c.get("/edit", params=q)
    assert r.status_code == 200 and "Edit translation" in r.text
    import re as _re
    token = _re.search(r'let token = "(\d+)"', r.text).group(1)
    # cross-site refused
    assert c.post("/api/edit/save", json={**q, "token": token, "edits": {"1": "x"}}).status_code == 403
    r = c.post("/api/edit/save", json={**q, "token": token, "edits": {"1": "Исправлено."}}, headers=SAME)
    assert r.status_code == 200 and r.json()["saved"] == 1, r.text
    assert "Исправлено." in target.read_text(encoding="utf-8")
    assert history(target, app.state.settings.history_dir)
    assert bible.corrections(app.state.settings.feedback_dir, "show")[0]["after"] == "Исправлено."
    # stale token
    assert c.post("/api/edit/save", json={**q, "token": token, "edits": {"1": "y"}}, headers=SAME).status_code == 409
    # re-translate one line, cost recorded
    r = c.post("/api/edit/retranslate", json={**q, "id": 2, "instruction": "short"}, headers=SAME)
    assert r.status_code == 200 and "Строка номер 2" in r.json()["text"], r.text
    assert any(j.source == "edit" for j in app.state.store.list())
    # never outside the library
    assert c.get("/edit", params={**q, "path": "/etc/passwd"}).status_code == 404


def test_generate_notes_prompt_and_parse():
    class P:
        model = "claude-opus-5-5"
        def complete(self, system, context, work):
            from captionizerr.providers.base import ProviderResult
            from captionizerr.pricing import Usage
            assert "TARGET LANGUAGE: Russian" in work and "Hello there" in work
            return ProviderResult("```markdown\n# Show\nNotes.\n```", Usage(10, 10), self.model, "end_turn")
    from captionizerr.cues import Cue
    text, _ = bible.generate_notes(P(), "Show", "A show.", "ru", [Cue(1, 0, 1000, "Hello there")])
    assert text.startswith("# Show") and "```" not in text


def test_optional_login(tmp_path):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, ui_password="pw", library_roots=[str(tmp_path / "lib")])
    c = TestClient(create_app(s, provider_factory=FakeProvider, start_worker=False))
    assert c.get("/").status_code == 401 and c.get("/settings").status_code == 401
    assert c.get("/", auth=("admin", "wrong")).status_code == 401
    assert c.get("/", auth=("admin", "pw")).status_code == 200
    assert c.get("/health").status_code == 200 and c.get("/metrics").status_code == 200
    assert c.get("/static/favicon.svg").status_code == 200
    assert c.post(f"/api/hooks/sonarr?apikey={KEY}", json={"eventType": "Test"}).status_code == 200
    assert c.get("/api/jobs").status_code == 401 and c.get(f"/api/jobs?apikey={KEY}").status_code == 200
