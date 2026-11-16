
import urllib.parse
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


@pytest.mark.parametrize("path", ["/", "/library", "/settings/general", "/settings/ai", "/settings/connections",
                                  "/settings/security", "/settings/system", "/setup", "/setup?step=ai",
                                  "/setup?step=languages", "/setup?step=arr", "/setup?step=media", "/setup?step=done",
                                  "/static/favicon.svg", "/static/mark.svg"])
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
    assert c.post("/settings/general", data={"languages": "de"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/settings/general", data={"languages": "de"}).status_code == 403  # no Origin/Referer at all
    assert c.post("/settings/connections", data={"sonarr_url": "http://x"}).status_code == 403
    assert c.post("/jobs/resume", headers={"Origin": "http://evil.example"}).status_code == 403


def test_settings_save_and_validate(ui):
    app, c, _ = ui
    form = {"languages": "ru, uk", "source_langs": "en", "any_source": "1", "tone": "softened", "songs": "verse",
            "max_line": "40", "target_cps": "18", "monthly_cap_usd": "15", "alert_pct": "70"}
    r = c.post("/settings/general", data=form, headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and "Saved" in r.headers["location"]
    s = app.state.settings
    assert s.languages == ["ru", "uk"] and s.monthly_cap_usd == 15 and s.alert_at == 0.7 and s.tone == "softened"
    c.post("/settings/ai", data={"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "low"}, headers=SAME)
    assert s.model == "claude-sonnet-5-5"
    c.post("/settings/connections", data={"jellyfin_url": "http://jf:8096", "jellyfin_path_map": "/downloads/=/mnt/media/"},
           headers=SAME)
    assert s.jellyfin_path_map == [("/downloads/", "/mnt/media/")]
    s2 = Settings(data_dir=s.data_dir, webhook_key=KEY)          # persisted across a restart
    create_app(s2, provider_factory=FakeProvider, start_worker=False)
    assert s2.languages == ["ru", "uk"] and s2.tone == "softened" and s2.model == "claude-sonnet-5-5"
    r = c.post("/settings/general", data=dict(form, languages="russian!!"), headers=SAME, follow_redirects=False)
    assert "Not+saved" in r.headers["location"] and s.languages == ["ru", "uk"]
    r = c.post("/settings/ai", data={"provider": "anthropic", "model": "gpt-9"}, headers=SAME, follow_redirects=False)
    assert "Not+saved" in r.headers["location"]
    r = c.post("/settings/connections", data={"sonarr_url": "ftp://x"}, headers=SAME, follow_redirects=False)
    assert "Not+saved" in r.headers["location"]


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
    r = c.post("/settings/test/sonarr", data={}, headers=SAME)
    assert r.json()["ok"] is False and r.json()["message"] == "enter the address and the API key"
    r = c.post("/settings/test/anthropic", data={}, headers=SAME)          # no key anywhere
    assert r.status_code == 200 and r.json()["ok"] is False
    assert c.post("/settings/test/nope", data={}, headers=SAME).status_code == 404


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
    assert r.status_code == 200 and "Season 1, Episode 1" in r.text
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
    assert c.get("/").status_code == 401 and c.get("/settings/general").status_code == 401
    assert c.get("/", auth=("admin", "wrong")).status_code == 401
    assert c.get("/", auth=("admin", "pw")).status_code == 200
    assert c.get("/health").status_code == 200 and c.get("/metrics").status_code == 200
    assert c.get("/static/favicon.svg").status_code == 200
    assert c.post(f"/api/hooks/sonarr?apikey={KEY}", json={"eventType": "Test"}).status_code == 200
    assert c.get("/api/jobs").status_code == 401 and c.get(f"/api/jobs?apikey={KEY}").status_code == 200


def test_settings_show_and_reset_overrides(ui):
    app, c, _ = ui
    s = app.state.settings
    c.post("/settings/general", data={"languages": "de"}, headers=SAME)
    assert s.languages == ["de"]
    page = c.get("/settings/system").text
    assert "Changed here" in page and "languages" in page
    c.post("/settings/reset", data={"key": "languages"}, headers=SAME)
    assert s.languages == ["ru"] and "languages" not in app.state.store.load_settings()


def test_secrets_entered_in_the_ui(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path / "config", languages=["ru"], library_roots=[str(tmp_path / "lib")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    assert s.webhook_key and s.secret_sources["webhook_key"] == "ui"          # generated on first run
    r = c.post("/settings/connections", data={"sonarr_url": "http://sonarr:8989", "secret:sonarr_key": "SONARR-SECRET"},
               headers=SAME, follow_redirects=False)
    assert "Saved" in r.headers["location"] and s.sonarr_key == "SONARR-SECRET"
    f = tmp_path / "config/secrets.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600" and "SONARR-SECRET" in f.read_text()
    assert "SONARR-SECRET" not in str(app.state.store.load_settings())         # never in the database
    page = c.get("/settings/connections").text
    assert "SONARR-SECRET" not in page and "Saved" in page                    # never rendered back
    s2 = Settings(data_dir=s.data_dir)                                         # survives a restart
    create_app(s2, provider_factory=FakeProvider, start_worker=False)
    assert s2.sonarr_key == "SONARR-SECRET" and s2.webhook_key == s.webhook_key
    c.post("/settings/secret/sonarr_key/remove", data={"next": "/settings/connections"}, headers=SAME)
    assert s.sonarr_key == "" and "SONARR-SECRET" not in f.read_text()
    monkeypatch.setenv("CAPTIONIZERR_RADARR_KEY", "from-env")                  # set on the server: read-only here
    s3 = Settings.from_env()
    s3.data_dir, s3.library_roots = tmp_path / "c3", [str(tmp_path / "lib")]
    c3 = TestClient(create_app(s3, provider_factory=FakeProvider, start_worker=False))
    c3.post("/settings/connections", data={"secret:radarr_key": "typed"}, headers=SAME)
    assert s3.radarr_key == "from-env" and "Set on the server" in c3.get("/settings/connections").text


def test_ui_password_set_in_the_ui_is_hashed(ui):
    app, c, _ = ui
    r = c.post("/settings/security", data={"ui_user": "me", "secret:ui_password": "short", "password_again": "short"},
               headers=SAME, follow_redirects=False)
    assert "Password+not+set" in r.headers["location"]
    c.post("/settings/security", data={"ui_user": "me", "secret:ui_password": "long enough pw",
                                       "password_again": "long enough pw"}, headers=SAME)
    stored = (app.state.settings.data_dir / "secrets.json").read_text()
    assert "long enough pw" not in stored and "pbkdf2_sha256$" in stored
    assert c.get("/").status_code == 401
    assert c.get("/", auth=("me", "long enough pw")).status_code == 200


def test_setup_wizard_flow(tmp_path):
    s = Settings(data_dir=tmp_path / "config", library_roots=[str(tmp_path / "lib")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/setup"           # fresh install
    r = c.post("/settings/ai", data={"provider": "anthropic", "model": "claude-opus-5-5", "effort": "low",
                                     "secret:anthropic_key": "sk-ant-x", "next": "/setup?step=languages"},
               headers=SAME, follow_redirects=False)
    assert r.headers["location"].startswith("/setup?step=languages")
    c.post("/settings/general", data={"languages": "de", "source_langs": "en", "monthly_cap_usd": "10",
                                      "alert_pct": "80", "next": "/setup?step=arr"}, headers=SAME)
    assert s.languages == ["de"] and s.anthropic_key == "sk-ant-x"
    assert "Ready" in c.get("/setup?step=done").text
    c.post("/setup/finish", headers=SAME)
    assert c.get("/", follow_redirects=False).status_code == 200


def test_connect_arr_creates_webhook(monkeypatch):
    from captionizerr import connections
    calls = []

    def fake(url, headers=None, method="GET", body=None, timeout=10):
        calls.append((method, url, body))
        if url.endswith("/notification") and method == "GET":
            return 200, []
        if url.endswith("/notification/schema"):
            return 200, [{"implementation": "Webhook", "onDownload": False, "onUpgrade": False, "onRename": False,
                          "onHealthIssue": False, "fields": [{"name": "url"}, {"name": "method"},
                                                              {"name": "username"}, {"name": "password"}]}]
        if url.endswith("/notification") and method == "POST":
            return 201, {"id": 7}
        return 200, {}
    monkeypatch.setattr(connections, "_req", fake)
    ok, msg = connections.connect_arr("sonarr", "http://sonarr:8989", "k", "http://cz:8095/api/hooks/sonarr", "WK")
    assert ok and "created" in msg
    created = [b for m, u, b in calls if m == "POST" and u.endswith("/notification")][0]
    f = {x["name"]: x.get("value") for x in created["fields"]}
    assert f == {"url": "http://cz:8095/api/hooks/sonarr", "method": 1, "username": "sonarr", "password": "WK"}
    assert created["onDownload"] and created["onUpgrade"] and created["onRename"] and not created["onHealthIssue"]
    assert any(u.endswith("/notification/test") for _, u, _ in calls)


def test_webhook_basic_auth_and_lockout(tmp_path):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, ui_password="pw", library_roots=[str(tmp_path / "lib")])
    c = TestClient(create_app(s, provider_factory=FakeProvider, start_worker=False))
    test = {"eventType": "Test"}
    assert c.post("/api/hooks/sonarr", json=test, auth=("sonarr", KEY)).status_code == 200      # Basic, any user
    assert c.post("/api/hooks/sonarr", json=test, headers={"X-Api-Key": KEY}).status_code == 200
    assert c.post("/api/hooks/sonarr", json=test, auth=("sonarr", "nope")).status_code == 401
    for _ in range(10):
        c.get("/", auth=("admin", "wrong"))
    assert c.get("/", auth=("admin", "pw")).status_code == 429          # locked even with the right password
    assert c.post(f"/api/hooks/sonarr?apikey={KEY}", json=test).status_code == 429


def test_secrets_from_files(tmp_path, monkeypatch):
    f = tmp_path / "wk"
    f.write_text("from-file\n")
    monkeypatch.setenv("CAPTIONIZERR_WEBHOOK_KEY_FILE", str(f))
    monkeypatch.setenv("CAPTIONIZERR_WEBHOOK_KEY", "from-env")
    a = tmp_path / "ak"
    a.write_text("sk-ant-file")
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(a))
    s = Settings.from_env()
    assert s.webhook_key == "from-file" and s.anthropic_key == "sk-ant-file"
    assert "from-file" not in repr(s) and "sk-ant" not in repr(s)
    monkeypatch.setenv("CAPTIONIZERR_WEBHOOK_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_editor_doesnt_flag_dropped_credits():
    from captionizerr.editor import Row
    from captionizerr.profile import Profile
    p = Profile()
    assert Row(1, 0, 3000, "- synced and corrected by chamallow -\n- www.addic7ed.com -", "").flags(p) == []
    assert Row(2, 0, 3000, "-", "").flags(p) == []
    assert Row(3, 0, 3000, "Where are you going?", "").flags(p) == ["empty"]


def test_series_key_cannot_escape_folders(tmp_path):
    from captionizerr import bible
    from captionizerr.hooks import valid_key
    assert valid_key("the-long-road-home-part-two") and valid_key("northwind")
    for bad in ("../x", "..", "a/b", "a\\b", "", None, "Northwind", "x" * 200, "a--b", "-a"):
        assert valid_key(bad) is None, bad
    fb = tmp_path / "feedback"
    bible.log_corrections(fb, "../../escaped", [{"src": "a", "before": "b", "after": "c"}])
    assert not (tmp_path / "escaped.jsonl").exists() and not list(tmp_path.rglob("*.jsonl"))
    assert bible.corrections(fb, "../../etc/passwd") == []


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_editor_ignores_crafted_series_key(ui):
    app, c, tmp = ui
    video = make_video(tmp / "lib" / "Show" / "Season 1", "Show S01E01.mkv")
    target = video.with_name("Show S01E01.captionizerr.ru.srt")
    from test_service import SRT
    target.write_text(SRT, encoding="utf-8")
    import re as _re
    token = _re.search(r'let token = "(\d+)"', c.get("/edit", params={"path": str(video), "lang": "ru"}).text).group(1)
    r = c.post("/api/edit/save", json={"path": str(video), "lang": "ru", "key": "../../../evil", "token": token,
                                       "edits": {"1": "x"}}, headers=SAME)
    assert r.status_code == 200
    assert not list(tmp.rglob("evil*")) and not (app.state.settings.feedback_dir / "../../../evil.jsonl").exists()


def test_redirect_targets_stay_on_site():
    from captionizerr.web.ui import local_path
    assert local_path("/library/series/40") == "/library/series/40"
    for bad in ("//evil.com", "/\\evil.com", "/\\/evil.com", "https://evil.com", "javascript:alert(1)",
                "/ok\\..\\x", "/a\r\nLocation: x", "", None):
        assert local_path(bad) == "/", bad


def test_translate_back_param_is_sanitised(ui):
    _, c, _ = ui
    r = c.post("/library/translate", data={"back": "/\\evil.com"}, headers=SAME, follow_redirects=False)
    assert r.headers["location"].startswith("/library") or r.headers["location"].startswith("/?")


def test_translations_list_shows_the_model(ui):
    app, c, _ = ui
    st = app.state.store
    st.save_settings({"_setup_done": True})          # the test app has no API key, so / would open the wizard
    j = st.enqueue("manual", "Show S01E01", "/x/a.mkv", "ru", None)
    st.finish(j.id, "done", "ok", "", 0.31, {}, "claude-opus-5-5")
    k = st.enqueue("manual", "Show S01E02", "/x/b.mkv", "ru", None)
    st.finish(k.id, "done", "ok", "", 0.008, {}, "gpt-6-luna")
    page = c.get("/").text
    assert "<th>Model</th>" in page and ">opus-5-5<" in page and ">gpt-6-luna<" in page
    assert "gpt-6-luna" in c.get(f"/jobs/{k.id}").text


def test_changed_here_uses_readable_names(ui):
    app, c, _ = ui
    app.state.store.save_settings({"_setup_done": True, "openai_price_in": 0.1})
    app.state.settings.openai_price_in = 0.1
    page = c.get("/settings/system").text
    assert "Input price ($/1M tokens)" in page and ">openai price in<" not in page


def test_replaced_translation_has_no_edit_and_points_to_newer(ui):
    app, c, _ = ui
    st = app.state.store
    st.save_settings({"_setup_done": True})
    a = st.enqueue("manual", "Show S01E01", "/x/a.mkv", "ru", None); st.finish(a.id, "done", "first", "/x/a.captionizerr.ru.srt", 0.003, {}, "gpt-6-luna")
    b = st.enqueue("manual", "Show S01E01", "/x/a.mkv", "ru", None, redo=True); st.finish(b.id, "done", "second", "/x/a.captionizerr.ru.srt", 0.3, {}, "claude-opus-5-5")
    page = c.get("/").text                                    # hidden by default, with a way to show it
    assert "Replaced by a newer translation" not in page and "1 older translation that were translated again is hidden" not in page
    assert "1 older translation" in page and "replaced=1" in page and "$0.30" in page and "$0.003" not in page
    page = c.get("/?replaced=1").text
    assert page.count("Replaced by a newer translation") == 1 and page.count(">Edit<") == 1
    assert "$0.003" in page and "$0.30" in page
    jp = c.get(f"/jobs/{a.id}").text
    assert f'href="/jobs/{b.id}"' in jp and "Open in editor" not in jp
    assert "Open in editor" in c.get(f"/jobs/{b.id}").text


def test_deleted_video_is_marked_and_loses_actions(ui):
    app, c, tmp = ui
    st = app.state.store
    st.save_settings({"_setup_done": True})
    lib = tmp / "lib" / "Show"
    lib.mkdir(parents=True)
    kept = lib / "kept.mkv"; kept.write_bytes(b"x")
    gone = str(lib / "gone.mkv")
    a = st.enqueue("manual", "Gone S01E01", gone, "ru", None); st.finish(a.id, "done", "ok", gone[:-4] + ".captionizerr.ru.srt", 0.3, {}, "m")
    b = st.enqueue("manual", "Kept S01E01", str(kept), "ru", None); st.finish(b.id, "done", "ok", str(kept)[:-4] + ".captionizerr.ru.srt", 0.3, {}, "m")
    f = st.enqueue("manual", "Gone S01E02", str(lib / "gone2.mkv"), "ru", None); st.finish(f.id, "failed", "boom")
    page = c.get("/").text
    assert page.count("Video deleted") == 2 and page.count(">Edit<") == 1 and page.count(">Retry<") == 0
    jp = c.get(f"/jobs/{a.id}").text
    assert "Video deleted" in jp and "Open in editor" not in jp and "Re-translate" not in jp
    assert "Video deleted" in c.get(f"/jobs/{f.id}").text and ">Retry<" not in c.get(f"/jobs/{f.id}").text
    assert "Open in editor" in c.get(f"/jobs/{b.id}").text
    r = c.post(f"/jobs/{a.id}/retranslate", headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert "deleted" in urllib.parse.unquote_plus(r.headers["location"]) and st.counts().get("queued", 0) == 0


def test_unreachable_library_marks_nothing_deleted(ui):
    app, c, tmp = ui
    st = app.state.store
    st.save_settings({"_setup_done": True})
    p = str(tmp / "lib" / "Show" / "e.mkv")          # library root missing, e.g. the share isn't mounted
    a = st.enqueue("manual", "Show S01E01", p, "ru", None); st.finish(a.id, "done", "ok", p[:-4] + ".captionizerr.ru.srt", 0.3, {}, "m")
    page = c.get("/").text
    assert "Video deleted" not in page and ">Edit<" in page


def test_notes_for_removed_series_listed_and_deleted(ui, monkeypatch):
    from captionizerr import maintenance
    from captionizerr.arr import ArrError
    app, c, _ = ui
    s, st = app.state.settings, app.state.store
    st.save_settings({"_setup_done": True})
    s.sonarr_url, s.sonarr_key = "http://sonarr.invalid", "k" * 32
    s.context_dir.mkdir(parents=True, exist_ok=True); s.feedback_dir.mkdir(parents=True, exist_ok=True)
    (s.context_dir / "northwind.md").write_text("notes")
    (s.context_dir / "gone-show.md").write_text("old notes")
    (s.feedback_dir / "gone-show.jsonl").write_text('{"a":1}\n{"a":2}\n')
    st.save_series("gone-show", "Gone Show's Title", True, ["ru"], "")
    known = {"northwind"}
    monkeypatch.setattr(maintenance, "known_keys", lambda _s: known)
    page = c.get("/settings/system").text
    assert "Notes for removed series" in page and "gone-show" not in page          # nothing checked yet
    page = c.get("/settings/system?notes=1").text
    assert "Gone Show&#39;s Title" in page and ">northwind<" not in page and ">2<" in page
    o = {"Origin": "http://testserver"}
    known.add("gone-show")                                                          # re-added meanwhile
    c.post("/settings/notes/forget", data={"key": "gone-show"}, headers=o)
    assert (s.context_dir / "gone-show.md").exists()
    known.discard("gone-show")
    assert "Unknown series" in c.post("/settings/notes/forget", data={"key": "../etc"}, headers=o).text
    c.post("/settings/notes/forget", data={"key": "gone-show"}, headers=o)
    assert not (s.context_dir / "gone-show.md").exists() and not (s.feedback_dir / "gone-show.jsonl").exists()
    assert st.series("gone-show") is None and (s.context_dir / "northwind.md").exists()
    assert "None: every series" in c.get("/settings/system?notes=1").text

    def down(_s):
        raise ArrError("timed out")
    monkeypatch.setattr(maintenance, "known_keys", down)
    (s.context_dir / "x.md").write_text("n")
    assert "Couldn't reach" in c.get("/settings/system?notes=1").text
    c.post("/settings/notes/forget", data={"key": "x"}, headers=o)
    assert (s.context_dir / "x.md").exists()
