"""Tell Jellyfin (or Emby: same API) a file changed so new external subtitles show up without a
full library scan. `refresh_all` also covers Plex."""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("captionizerr.jellyfin")


def map_path(path: str, mapping: list[tuple[str, str]]) -> str:
    for src, dst in mapping:
        if path.startswith(src):
            return dst + path[len(src):]
    return path


def media_updated(url: str, key: str, paths: list[str], mapping: list[tuple[str, str]]) -> bool:
    if not url or not key or not paths:
        return False
    body = {"Updates": [{"Path": map_path(p, mapping), "UpdateType": "Modified"} for p in paths]}
    req = urllib.request.Request(url.rstrip("/") + "/Library/Media/Updated", data=json.dumps(body).encode(),
                                 method="POST", headers={"X-Emby-Token": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:     # a missed refresh only delays the track appearing
        log.warning("Jellyfin refresh failed: %s", e)
        return False


def refresh_all(s, paths: list[str]) -> None:
    """Every configured media server. Paths are in the *arr form (what webhooks and jobs carry)."""
    if not paths:
        return
    media_updated(s.jellyfin_url, s.jellyfin_key, paths, s.jellyfin_path_map)
    if getattr(s, "plex_url", "") and getattr(s, "plex_token", ""):
        from .plex import refresh
        refresh(s.plex_url, s.plex_token, paths, s.plex_path_map)
