"""M4: sources, ASS, chunking, Batch backlog, Bazarr wait, housekeeping, local and OpenAI-compatible providers."""
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from captionizerr import cues as cuesmod, editor, langs, maintenance, media
from captionizerr.app import create_app
from captionizerr.config import Settings
from captionizerr.pipeline import translate
from captionizerr.pricing import Usage
from captionizerr.profile import Profile
from captionizerr.providers.base import ProviderError, ProviderResult

from test_service import HAS_FFMPEG, KEY, SRT, FakeProvider, make_video

SAME = {"Origin": "http://testserver"}
ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")


def video_without_subs(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    v = folder / name
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=5",
                    "-c:v", "libx264", str(v)], check=True)
    return v


def old(p: Path, age: float = 3600):
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], library_roots=[str(tmp_path / "lib")],
                    **kw)


# ------------------------------------------------------------------ languages and sources
def test_lang_codes():
    assert langs.norm("eng") == "en" and langs.norm("GER") == "de" and langs.norm("pt-BR") == "pt"
    assert langs.norm("und") is None and langs.same("jpn", "ja") and not langs.same(None, None)


@ffmpeg
def test_find_source_prefers_embedded_then_sidecar_never_target(tmp_path):
    v = make_video(tmp_path / "lib/Show", "Show S01E01.mkv")
    src = media.find_source(v, tmp_path / "cache", ["en"], "ru")
    assert src.origin == "embedded" and src.lang == "en"
    # English target: the embedded English track is not a source; a Japanese sidecar is
    (v.parent / "Show S01E01.ja.srt").write_text(SRT, encoding="utf-8")
    (v.parent / "Show S01E01.captionizerr.de.srt").write_text(SRT, encoding="utf-8")   # ours: never a source
    src = media.find_source(v, tmp_path / "cache", ["en"], "en")
    assert src.origin == "sidecar" and src.lang == "ja"
    assert media.find_source(v, tmp_path / "cache", ["en"], "en", any_language=False) is None
    # second call hits the cache (no new files)
    n = len(list((tmp_path / "cache").iterdir()))
    media.find_source(v, tmp_path / "cache", ["en"], "ru")
    assert len(list((tmp_path / "cache").iterdir())) == n


