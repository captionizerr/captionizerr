"""Tell Plex a folder changed: find the library section whose location holds the file and ask it
to scan just that folder (`/library/sections/{id}/refresh?path=`), not the whole library."""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

from .jellyfin import map_path

log = logging.getLogger("captionizerr.plex")


def _get(url: str, token: str, endpoint: str, **q):
    q["X-Plex-Token"] = token
    req = urllib.request.Request(f"{url.rstrip('/')}{endpoint}?{urllib.parse.urlencode(q)}",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
        return json.loads(body) if body else {}


def sections(url: str, token: str) -> list[tuple[str, list[str]]]:
    data = _get(url, token, "/library/sections")
    out = []
    for d in (data.get("MediaContainer") or {}).get("Directory", []):
        out.append((str(d["key"]), [loc["path"] for loc in d.get("Location", [])]))
    return out


def refresh(url: str, token: str, paths: list[str], mapping: list[tuple[str, str]]) -> int:
    """Returns how many folders Plex was asked to scan."""
    if not url or not token or not paths:
        return 0
    try:
        secs = sections(url, token)
    except Exception as e:
        log.warning("Plex: listing sections failed: %s", e)
        return 0
    done = set()
    for p in paths:
        folder = os.path.dirname(map_path(p, mapping))
        for key, locs in secs:
            if any(folder == l.rstrip("/") or folder.startswith(l.rstrip("/") + "/") for l in locs):
                if (key, folder) in done:
                    break
                try:
                    _get(url, token, f"/library/sections/{key}/refresh", path=folder)
                    done.add((key, folder))
                except Exception as e:
                    log.warning("Plex refresh of %s failed: %s", folder, e)
                break
    return len(done)
