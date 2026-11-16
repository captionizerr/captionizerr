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


# ------------------------------------------------------------------ automatic prices
def fake_fetch(url, cache, timeout=20):
    if "litellm" in url:
        return {"gpt-6-luna": {"input_cost_per_token": 1e-07, "output_cost_per_token": 5e-07, "cache_read_input_token_cost": 1e-08},
                "deepseek/deepseek-chat": {"input_cost_per_token": 2.8e-07, "output_cost_per_token": 4.2e-07},
                "claude-opus-5-5": {"input_cost_per_token": 4e-06, "output_cost_per_token": 2e-05},
                "claude-sonnet-5-5": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1e-05}}, 1700000000.0
    return {"data": [{"id": "openai/gpt-6-luna", "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}}]}, 1700000000.0


def test_price_lookup_sources(tmp_path):
    from captionizerr.price_lookup import lookup, check_builtin
    p = lookup("openai", "https://api.openai.com/v1", "gpt-6-luna", tmp_path, fake_fetch)
    assert (round(p.inp, 4), round(p.out, 4), round(p.cache_read, 4)) == (0.1, 0.5, 0.01) and "LiteLLM" in p.source
    assert round(p.per_episode, 4) == round((16000 * 0.1 + 12000 * 0.5) / 1e6, 4)
    assert lookup("openai", "https://api.deepseek.com/v1", "deepseek-chat", tmp_path, fake_fetch).inp == pytest.approx(0.28)
    assert lookup("openai", "https://openrouter.ai/api/v1", "openai/gpt-6-luna", tmp_path, fake_fetch).source == "OpenRouter (live)"
    assert lookup("openai", "http://192.168.1.5:1234/v1", "anything", tmp_path, fake_fetch).inp == 0
    assert lookup("ollama", "", "gemma3:27b", tmp_path, fake_fetch).source.startswith("local")
    a = lookup("anthropic", "", "claude-opus-5-5", tmp_path, fake_fetch)
    assert (a.inp, a.out) == (4.0, 20.0) and "built-in" in a.source
    assert lookup("openai", "https://api.openai.com/v1", "no-such-model", tmp_path, fake_fetch) is None
    chk = check_builtin(tmp_path, fake_fetch)
    assert chk["claude-opus-5-5"]["status"] == "matches" and chk["claude-sonnet-5-5"]["status"] == "differs"


def test_price_cache_used_when_offline(tmp_path):
    from captionizerr import price_lookup
    import json as _json, time as _time
    cache = tmp_path / "prices-litellm.json"
    cache.write_text(_json.dumps({"fetched": _time.time() - 10 * 86400, "data": {"m": {"input_cost_per_token": 1e-06}}}))
    def offline(*a, **k):
        raise OSError("no network")
    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = offline
    try:
        p = price_lookup.litellm("m", tmp_path)       # stale but usable
    finally:
        urllib.request.urlopen = orig
    assert p is not None and p.inp == pytest.approx(1.0)


def test_price_endpoint_and_zero_price_warning(tmp_path, monkeypatch):
    from captionizerr import price_lookup
    monkeypatch.setattr(price_lookup, "_fetch_json", fake_fetch)
    monkeypatch.setattr(price_lookup.lookup, "__defaults__", (fake_fetch,))
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    r = c.post("/settings/price", data={"provider": "openai", "model": "gpt-6-luna", "base_url": "https://api.openai.com/v1"}, headers=SAME)
    assert r.json()["found"] and r.json()["per_episode"] == pytest.approx(0.0076)
    assert c.post("/settings/price", data={"provider": "openai", "model": "x"}).status_code == 403
    r = c.post("/settings/ai", data={"provider": "openai", "openai_base_url": "https://api.openai.com/v1", "openai_model": "gpt-6-luna",
                                     "openai_price_in": "0", "openai_price_out": "0"}, headers=SAME, follow_redirects=False)
    assert "can%27t+protect" in r.headers["location"] or "can't+protect" in r.headers["location"]
    r = c.post("/settings/ai", data={"provider": "openai", "openai_base_url": "https://api.openai.com/v1", "openai_model": "gpt-6-luna",
                                     "openai_price_in": "0.1", "openai_price_out": "0.5", "openai_price_source": "LiteLLM community price list"},
               headers=SAME, follow_redirects=False)
    s = app.state.settings
    assert "protect" not in r.headers["location"] and s.openai_price_source.startswith("LiteLLM") and s.openai_price_in == 0.1
    r = c.post("/settings/ai", data={"provider": "openai", "openai_base_url": "http://localhost:1234/v1", "openai_model": "local",
                                     "openai_price_in": "0", "openai_price_out": "0"}, headers=SAME, follow_redirects=False)
    assert "protect" not in r.headers["location"]                     # local servers are free: no warning


# ------------------------------------------------------------------ switching vendors / models
def test_price_tolerates_snapshot_names():
    from captionizerr.pricing import price, register
    register("acme-model", 1.0, 2.0)
    assert price("acme-model-2026-08-06") == price("acme-model") == price("acme-model-20260806")
    assert price("claude-opus-5-5-20260101") == (4.0, 20.0, 0.2)
    assert price("totally-unknown") is None


def test_estimates_follow_the_current_model(tmp_path):
    s = settings(tmp_path)
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    st = app.state.store
    for i in range(3):                              # history: three Opus episodes
        j = st.enqueue("manual", f"ep{i}", f"/x/{i}.mkv", "ru", None)
        st.finish(j.id, "done", "", "", 0.30, {}, "claude-opus-5-5")
    est = app.state.episode_estimate
    assert est() == pytest.approx(0.30)             # Anthropic Opus: its own average
    s.provider, s.openai_model, s.openai_base_url = "openai", "luna-x", "https://api.openai.com/v1"
    from captionizerr.pricing import register
    register("luna-x", 0.1, 0.5)
    assert est() == pytest.approx((16000 * 0.1 + 12000 * 0.5) / 1e6)   # not Opus's history
    s.provider = "ollama"; s.ollama_model = "gemma3:27b"
    assert est() == 0.0
    s.provider = "anthropic"
    assert est() == pytest.approx(0.30)             # back again


def test_unknown_price_is_reported_not_hidden(tmp_path):
    class Unpriced(FakeProvider):
        model = "mystery-model"
        def complete(self, system, context, work):
            r = super().complete(system, context, work); r.model = "mystery-model"; return r
    app = create_app(settings(tmp_path), provider_factory=Unpriced, start_worker=False)
    v = make_video(tmp_path / "lib/Show", "Show S01E01.mkv") if HAS_FFMPEG else None
    if v is None:
        pytest.skip("ffmpeg needed")
    app.state.store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    app.state.worker.process(app.state.store.next_queued())
    j = app.state.store.list(1)[0]
    assert "cost unknown: no price for mystery-model" in j.detail and j.model == "mystery-model"


@ffmpeg
def test_editor_retranslate_priced_by_the_model_that_did_it(tmp_path):
    from captionizerr.pricing import register
    register("cheap-model", 1.0, 1.0)

    class Cheap(FakeProvider):
        model = "cheap-model"
        def complete(self, system, context, work):
            r = super().complete(system, context, work); r.model = "cheap-model"; return r
    app = create_app(settings(tmp_path), provider_factory=Cheap, start_worker=False)
    c = TestClient(app)
    v = make_video(tmp_path / "lib/Show", "Show S01E01.mkv")
    v.with_name("Show S01E01.captionizerr.ru.srt").write_text(SRT, encoding="utf-8")
    r = c.post("/api/edit/retranslate", json={"path": str(v), "lang": "ru", "id": 2}, headers=SAME)
    assert r.status_code == 200, r.text
    rec = [j for j in app.state.store.list(5) if j.source == "edit"][0]
    assert rec.model == "cheap-model" and rec.cost_usd == pytest.approx((1000 + 800) * 1.0 / 1e6)   # not Opus's $4/$20


@ffmpeg
def test_open_batch_still_collected_after_switching_provider(tmp_path, monkeypatch):
    s = settings(tmp_path, batch_min_wait_s=0)
    app = create_app(s, provider_factory=FakeBatch, start_worker=False)
    st, w = app.state.store, app.state.worker
    v = make_video(tmp_path / "lib/Show/Season 1", "Show S01E01.mkv")
    j = st.enqueue("manual", "Show S01E01", str(v), "ru", None, "batch")
    w.submit_batch()
    assert st.get(j.id).state == "batched"
    w.provider_factory = FakeProvider                     # owner switches to a provider without Batch
    from captionizerr.providers import anthropic_provider
    monkeypatch.setattr(anthropic_provider, "AnthropicProvider", lambda **k: FakeBatch())
    w.poll_batches()
    assert st.get(j.id).state == "done"


def test_cli_uses_keys_saved_in_the_ui(tmp_path, monkeypatch):
    from captionizerr import secret_store
    from captionizerr.cli import make_cli_provider
    import argparse
    monkeypatch.setenv("CAPTIONIZERR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CAPTIONIZERR_OPENAI_API_KEY", raising=False)
    secret_store.SecretStore(tmp_path / "secrets.json").set("openai_key", "sk-from-ui")
    a = argparse.Namespace(provider="openai", model="gpt-6-luna", base_url="https://api.openai.com/v1",
                           ollama_url="", num_ctx=0, effort="low")
    from captionizerr import price_lookup
    monkeypatch.setattr(price_lookup.lookup, "__defaults__", (fake_fetch,))
    p = make_cli_provider(a)
    from captionizerr.pricing import price
    assert p.key == "sk-from-ui" and price("gpt-6-luna")[:2] == pytest.approx((0.1, 0.5))


# ------------------------------------------------------------------ re-translate, effort, key display
@ffmpeg
def test_retranslate_replaces_our_file_and_keeps_history(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    st, w, c = app.state.store, app.state.worker, TestClient(app)
    v = make_video(tmp_path / "lib/Show/Season 1", "Show S01E01.mkv")
    j = st.enqueue("manual", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    out = Path(st.get(j.id).output_path)
    out.write_text(out.read_text(encoding="utf-8") + "\n", encoding="utf-8")   # pretend the owner edited it
    edited = out.read_text(encoding="utf-8")
    # a plain re-enqueue is still skipped (webhook double-fire safety)
    k = st.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(st.next_queued())
    assert st.get(k.id).state == "skipped"
    # Re-translate replaces it, and the edited version is in the editor's history
    r = c.post(f"/jobs/{j.id}/retranslate", headers=SAME, follow_redirects=False)
    assert r.status_code == 303 and "previous+version" in r.headers["location"]
    nxt = st.next_queued(); assert nxt.redo == 1
    w.process(nxt)
    assert st.get(nxt.id).state in ("done", "needs_review")
    from captionizerr.writer import history
    assert any(h.read_text(encoding="utf-8") == edited for h in history(out, app.state.settings.history_dir))
    # the series page's Translate on an already-translated episode is a redo too
    c.post("/library/translate", data={"path": str(v), "series_key": "", "title": "Show"}, headers=SAME)
    assert st.next_queued().redo == 1


def test_needs_review_retry_may_replace_its_file(tmp_path):
    st = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False).state.store
    j = st.enqueue("manual", "x", "/x/a.mkv", "ru", None)
    st.finish(j.id, "needs_review", "1 open")
    assert st.requeue(j.id) and st.get(j.id).redo == 1


def test_openai_effort_sent_and_dropped_when_unsupported():
    from captionizerr.providers.openai_compat import OpenAICompatProvider
    seen = []

    def server(req):
        b = json.loads(req.content); seen.append(b.get("reasoning_effort"))
        if "reasoning_effort" in b and b["model"] == "plain":
            return httpx.Response(400, json={"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}})
        return httpx.Response(200, json={"model": b["model"], "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    cl = httpx.Client(transport=httpx.MockTransport(server))
    OpenAICompatProvider("https://api.openai.com/v1", "gpt-6-luna", "k", client=cl, effort="low").complete("", "", "hi")
    assert seen == ["low"]
    seen.clear()
    r = OpenAICompatProvider("https://x/v1", "plain", "k", client=cl, effort="high").complete("", "", "hi")
    assert seen == ["high", None] and any("effort high not supported" in n for n in r.notes)
    seen.clear()
    OpenAICompatProvider("https://x/v1", "m", "k", client=cl).complete("", "", "hi")
    assert seen == [None]                                      # default: not sent at all


def test_ollama_thinking_levels_fall_back():
    from captionizerr.providers.ollama import OllamaProvider
    seen = []

    def server(req):
        b = json.loads(req.content); seen.append(b.get("think", "absent"))
        if b["model"] == "onoff" and isinstance(b.get("think"), str):
            return httpx.Response(400, text='{"error":"think value \\"medium\\" is not supported for this model"}')
        if b["model"] == "nothink" and "think" in b and b["think"] is not False:
            return httpx.Response(400, text='{"error":"model does not support think"}')
        return httpx.Response(200, json={"message": {"content": "OK"}, "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1})
    cl = httpx.Client(transport=httpx.MockTransport(server))
    OllamaProvider("http://o:11434", "gpt-oss", client=cl, effort="medium").complete("", "", "hi"); assert seen == ["medium"]
    seen.clear(); OllamaProvider("http://o:11434", "onoff", client=cl, effort="medium").complete("", "", "hi"); assert seen == ["medium", True]
    seen.clear(); OllamaProvider("http://o:11434", "nothink", client=cl, effort="high").complete("", "", "hi"); assert seen == ["high", True, "absent"]
    seen.clear(); OllamaProvider("http://o:11434", "m", client=cl).complete("", "", "hi"); assert seen == [False]


def test_ai_page_shows_saved_key_clearly_and_effort_saves(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    app.state.store.save_settings({"_setup_done": True})
    c.post("/settings/ai", data={"provider": "openai", "openai_base_url": "https://api.openai.com/v1", "openai_model": "gpt-6-luna",
                                 "openai_price_in": "0.1", "openai_price_out": "0.5", "openai_effort": "medium",
                                 "secret:openai_key": "sk-test-0123456789abcdefWXYZ"}, headers=SAME)
    s = app.state.settings
    assert s.openai_effort == "medium" and s.openai_key.endswith("WXYZ")
    page = c.get("/settings/ai").text
    assert "✓ Saved" in page and "…WXYZ" in page and "sk-test-0123456789" not in page and "saved " in page
    c.post("/settings/ai", data={"provider": "ollama", "ollama_model": "gpt-oss:20b", "ollama_effort": "bogus"}, headers=SAME)
    assert s.ollama_effort == "off"                            # unknown values fall back safely


class RealShapeOS(FakeOS):
    """GuessMovieFromString exactly as OpenSubtitles answered for a real episode file (2026-11-16)."""
    def GuessMovieFromString(self, token, names):
        return {"status": "200 OK", "data": {names[0]: {
            "GuessIt": {"title": "Harbor Lights", "season": 2, "episode": 1, "type": "episode"},
            "BestGuess": {"IDMovieIMDB": "7654321", "MovieName": "Harbor Lights", "MovieYear": 2020, "MovieKind": "tv series",
                          "Reason": "GetIMDBSuggest", "Episode": 1, "Season": 2, "IMDBEpisode": "35366575"}}}}


def test_guess_uses_the_episode_id():
    from captionizerr.opensubtitles import Client
    c = Client("me", "pw", "ua", proxy=RealShapeOS()); c.login()
    g = c.guess("Harbor Lights - S02E01 - Title WEBDL-1080p.mkv")
    assert (g.imdb_id, g.series_imdb_id, g.kind, g.season, g.episode) == ("35366575", "7654321", "episode", 2, 1)


@ffmpeg
def test_share_episode_id_guard_and_download(tmp_path, monkeypatch):
    s = settings(tmp_path)
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    v = make_video(tmp_path / "lib/Harbor Lights/Season 2", "Harbor Lights - S02E01 - Title WEBDL-1080p.mkv")
    with open(v, "ab") as fh:
        fh.write(b"\0" * 200_000)
    sub = v.with_name(v.stem + ".captionizerr.ru.srt"); sub.write_text(SRT, encoding="utf-8")
    q = {"path": str(v), "lang": "ru"}
    # download works without any OpenSubtitles setup, with a clean player-style name
    r = c.get("/share/download", params=q)
    assert r.status_code == 200 and r.content == sub.read_bytes()
    import urllib.parse as _up
    cd = r.headers["content-disposition"]
    assert _up.unquote(cd.split("filename*=UTF-8''")[1]) == sub.name and "'" not in cd.split("filename*=UTF-8''")[1]
    assert c.get("/share/download", params={"path": "/etc/passwd", "lang": "ru"}).status_code == 404
    assert "Download SRT" in c.get("/share", params=q).text
    s.opensubtitles_user, s.opensubtitles_password = "me", "pw"
    from captionizerr import opensubtitles
    fake = RealShapeOS(); real = opensubtitles.Client
    monkeypatch.setattr(opensubtitles, "Client", lambda u, p, ua: real(u, p, ua, proxy=fake))
    page = c.get("/share", params=q).text
    assert 'value="35366575"' in page and "the show is tt7654321" in page
    data = {**q, "release": "x", "comment": "c", "translator": "t", "reviewed": "1", "rights": "1"}
    r = c.post("/share", data={**data, "imdb_id": "tt7654321"}, headers=SAME, follow_redirects=False)
    assert "whole+series" in r.headers["location"] and not [x for x in fake.calls if x[0] == "Upload"]
    r = c.post("/share", data={**data, "imdb_id": "tt35366575"}, headers=SAME, follow_redirects=False)
    assert "Uploaded" in r.headers["location"]
    assert [x for x in fake.calls if x[0] == "Upload"][0][1]["baseinfo"]["idmovieimdb"] == "35366575"


@ffmpeg
def test_share_credits_the_model_that_made_the_file(tmp_path, monkeypatch):
    s = settings(tmp_path)
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    c, st = TestClient(app), app.state.store
    v = make_video(tmp_path / "lib/Show", "Show's Day S01E01 1080p.mkv")
    with open(v, "ab") as fh:
        fh.write(b"\0" * 200_000)
    sub = v.with_name(v.stem + ".captionizerr.ru.srt"); sub.write_text(SRT, encoding="utf-8")
    j = st.enqueue("manual", "Show S01E01", str(v), "ru", None); st.finish(j.id, "done", "", str(sub), 0.003, {}, "gpt-6-luna")
    s.provider, s.model = "anthropic", "claude-opus-5-5"            # owner switched since
    s.opensubtitles_user, s.opensubtitles_password = "me", "pw"
    from captionizerr import opensubtitles
    real = opensubtitles.Client
    monkeypatch.setattr(opensubtitles, "Client", lambda u, p, ua: real(u, p, ua, proxy=FakeOS()))
    page = c.get("/share", params={"path": str(v), "lang": "ru"}).text
    assert "AI translation (gpt-6-luna)" in page and "CaptionizeRR (AI: gpt-6-luna)" in page and "opus" not in page.split("Upload to OpenSubtitles")[1]
    r = c.get("/share/download", params={"path": str(v), "lang": "ru"})
    import urllib.parse as _up
    assert _up.unquote(r.headers["content-disposition"].split("filename*=UTF-8''")[1]) == sub.name


# ------------------------------------------------------------------ incomplete source tracks
def _srt(n: int, last_len_s: float = 2, step_s: float = 3) -> str:
    def ts(sec):
        ms = int(sec * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    out = []
    for i in range(n):
        a = 1 + i * step_s
        b = a + (last_len_s if i == n - 1 else 2)
        out.append(f"{i + 1}\n{ts(a)} --> {ts(b)}\nLine number {i + 1} of the dialogue.\n")
    return "\n".join(out)


def _video_with(folder: Path, name: str, srt_text: str, seconds: int = 140) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    srt = folder / "src.srt"
    srt.write_text(srt_text, encoding="utf-8")
    v = folder / name
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=black:s=32x32:d={seconds}:r=1",
                    "-i", str(srt), "-map", "0", "-map", "1", "-c:v", "libx264", "-c:s", "srt",
                    "-metadata:s:s:0", "language=eng", str(v)], check=True)
    srt.unlink()
    return v


def test_cut_off_shapes(tmp_path):
    def cues(text):
        f = tmp_path / "x.srt"
        f.write_text(text, encoding="utf-8")
        return cuesmod.load(f).cues
    assert media.cut_off(cues(_srt(25)), 80) is None                         # complete
    assert media.cut_off(cues(_srt(25, last_len_s=600)), 700) == 73_000       # last line stretched to the end
    assert media.cut_off(cues(_srt(25)), 3600) == 75_000                      # stops ~58 min before the end
    assert media.cut_off(cues(_srt(25)), 600) is None                         # long credits, not a quarter gone
    assert media.cut_off(cues(_srt(5, last_len_s=600)), 700) is None          # short (forced/signs) tracks: never judged
    assert media.clock(3_076_917) == "51:16" and media.clock(3_676_917) == "1:01:16"


@ffmpeg
def test_complete_sidecar_beats_a_cut_off_embedded_track(tmp_path):
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt(40, last_len_s=65))
    assert media.find_source(v, tmp_path / "cache", ["en"], "ru").origin == "embedded"
    (v.parent / "Show S01E01.en.srt").write_text(_srt(70), encoding="utf-8")
    src = media.find_source(v, tmp_path / "cache", ["en"], "ru")
    assert src.origin == "embedded + sidecar ending" and ".spliced." in src.path.name
    got = cuesmod.load(src.path).cues
    assert len(got) == 70 and max(c.end_ms - c.start_ms for c in got) < 60_000
    (v.parent / "Show S01E01.en.srt").write_text(_srt(38), encoding="utf-8")   # shorter: no better
    assert media.find_source(v, tmp_path / "cache", ["en"], "ru").origin == "embedded"


@ffmpeg
def test_cut_off_source_asks_bazarr_then_translates_with_a_warning(tmp_path, monkeypatch):
    s = settings(tmp_path, bazarr_url="http://bazarr", bazarr_key="k")
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt(40, last_len_s=65))
    asked = []
    monkeypatch.setattr(w, "_bazarr_request", lambda bz, job: asked.append(job.id) or True)
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state == "waiting_source" and j.detail.startswith("Source cut short:") and "1:58" in j.detail and asked
    v.with_name("Show S01E01.de.srt").write_text(_srt(25), encoding="utf-8")   # older, unrelated: not a reason
    old(v.with_name("Show S01E01.de.srt"), 10 * 3600)
    w.check_waiting()
    assert store.get(job.id).state == "waiting_source"
    store._q("UPDATE jobs SET wait_since=? WHERE id=?", (time.time() - 4 * 3600, job.id))
    w.check_waiting()                                                          # nothing better came: use what we have
    assert store.get(job.id).state == "queued"
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state == "needs_review" and j.detail.startswith("Incomplete source:") and "Re-translate" in j.detail
    out = cuesmod.load(j.output_path).cues
    assert len(out) == 40 and out[-1].end_ms - out[-1].start_ms <= 7000 and asked == [job.id]


@ffmpeg
def test_cut_off_source_uses_the_complete_subtitle_bazarr_brings(tmp_path, monkeypatch):
    s = settings(tmp_path, bazarr_url="http://bazarr", bazarr_key="k")
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt(40, last_len_s=65))
    monkeypatch.setattr(w, "_bazarr_request", lambda bz, job: True)
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    side = v.with_name("Show S01E01.en.srt")
    side.write_text(_srt(70), encoding="utf-8")
    t = time.time() - 300
    os.utime(side, (t, t))
    store._q("UPDATE jobs SET wait_since=? WHERE id=?", (t - 60, job.id))
    w.check_waiting()
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state == "done" and "70 cues from en (embedded + sidecar ending)" in j.detail


@ffmpeg
def test_cut_off_source_without_bazarr_goes_straight_to_review(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt(40, last_len_s=65))
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    assert store.get(job.id).state == "needs_review" and "stops at 1:58" in store.get(job.id).detail


@ffmpeg
def test_stretched_last_line_confirmed_by_a_sidecar_is_just_shortened(tmp_path, monkeypatch):
    s = settings(tmp_path, bazarr_url="http://bazarr", bazarr_key="k")
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt(40, last_len_s=65))
    side = v.with_name("Show S01E01.en.hi.srt")
    side.write_text(_srt(41), encoding="utf-8")          # complete, ends 3 s after the embedded track: same dialogue
    old(side)
    asked = []
    monkeypatch.setattr(w, "_bazarr_request", lambda bz, job: asked.append(1) or True)
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state == "done" and "(embedded)" in j.detail and "shortened" in j.detail and not asked
    assert cuesmod.load(j.output_path).cues[-1].end_ms - cuesmod.load(j.output_path).cues[-1].start_ms <= 7000


def test_fit_timing_undoes_frame_rate_and_shift(tmp_path):
    ref = cuesmod.load(_write(tmp_path / "ref.srt", _srt(80))).cues
    pal = cuesmod.load(_write(tmp_path / "pal.srt", _srt(80))).cues
    for c in pal:                                   # the same lines timed for 25 fps with a 20 s longer intro
        c.start_ms, c.end_ms = round(c.start_ms / 1.0427 - 20_000), round(c.end_ms / 1.0427 - 20_000)
    a, b = media.fit_timing(ref, pal)
    assert abs(a - 1.0427) < 1e-3 and all(abs(a * p.start_ms + b - r.start_ms) < 50 for p, r in zip(pal, ref))
    other = cuesmod.load(_write(tmp_path / "other.srt", _srt(80).replace("dialogue", "monologue"))).cues
    assert media.fit_timing(ref, other) is None     # nothing in common: never used


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


@ffmpeg
def test_out_of_sync_sidecar_is_lined_up_before_use(tmp_path):
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt(40, last_len_s=65))
    side = v.parent / "Show S01E01.en.srt"
    pal = cuesmod.load(_write(tmp_path / "x.srt", _srt(70)))
    import pysubs2
    for ev in pal.ssa.events:
        ev.start, ev.end = round(ev.start / 1.0427) + 5000, round(ev.end / 1.0427) + 5000
    pal.ssa.save(str(side), format_="srt")
    src = media.find_source(v, tmp_path / "cache", ["en"], "ru")
    assert src.origin == "embedded + sidecar ending" and src.path.parent == tmp_path / "cache"
    fixed = cuesmod.load(src.path).cues
    want = cuesmod.load(_write(tmp_path / "y.srt", _srt(70))).cues
    assert all(abs(f.start_ms - w.start_ms) <= 20 for f, w in zip(fixed, want))
    assert pysubs2 and side.read_text(encoding="utf-8").startswith("1\n")      # the original is untouched


def test_long_last_line_that_stops_before_the_end_is_not_a_cut(tmp_path):
    f = _write(tmp_path / "x.srt", _srt(40, last_len_s=90))          # 118 s -> 208 s, a song over the credits
    cs = cuesmod.load(f).cues
    assert media.cut_off(cs, 600) is None and media.cut_off(cs, 230) == 118_000
    assert media.cut_off(cuesmod.load(_write(tmp_path / "y.srt", _srt(40, last_len_s=200))).cues, 600) == 118_000


@ffmpeg
def test_stretched_line_in_the_middle_goes_to_review(tmp_path):
    text = _srt(40)
    text = text.replace("00:00:31,000 --> 00:00:33,000", "00:00:31,000 --> 00:03:31,000")   # line 11 over 3 min
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", text)
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state == "needs_review" and "line at 0:31 stayed on screen for 3:00" in j.detail
    assert max(c.end_ms - c.start_ms for c in cuesmod.load(j.output_path).cues) <= 7000


def test_unreadable_video_fails_with_a_clear_message(tmp_path):
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = tmp_path / "lib/Show/Show S01E01.mkv"
    v.parent.mkdir(parents=True)
    v.write_bytes(b"not a video")
    job = store.enqueue("sonarr", "Show S01E01", str(v), "ru", None)
    w.process(store.next_queued())
    j = store.get(job.id)
    assert j.state == "failed" and j.detail.startswith("can't read the video") and "network share" in j.detail


@ffmpeg
def test_batch_result_uses_the_source_it_was_built_from(tmp_path):
    s = settings(tmp_path, batch_min_wait_s=0)
    app = create_app(s, provider_factory=FakeBatch, start_worker=False)
    store, w = app.state.store, app.state.worker
    v = _video_with(tmp_path / "lib/Show", "Show S01E01.mkv", _srt(40, last_len_s=65))
    job = store.enqueue("manual", "Show S01E01", str(v), "ru", None, "batch")
    store._q("UPDATE jobs SET wait_since=? WHERE id=?", (time.time() - 10 * 3600, job.id))  # already waited for Bazarr
    w.submit_batch()
    assert store.get(job.id).state == "batched" and '"origin": "embedded"' in store.get(job.id).source_pin
    side = v.with_name("Show S01E01.en.srt")                          # a complete subtitle lands meanwhile
    side.write_text(_srt(70), encoding="utf-8")
    old(side)
    w.poll_batches()
    j = store.get(job.id)
    assert "40 cues from en (embedded)" in j.detail and j.state == "needs_review"   # not re-planned on the new file


def test_opening_the_database_elsewhere_leaves_a_running_job_alone(tmp_path):
    from captionizerr.store import Store
    app = create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)
    store = app.state.store
    j = store.enqueue("manual", "x", "/x.mkv", "ru", None)
    store.next_queued()                                   # now running
    Store(tmp_path / "config" / "captionizerr.db")        # e.g. the CLI, or a script
    assert store.get(j.id).state == "running"
    create_app(settings(tmp_path), provider_factory=FakeProvider, start_worker=False)   # the service restarting
    assert store.get(j.id).state == "queued"