# ------------------------------------------------------------------ ASS / anime
ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,20,20,40,1
Style: Sign,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,8,20,20,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Where are you going?
Dialogue: 0,0:00:04.00,0:00:06.00,Sign,,0,0,0,,{\\pos(960,100)}TOKYO STATION
Dialogue: 0,0:00:04.00,0:00:06.00,Sign,,0,0,0,,{\\p1}m 0 0 l 100 0 100 100 0 100{\\p0}
Dialogue: 0,0:00:07.00,0:00:09.00,Default,,0,0,0,,{\\k20}ka{\\k30}ze{\\k25}ga
Comment: 0,0:00:07.00,0:00:09.00,Default,,0,0,0,,timing note
Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,I'll be\\Nright back.
"""


def test_ass_keeps_drawings_karaoke_styles(tmp_path):
    p = tmp_path / "ep.ass"
    p.write_text(ASS, encoding="utf-8")
    doc = cuesmod.load(p)
    sendable = [c for c in doc.cues if not c.is_tag_only]
    assert [c.text for c in sendable] == ["Where are you going?", "TOKYO STATION", "I'll be\nright back."]
    out = cuesmod.render(doc, {sendable[0].index: "Куда ты?", sendable[1].index: "ВОКЗАЛ ТОКИО",
                               sendable[2].index: "Я скоро\nвернусь."})
    assert "{\\pos(960,100)}ВОКЗАЛ ТОКИО" in out and "{\\p1}m 0 0 l 100" in out and "{\\k20}ka" in out
    assert "Я скоро\\Nвернусь." in out and "Style: Sign" in out and "PlayResX: 1920" in out
    assert "timing note" in out                              # comments survive


def test_editor_saves_ass_without_losing_events(tmp_path):
    src = tmp_path / "en.ass"
    src.write_text(ASS, encoding="utf-8")
    tr = tmp_path / "ru.ass"
    tr.write_text(cuesmod.render(cuesmod.load(src), {1: "Куда ты?", 2: "ВОКЗАЛ", 5: "Я скоро вернусь."}), encoding="utf-8")
    tdoc = cuesmod.load(tr)
    rows = editor.build_rows(cuesmod.load(src), tdoc)
    shown = [r for r in rows if not r.hidden]
    assert [r.tr for r in shown] == ["Куда ты?", "ВОКЗАЛ", "Я скоро вернусь."]
    shown[0].tr = "Ты куда?"
    out = editor.save_text(tdoc, rows)
    assert "Ты куда?" in out and "{\\p1}m 0 0" in out and "{\\k20}ka" in out and "{\\pos(960,100)}ВОКЗАЛ" in out


def test_cjk_targets_get_shorter_lines():
    s = Settings()
    assert s.profile("ja").max_line == 16 and s.profile("ru").max_line == 42


# ------------------------------------------------------------------ pipeline: chunks and batch results
def _doc(tmp_path, n=10):
    p = tmp_path / "x.srt"
    p.write_text("".join(f"{i}\n00:00:{i * 3:02d},000 --> 00:00:{i * 3 + 2:02d},500\nLine number {i} here.\n\n"
                         for i in range(1, n + 1)), encoding="utf-8")
    return cuesmod.load(p)


def test_chunked_translation_covers_every_cue(tmp_path):
    class Small(FakeProvider):
        max_cues = 4
        calls = []

        def complete(self, system, context, work):
            Small.calls.append(work)
            return super().complete(system, context, work)

    res = translate(_doc(tmp_path), Profile(), Small(), log=lambda m: None)
    assert len(res.translations) == 10 and len(Small.calls) == 3
    assert "PREVIOUS CUES" in Small.calls[1] and "#4 " in Small.calls[1]


def test_batch_result_is_priced_half(tmp_path):
    doc = _doc(tmp_path, 4)
    first = ProviderResult("\n".join(f"{i}|Строка номер {i} тестового эпизода." for i in range(1, 5)),
                           Usage(10_000, 4_000), "claude-opus-5-5", "end_turn")
    res = translate(doc, Profile(), FakeProvider(), log=lambda m: None, first=first)
    assert res.steps[0].batch and len(res.steps) == 1
    assert abs(res.cost() - (10_000 * 4 + 4_000 * 20) / 1e6 / 2) < 1e-9


# ------------------------------------------------------------------ worker: Batch backlog end to end
class FakeBatch(FakeProvider):
    supports_batch = True
    batches: dict = {}

    def batch_params(self, system, context, work):
        return {"system": system, "context": context, "work": work}

    def submit_batch(self, items):
        bid = f"msgbatch_{len(FakeBatch.batches) + 1}"
        FakeBatch.batches[bid] = items
        return bid

    def batch_status(self, bid):
        return "ended"

    def batch_results(self, bid):
        out = {}
        for cid, params in FakeBatch.batches[bid]:
            out[cid] = (ProviderError("batch request expired") if "Bad" in params["work"]
                        else self.complete(params["system"], params["context"], params["work"]))
        return out


@ffmpeg
def test_backlog_goes_through_batch(tmp_path):
    s = settings(tmp_path, batch_min_wait_s=0)
    app = create_app(s, provider_factory=FakeBatch, start_worker=False)
    store, w = app.state.store, app.state.worker
    good = make_video(tmp_path / "lib/Show/Season 1", "Show S01E01.mkv")
    bad = make_video(tmp_path / "lib/Show/Season 1", "Bad S01E02.mkv")
    j1 = store.enqueue("manual", "Show S01E01", str(good), "ru", None, "batch")
    j2 = store.enqueue("manual", "Bad S01E02", str(bad), "ru", None, "batch")
    assert store.next_queued() is None                       # batch jobs never run live on a batch provider
    w.submit_batch()
    assert {store.get(j1.id).state, store.get(j2.id).state} == {"batched"}
    w.poll_batches()
    done = store.get(j1.id)
    assert done.state == "done" and "via Batch" in done.detail, done.detail
    assert good.with_name("Show S01E01.captionizerr.ru.srt").is_file()
    again = store.get(j2.id)                                  # expired in the batch -> retried live
    assert again.state == "queued" and again.mode == "live"
    w.process(store.next_queued())
    assert store.get(j2.id).state == "done"


@ffmpeg
def test_batch_mode_runs_live_without_batch_api(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    v = make_video(tmp_path / "lib/Show", "Show S01E01.mkv")
    app.state.store.enqueue("manual", "x", str(v), "ru", None, "batch")
    assert app.state.store.next_queued(include_batch=not app.state.worker.batch_capable()) is not None


# ------------------------------------------------------------------ worker: Bazarr fallback
@ffmpeg
def test_waits_for_bazarr_then_translates_the_sidecar(tmp_path, monkeypatch):
    s = settings(tmp_path, bazarr_url="http://bazarr", bazarr_key="k")
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = video_without_subs(tmp_path / "lib/Show", "Show S01E01.mkv")
    asked = []
    monkeypatch.setattr(w, "_bazarr_request", lambda bz, job: asked.append(job.id) or True)
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    assert store.get(job.id).state == "waiting_source" and asked == [job.id]
    side = v.with_name("Show S01E01.en.srt")
    side.write_text(SRT, encoding="utf-8")
    w.check_waiting()
    assert store.get(job.id).state == "waiting_source"        # brand new: Bazarr may still be syncing
    old(side)
    w.check_waiting()
    assert store.get(job.id).state == "queued"
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state == "done" and "(sidecar)" in j.detail and asked == [job.id]


@ffmpeg
def test_no_bazarr_means_no_source(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    v = video_without_subs(tmp_path / "lib/Show", "Show S01E01.mkv")
    job = app.state.store.enqueue("sonarr", "x", str(v), "ru", None)
    app.state.worker.process(app.state.store.next_queued())
    assert app.state.store.get(job.id).state == "no_source"


# ------------------------------------------------------------------ housekeeping
def test_history_cache_and_jobs_cleanup(tmp_path):
    s = settings(tmp_path, keep_history=2)
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    target = tmp_path / "lib/Show/E1.captionizerr.ru.srt"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    from captionizerr.writer import history, replace_own
    for i in range(4):
        replace_own(target, f"v{i}", s.history_dir)
    assert len(history(target, s.history_dir)) == 2 + 2
    maintenance.clean_history(s)
    assert len(history(target, s.history_dir)) == 2
    s.cache_dir.mkdir(parents=True)
    for i in range(3):
        f = s.cache_dir / f"k{i}.en.srt"
        f.write_bytes(b"x" * 1000)
        old(f, 3600 * (i + 1))
    assert maintenance.clean_cache(s, cap=2000) == 1 and not (s.cache_dir / "k2.en.srt").exists()
    store = app.state.store
    j = store.enqueue("manual", "x", "/nope.mkv", "ru", None)
    store.finish(j.id, "no_source", "")
    store._q("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 400 * 86400, j.id))
    assert store.prune_jobs(180) == 1


def test_orphans_quarantined_after_two_sweeps(tmp_path):
    s = settings(tmp_path)
    d = tmp_path / "lib/Show"
    d.mkdir(parents=True)
    (d / "E1.mkv").write_bytes(b"")
    keep = d / "E1.captionizerr.ru.srt"
    orphan = d / "E0 old release.captionizerr.ru.srt"
    other = d / "E0 old release.ru.srt"                      # not ours: never touched
    for f in (keep, orphan, other):
        f.write_text("x")
    state: dict = {}
    assert maintenance.clean_orphans(s, state) == (0, 0) and orphan.exists()
    state[str(orphan)] -= 21 * 3600                           # seen a day ago
    assert maintenance.clean_orphans(s, state)[0] == 1
    assert not orphan.exists() and keep.exists() and other.exists()
    assert list((s.data_dir / "orphans").rglob(orphan.name))


def test_mass_orphans_move_nothing(tmp_path):
    s = settings(tmp_path)
    d = tmp_path / "lib/Show"
    d.mkdir(parents=True)
    for i in range(25):
        (d / f"E{i}.captionizerr.ru.srt").write_text("x")
    state = {str(p): 0.0 for p in d.iterdir()}
    with pytest.raises(RuntimeError):
        maintenance.clean_orphans(s, state)
    assert len(list(d.iterdir())) == 25


def test_maintenance_run_reports_and_metrics(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    r = c.post("/maintenance/run", headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and "Cleanup+done" in r.headers["location"]
    m = c.get("/metrics").text
    assert 'captionizerr_storage_bytes{kind="cache"}' in m and 'captionizerr_jobs{state="batched"} 0' in m
    assert "Storage and cleanup" in c.get("/settings/system").text


# ------------------------------------------------------------------ local (Ollama) and OpenAI-compatible
def ollama_server(seen: list, think_ok: bool = True):
    def handle(req: httpx.Request):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": "qwen3:8b", "size": 5_200_000_000, "details": {"parameter_size": "8.2B", "quantization_level": "Q4_K_M"}},
                {"name": "gemma3:27b", "size": 17_000_000_000, "details": {"parameter_size": "27B"}}]})
        body = json.loads(req.content)
        seen.append(body)
        if "think" in body and not think_ok:
            return httpx.Response(400, json={"error": "model does not support think"})
        work = body["messages"][-1]["content"]
        lines = [l for l in work.splitlines() if "|" in l and l.split("|")[0].isdigit()]
        return httpx.Response(200, json={"message": {"content": "\n".join(f"{l.split('|')[0]}|Строка {l.split('|')[0]}." for l in lines)},
                                         "prompt_eval_count": 900, "eval_count": 300, "done_reason": "stop"})
    return httpx.MockTransport(handle)


def test_ollama_provider_sets_context_and_translates(tmp_path, monkeypatch):
    from captionizerr.providers import ollama
    seen: list = []
    t = ollama_server(seen, think_ok=False)
    p = ollama.OllamaProvider("http://box:11434/v1", "gemma3:27b", num_ctx=8192, client=httpx.Client(transport=t))
    assert p.url == "http://box:11434" and p.max_cues == (8192 - 4000) // 70
    res = translate(_doc(tmp_path, 5), Profile(), p, log=lambda m: None)
    assert len(res.translations) == 5 and res.cost() == 0
    assert seen[-1]["options"]["num_ctx"] == 8192 and "think" not in seen[-1]   # retried without think
    real_get = httpx.get
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Client(transport=t).get(url))
    models = ollama.list_models("http://box:11434")
    assert [m["name"] for m in models] == ["gemma3:27b", "qwen3:8b"] and models[1]["params"] == "8.2B"
    monkeypatch.setattr(httpx, "get", real_get)


def test_openai_compat_parses_usage():
    from captionizerr.providers.openai_compat import OpenAICompatProvider

    def handle(req):
        assert req.headers["authorization"] == "Bearer sk-x"
        return httpx.Response(200, json={"model": "deepseek-chat", "choices": [{"message": {"content": "1|Привет."},
                              "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                              "prompt_tokens_details": {"cached_tokens": 60}}})
    p = OpenAICompatProvider("https://api.example.com/v1", "deepseek-chat", "sk-x",
                             client=httpx.Client(transport=httpx.MockTransport(handle)))
    r = p.complete("sys", "", "1|1.0|Hello.")
    assert r.text == "1|Привет." and r.usage.input_tokens == 40 and r.usage.cache_read_tokens == 60


def test_ollama_setup_in_ui(tmp_path, monkeypatch):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    assert "Local model (Ollama)" in c.get("/settings/ai").text
    from captionizerr.providers import ollama
    monkeypatch.setattr(ollama, "list_models", lambda url: [{"name": "gemma3:27b", "size_gb": 17.0, "params": "27B", "quant": ""}])
    assert c.post("/settings/ollama/models", json={"url": "http://box:11434"}).status_code == 403
    r = c.post("/settings/ollama/models", json={"url": "http://box:11434"}, headers=SAME)
    assert r.json()["models"][0]["name"] == "gemma3:27b"
    r = c.post("/settings/ai", data={"provider": "ollama", "ollama_url": "http://box:11434", "ollama_model": ""},
               headers=SAME, follow_redirects=False)
    assert "pick+one" in r.headers["location"]
    c.post("/settings/ai", data={"provider": "ollama", "ollama_url": "http://box:11434", "ollama_model": "gemma3:27b",
                                 "ollama_num_ctx": "32768"}, headers=SAME)
    s = app.state.settings
    assert s.provider == "ollama" and s.ollama_num_ctx == 32768 and s.active_model == "gemma3:27b"
    assert app.state.store.load_settings()["provider"] == "ollama"
    from captionizerr.providers import make_provider
    assert make_provider(s).name == "ollama"


def test_plex_refreshes_only_the_matching_folder(monkeypatch):
    from captionizerr import plex
    calls = []

    def fake_get(url, token, endpoint, **q):
        calls.append((endpoint, q.get("path")))
        if endpoint == "/library/sections":
            return {"MediaContainer": {"Directory": [{"key": 1, "Location": [{"path": "/media/movies"}]},
                                                     {"key": 2, "Location": [{"path": "/media/tv"}]}]}}
        return {}
    monkeypatch.setattr(plex, "_get", fake_get)
    n = plex.refresh("http://plex:32400", "t", ["/data/tv/Show/S1/E1.mkv", "/data/tv/Show/S1/E2.mkv"], [("/data/", "/media/")])
    assert n == 1 and ("/library/sections/2/refresh", "/media/tv/Show/S1") in calls


# ------------------------------------------------------------------ improvements after M5 review
def video_with_forced(folder: Path, name: str) -> Path:
    """A full English track plus a forced English track (foreign-language scenes)."""
    folder.mkdir(parents=True, exist_ok=True)
    full, forced = folder / "full.srt", folder / "forced.srt"
    full.write_text(SRT, encoding="utf-8")
    forced.write_text("1\n00:00:04,000 --> 00:00:06,000\nWhere is the ring?\n", encoding="utf-8")
    v = folder / name
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=30",
                    "-i", str(full), "-i", str(forced), "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264",
                    "-c:s", "srt", "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
                    "-disposition:s:1", "forced", str(v)], check=True)
    full.unlink(); forced.unlink()
    return v


@ffmpeg
def test_forced_track_is_translated_separately(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    v = video_with_forced(tmp_path / "lib/Show", "Show S01E01.mkv")
    app.state.store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    app.state.worker.process(app.state.store.next_queued())
    main = v.with_name("Show S01E01.captionizerr.ru.srt")
    forced = v.with_name("Show S01E01.captionizerr.ru.forced.srt")
    assert main.is_file() and forced.is_file()
    assert forced.read_text(encoding="utf-8").count("-->") == 1 and main.read_text(encoding="utf-8").count("-->") == 8
    assert any(j.source == "forced" for j in app.state.store.list())


@ffmpeg
def test_upgrade_reuses_previous_translation_and_edits(tmp_path):
    calls = []

    class Counting(FakeProvider):
        def complete(self, system, context, work):
            calls.append(work)
            return super().complete(system, context, work)

    app = create_app(settings(tmp_path), provider_factory=Counting, start_worker=False)
    store, w = app.state.store, app.state.worker
    old = make_video(tmp_path / "lib/Show/Season 1", "Show - S01E01 - Pilot WEBDL-1080p.mkv")
    store.enqueue("sonarr", "Show S01E01", str(old), "ru", None)
    w.process(store.next_queued())
    old_tr = old.with_name(old.stem + ".captionizerr.ru.srt")
    edited = old_tr.read_text(encoding="utf-8").replace("Строка номер 2 тестового эпизода.", "Моя правка.")
    old_tr.write_text(edited, encoding="utf-8")                    # the viewer's edit
    # upgrade: Sonarr deletes the old video and imports a new release of the same episode
    new = make_video(tmp_path / "lib/Show/Season 1", "Show - S01E01 - Pilot Bluray-1080p.mkv")
    old.unlink()
    n_before = len(calls)
    store.enqueue("sonarr", "Show S01E01", str(new), "ru", None)
    w.process(store.next_queued())
    out = new.with_name(new.stem + ".captionizerr.ru.srt").read_text(encoding="utf-8")
    assert "Моя правка." in out and len(calls) == n_before            # everything reused, no model call
    assert not old_tr.exists() and list((tmp_path / "config/orphans").rglob(old_tr.name))
    job = store.list(1)[0]
    assert "re-timed from the previous release" in job.detail and job.cost_usd == 0


def test_align_by_timing_with_offset(tmp_path):
    from captionizerr import reuse
    new = tmp_path / "new.srt"
    new.write_text(SRT, encoding="utf-8")
    shifted = "".join(f"{i}\n00:00:{i * 3 + 1:02d},000 --> 00:00:{i * 3 + 3:02d},500\nСтрока {i}.\n\n" for i in range(1, 9))
    old = tmp_path / "old.srt"
    old.write_text(shifted, encoding="utf-8")                      # same lines, 1 s later
    r = reuse.align(cuesmod.load(new), cuesmod.load(old), None)
    assert r.coverage == 1.0 and r.method == "timing" and r.translations[3] == "Строка 3."


def test_open_batches_are_reserved_against_the_cap(tmp_path):
    app = create_app(settings(tmp_path, monthly_cap_usd=1.0), provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    a = store.enqueue("manual", "a", "/x/a.mkv", "ru", None, "batch")
    store.set_batched(a.id, "b1", "in batch", 0.9)
    assert abs(store.reserved() - 0.9) < 1e-9
    b = store.enqueue("manual", "b", "/x/b.mkv", "ru", None)
    assert w.over_budget(store.get(b.id), 0.2) and store.get(b.id).state == "paused_budget"


def test_daily_db_snapshot(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    rep = maintenance.run(app.state.settings, app.state.store)
    snap = tmp_path / "config/backup" / rep["db_snapshot"]
    import sqlite3
    assert sqlite3.connect(snap).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= 0


@ffmpeg
def test_editor_shows_preparing_while_extracting(tmp_path, monkeypatch):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    v = make_video(tmp_path / "lib/Show", "Show S01E01.mkv")
    v.with_name("Show S01E01.captionizerr.ru.srt").write_text(SRT, encoding="utf-8")
    import threading
    gate = threading.Event()
    real = media.find_source
    monkeypatch.setattr(media, "find_source", lambda *a, **k: (gate.wait(10), real(*a, **k))[1])
    r = c.get("/edit", params={"path": str(v), "lang": "ru"})
    assert "Preparing the editor" in r.text
    gate.set()
    time.sleep(0.5)
    r = c.get("/edit", params={"path": str(v), "lang": "ru"})
    assert "Season 1, Episode 1" in r.text


@ffmpeg
def test_eval_build_and_run(tmp_path):
    from captionizerr import evals
    from captionizerr.writer import replace_own
    s = settings(tmp_path)
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    v = make_video(tmp_path / "lib/Show", "Show - S01E01.mkv")
    app.state.store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    app.state.worker.process(app.state.store.next_queued())
    out = v.with_name("Show - S01E01.captionizerr.ru.srt")
    replace_own(out, out.read_text(encoding="utf-8").replace("Строка номер 5 тестового эпизода.", "Пятая."), s.history_dir)
    made = evals.build(s.data_dir, s.data_dir / "evals", lines=6, per_file=1)
    assert len(made) == 1 and json.loads((made[0] / "case.json").read_text())["edited"]
    assert "Пятая." in (made[0] / "reference.srt").read_text(encoding="utf-8")
    rep = evals.run(s.data_dir / "evals", FakeProvider())
    assert 50 < rep["chrf"] < 100 and rep["cases"][0]["flags"] == 0   # differs only on the edited line


# ------------------------------------------------------------------ OpenSubtitles sharing
def test_movie_hash_matches_reference_algorithm(tmp_path):
    from captionizerr.opensubtitles import movie_hash
    import random
    data = random.Random(1).randbytes(300_000)
    f = tmp_path / "v.mkv"
    f.write_bytes(data)
    words = lambda b: sum(int.from_bytes(b[i:i + 8], "little") for i in range(0, len(b), 8))
    expect = (len(data) + words(data[:65536]) + words(data[-65536:])) % 2 ** 64
    assert movie_hash(f) == (f"{expect:016x}", len(data))


class FakeOS:
    def __init__(self, already=False):
        self.calls, self.already = [], already

    def LogIn(self, u, p, lang, ua):
        self.calls.append(("LogIn", u, ua))
        return {"status": "200 OK", "token": "tok"}

    def GuessMovieFromString(self, token, names):
        return {"status": "200 OK", "data": {names[0]: {"BestGuess": {"IDMovieIMDB": "1234567", "MovieName": "Northwind",
                                                                      "MovieKind": "episode", "SeriesSeason": "2", "SeriesEpisode": "1"}}}}

    def TryUploadSubtitles(self, token, sub):
        self.calls.append(("Try", sub))
        return {"status": "200 OK", "alreadyindb": 1 if self.already else 0, "data": [] if not self.already else
                [{"IDSubtitle": "123"}]}

    def UploadSubtitles(self, token, sub):
        self.calls.append(("Upload", sub))
        return {"status": "200 OK", "data": "https://www.opensubtitles.org/subtitles/999"}


def test_upload_payload_is_flagged_and_complete(tmp_path):
    import base64, gzip
    from captionizerr.opensubtitles import Client
    v = tmp_path / "Northwind - S02E01 WEBDL-1080p.mkv"
    v.write_bytes(b"\0" * 200_000)
    sub = tmp_path / "Northwind - S02E01 WEBDL-1080p.captionizerr.ru.forced.srt"
    sub.write_text(SRT, encoding="utf-8")
    fake = FakeOS()
    c = Client("me", "pw", "Captionizerr v0.1.0", proxy=fake)
    c.login()
    assert c.guess(v.name).imdb_id == "1234567"
    r = c.upload(sub, v, "ru", "tt1234567", v.stem, "AI", "Captionizerr", forced=True, hd=True)
    assert r == {"already": False, "url": "https://www.opensubtitles.org/subtitles/999"}
    up = [x for x in fake.calls if x[0] == "Upload"][0][1]
    b = up["baseinfo"]
    assert b["automatictranslation"] == "1" and b["foreignpartsonly"] == "1" and b["sublanguageid"] == "rus"
    assert b["idmovieimdb"] == "1234567" and b["highdefinition"] == "1"
    assert gzip.decompress(base64.b64decode(up["cd1"]["subcontent"])) == sub.read_bytes()
    dup = FakeOS(already=True)
    c2 = Client("me", "pw", "ua", proxy=dup)
    c2.login()
    assert c2.upload(sub, v, "ru", "tt1234567", "", "", "")["already"] is True
    assert not [x for x in dup.calls if x[0] == "Upload"]


@ffmpeg
def test_share_needs_config_and_confirmation(tmp_path, monkeypatch):
    s = settings(tmp_path)
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    v = make_video(tmp_path / "lib/Show", "Show S01E01 1080p.mkv")
    with open(v, "ab") as fh:                     # the hash reads 2 x 64 KiB; test videos are tiny
        fh.write(b"\0" * 200_000)
    sub = v.with_name("Show S01E01 1080p.captionizerr.ru.srt")
    sub.write_text(SRT, encoding="utf-8")
    q = {"path": str(v), "lang": "ru"}
    assert "Not set up" in c.get("/share", params=q).text
    s.opensubtitles_user, s.opensubtitles_password = "me", "pw"
    from captionizerr import opensubtitles
    fake = FakeOS()
    real = opensubtitles.Client
    monkeypatch.setattr(opensubtitles, "Client", lambda u, p, ua: real(u, p, ua, proxy=fake))
    page = c.get("/share", params=q).text
    assert "1234567" in page and "AI translation" in page
    data = {**q, "imdb_id": "tt1234567", "release": "x", "comment": "c", "translator": "t"}
    r = c.post("/share", data=data, headers=SAME, follow_redirects=False)
    assert "Tick+both" in r.headers["location"] and not [x for x in fake.calls if x[0] == "Upload"]
    r = c.post("/share", data={**data, "reviewed": "1", "rights": "1"}, headers=SAME, follow_redirects=False)
    assert "Uploaded" in r.headers["location"]
    assert app.state.store.share(str(sub))["url"].endswith("/999")
    assert c.post("/share", data={**data, "reviewed": "1", "rights": "1"}).status_code == 403   # cross-site


def test_openai_adapts_token_parameter_and_temperature():
    from captionizerr.providers.openai_compat import OpenAICompatProvider
    seen = []

    def strict_openai(req):        # like OpenAI's newer models
        b = json.loads(req.content); seen.append(sorted(b))
        if "max_tokens" in b:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."}})
        if "temperature" in b:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.3 with this model."}})
        return httpx.Response(200, json={"model": b["model"], "choices": [{"message": {"content": "1|Привет."}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    # a non-OpenAI base URL starts with max_tokens and has to adapt twice
    p = OpenAICompatProvider("https://proxy.example/v1", "gpt-6-luna", "k", client=httpx.Client(transport=httpx.MockTransport(strict_openai)))
    assert p.complete("", "", "1|1.0|Hi.").text == "1|Привет."
    assert p.token_param == "max_completion_tokens" and p.send_temperature is False and len(seen) == 3
    seen.clear(); p.complete("", "", "1|1.0|Hi.")
    assert len(seen) == 1                                   # remembered: no retries next time
    # OpenAI's own URL starts with the new parameter right away
    assert OpenAICompatProvider("https://api.openai.com/v1", "m", "k").token_param == "max_completion_tokens"

    def old_server(req):           # e.g. an older local server
        b = json.loads(req.content)
        if "max_completion_tokens" in b:
            return httpx.Response(400, json={"error": {"message": "unknown field: max_completion_tokens; use max_tokens"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}})
    q = OpenAICompatProvider("https://api.openai.com/v1", "m", "k", client=httpx.Client(transport=httpx.MockTransport(old_server)))
    assert q.complete("", "", "x").text == "ok" and q.token_param == "max_tokens"


def test_openai_error_message_is_readable():
    from captionizerr.providers.openai_compat import OpenAICompatProvider
    from captionizerr.providers.base import ProviderError
    t = httpx.MockTransport(lambda req: httpx.Response(401, json={"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error"}}))
    with pytest.raises(ProviderError) as e:
        OpenAICompatProvider("https://api.openai.com/v1", "m", "bad", client=httpx.Client(transport=t)).complete("", "", "x")
    assert str(e.value) == "401: Incorrect API key provided."


def test_opensubtitles_login_errors_explain_themselves():
    from captionizerr.opensubtitles import Client, OpenSubtitlesError

    class R:
        def __init__(self, status): self.status = status
        def LogIn(self, *a): return {"status": self.status, "token": "t"}
    with pytest.raises(OpenSubtitlesError, match="sign in once"):
        Client("u", "p", "ua", proxy=R("401 Unauthorized")).login()
    with pytest.raises(OpenSubtitlesError, match="USER_AGENT"):
        Client("u", "p", "ua", proxy=R("414 Unknown User Agent")).login()
    assert Client("u", "p", "ua", proxy=R("200 OK")).login() == "t"
