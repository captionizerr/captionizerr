"""Minimal Sonarr/Radarr API client for the library view."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


class ArrError(RuntimeError):
    pass


class Arr:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    def get(self, path: str, **params):
        if not self.configured:
            raise ArrError("not configured")
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(f"{self.url}/api/v3{path}{q}", headers={"X-Api-Key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            raise ArrError(str(e)) from e


def sonarr_series(a: Arr) -> list[dict]:
    out = []
    for s in a.get("/series"):
        st = s.get("statistics") or {}
        out.append({"id": s["id"], "title": s["title"], "year": s.get("year"), "path": s.get("path"), "overview": s.get("overview") or "",
                    "files": st.get("episodeFileCount", 0), "episodes": st.get("totalEpisodeCount", 0)})
    return sorted(out, key=lambda x: x["title"].lower())


def sonarr_episodes(a: Arr, series_id: int) -> list[dict]:
    files = {f["id"]: f for f in a.get("/episodefile", seriesId=series_id)}
    eps = []
    for e in a.get("/episode", seriesId=series_id):
        f = files.get(e.get("episodeFileId"))
        if not f:
            continue
        eps.append({"season": e["seasonNumber"], "episode": e["episodeNumber"], "title": e.get("title") or "",
                    "path": f.get("path"), "size": f.get("size", 0)})
    return sorted(eps, key=lambda x: (x["season"], x["episode"]))


def radarr_movies(a: Arr) -> list[dict]:
    out = []
    for m in a.get("/movie"):
        f = m.get("movieFile") or {}
        if not f.get("path"):
            continue
        out.append({"id": m["id"], "title": m["title"], "year": m.get("year"), "path": f["path"], "size": f.get("size", 0)})
    return sorted(out, key=lambda x: x["title"].lower())


def sonarr_ids_for_path(a: Arr, path: str) -> tuple[int, int] | None:
    """(seriesId, episodeId) of the episode whose file is `path` (Sonarr's own path form)."""
    for s in a.get("/series"):
        root = (s.get("path") or "").rstrip("/") + "/"
        if not path.startswith(root):
            continue
        files = [f for f in a.get("/episodefile", seriesId=s["id"]) if f.get("path") == path]
        if not files:
            return None
        for e in a.get("/episode", seriesId=s["id"]):
            if e.get("episodeFileId") == files[0]["id"]:
                return s["id"], e["id"]
    return None


def radarr_id_for_path(a: Arr, path: str) -> int | None:
    for m in a.get("/movie"):
        if (m.get("movieFile") or {}).get("path") == path:
            return m["id"]
    return None
