import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from captionizerr.app import create_app
from captionizerr.config import Settings
from captionizerr.pricing import Usage
from captionizerr.providers.base import ProviderResult

KEY = "test-key"
HAS_FFMPEG = shutil.which("ffmpeg") is not None

SRT = "".join(f"{i}\n00:00:{i * 3:02d},000 --> 00:00:{i * 3 + 2:02d},500\nLine number {i} of the test episode.\n\n" for i in range(1, 9))


class FakeProvider:
    name, model = "fake", "claude-opus-5-5"

    def count_tokens(self, system, context, work):
        return 1000

    def complete(self, system, context, work):
        lines = [l for l in work.splitlines() if "|" in l and l.split("|")[0].isdigit()]
        out = "\n".join(f"{l.split('|')[0]}|Строка номер {l.split('|')[0]} тестового эпизода." for l in lines)
        return ProviderResult(out, Usage(1000, 800), self.model, "end_turn")


def make_video(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    srt = folder / "src.srt"
    srt.write_text(SRT, encoding="utf-8")
    video = folder / name
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=30",
                    "-i", str(srt), "-map", "0", "-map", "1", "-c:v", "libx264", "-c:s", "srt",
                    "-metadata:s:s:0", "language=eng", str(video)], check=True)
    srt.unlink()
    return video


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], monthly_cap_usd=20.0,
                 library_roots=[str(tmp_path / "lib")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    return app, TestClient(app), tmp_path


def sonarr_download(path: Path, title="Northwind"):
    return {"eventType": "Download", "series": {"id": 40, "title": title, "path": str(path.parent.parent)},
            "episodes": [{"seasonNumber": 2, "episodeNumber": 4}],
            "episodeFile": {"relativePath": f"{path.parent.name}/{path.name}", "path": str(path)}, "isUpgrade": False}


def test_webhook_requires_key(svc):
    _, c, _ = svc
    assert c.post("/api/hooks/sonarr", json={"eventType": "Test"}).status_code == 401
    assert c.post(f"/api/hooks/sonarr?apikey={KEY}", json={"eventType": "Test"}).json()["queued"] == []


def test_sonarr_download_queues_once(svc, tmp_path):
    _, c, _ = svc
    p = tmp_path / "lib/Northwind/Season 2/Northwind - S02E04.mkv"
    r1 = c.post(f"/api/hooks/sonarr?apikey={KEY}", json=sonarr_download(p)).json()
    r2 = c.post(f"/api/hooks/sonarr?apikey={KEY}", json=sonarr_download(p)).json()
    assert len(r1["queued"]) == 1 and r2["queued"] == []          # duplicate while active is ignored
    job = c.get("/api/jobs").json()[0]
    assert job["title"] == "Northwind S02E04" and job["series_key"] == "northwind" and job["state"] == "queued"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
def test_worker_end_to_end(svc, tmp_path):
    app, c, _ = svc
    video = make_video(tmp_path / "lib/Northwind/Season 2", "Northwind - S02E04 - First Light.mkv")
    c.post(f"/api/hooks/sonarr?apikey={KEY}", json=sonarr_download(video))
    w = app.state.worker
    w.process(app.state.store.next_queued())
    job = c.get("/api/jobs").json()[0]
    assert job["state"] == "done", job
    out = video.with_name("Northwind - S02E04 - First Light.captionizerr.ru.srt")
    assert out.is_file() and "Строка номер 3" in out.read_text(encoding="utf-8")
    assert "00:00:09,000 --> 00:00:11,500" in out.read_text(encoding="utf-8")
    assert c.get("/health").json()["spent_month"] > 0
    # a second import of the same file is skipped, never overwritten
    c.post(f"/api/hooks/sonarr?apikey={KEY}", json=sonarr_download(video))
    w.process(app.state.store.next_queued())
    assert c.get("/api/jobs").json()[0]["state"] == "skipped"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
def test_budget_cap_pauses(tmp_path):
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], monthly_cap_usd=0.001,
                 library_roots=[str(tmp_path / "lib")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    video = make_video(tmp_path / "lib/Show/Season 1", "Show - S01E01.mkv")
    c.post(f"/api/hooks/sonarr?apikey={KEY}", json=sonarr_download(video, "Show"))
    app.state.worker.process(app.state.store.next_queued())
    job = c.get("/api/jobs").json()[0]
    assert job["state"] == "paused_budget" and "cap" in job["detail"]
    assert not list(video.parent.glob("*.captionizerr.*"))
    assert c.post(f"/api/jobs/resume?apikey={KEY}").json()["resumed"] == 1


def test_rename_moves_only_our_sidecars(svc, tmp_path):
    _, c, _ = svc
    d = tmp_path / "lib/Northwind/Season 2"
    d.mkdir(parents=True)
    old, new = d / "Northwind - S02E04 - Old.mkv", d / "Northwind - S02E04 - New.mkv"
    new.write_bytes(b"video")
    (d / "Northwind - S02E04 - Old.captionizerr.ru.srt").write_text("ours")
    (d / "Northwind - S02E04 - Old.ru.srt").write_text("community")
    r = c.post(f"/api/hooks/sonarr?apikey={KEY}", json={"eventType": "Rename", "series": {"title": "Northwind"},
               "renamedEpisodeFiles": [{"previousPath": str(old), "path": str(new)}]}).json()
    assert r["moved_sidecars"] == 1
    assert (d / "Northwind - S02E04 - New.captionizerr.ru.srt").read_text() == "ours"
    assert (d / "Northwind - S02E04 - Old.ru.srt").read_text() == "community"      # not ours: untouched


def test_status_page_and_metrics(svc):
    _, c, _ = svc
    assert "Captionizerr" in c.get("/").text and "/static/favicon.svg" in c.get("/").text
    m = c.get("/metrics").text
    assert "captionizerr_budget_usd 20.00" in m
    assert 'captionizerr_jobs{state="failed"} 0' in m      # present before any failure, so delta() rules work


def test_jellyfin_path_map():
    from captionizerr.jellyfin import map_path
    m = [("/downloads/", "/mnt/media/")]
    assert map_path("/downloads/complete/sonarr/Northwind/x.mkv", m) == "/mnt/media/complete/sonarr/Northwind/x.mkv"
    assert map_path("/other/x.mkv", m) == "/other/x.mkv"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
def test_worker_tells_jellyfin(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("captionizerr.worker.media_updated", lambda url, key, paths, m: calls.append((url, paths, m)))
    s = Settings(data_dir=tmp_path / "config", webhook_key=KEY, languages=["ru"], jellyfin_url="http://jf:8096",
                 library_roots=[str(tmp_path / "lib")],
                 jellyfin_key="k", jellyfin_path_map=[("/x/", "/y/")])
    app = create_app(s, provider_factory=FakeProvider, start_worker=False)
    c = TestClient(app)
    video = make_video(tmp_path / "lib/Show/Season 1", "Show - S01E01.mkv")
    c.post(f"/api/hooks/sonarr?apikey={KEY}", json=sonarr_download(video, "Show"))
    app.state.worker.process(app.state.store.next_queued())
    assert calls == [("http://jf:8096", [str(video)], [("/x/", "/y/")])]
