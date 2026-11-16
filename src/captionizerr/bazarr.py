"""Bazarr as a source of last resort: when a video has no usable text subtitle, ask Bazarr to
search and download one in a source language. Bazarr queues the search and (with subsync on)
syncs the file to the audio, then writes `<video>.<lang>.srt`, which the worker picks up."""
from __future__ import annotations

import urllib.parse
import urllib.request


class BazarrError(RuntimeError):
    pass


class Bazarr:
    def __init__(self, url: str, key: str):
        self.url, self.key = (url or "").rstrip("/"), key

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    def _patch(self, path: str, **params) -> None:
        if not self.configured:
            raise BazarrError("Bazarr not configured")
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.url}/api{path}?{q}", method="PATCH", headers={"X-API-KEY": self.key})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status >= 300:
                    raise BazarrError(f"HTTP {r.status}")
        except BazarrError:
            raise
        except Exception as e:
            raise BazarrError(str(e)) from e

    def request_episode(self, series_id: int, episode_id: int, lang: str) -> None:
        self._patch("/episodes/subtitles", seriesid=series_id, episodeid=episode_id, language=lang,
                    forced="false", hi="false")

    def request_movie(self, radarr_id: int, lang: str) -> None:
        self._patch("/movies/subtitles", radarrid=radarr_id, language=lang, forced="false", hi="false")
