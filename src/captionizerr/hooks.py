"""Parse Sonarr/Radarr webhook payloads into work items.

Handled events: Download (new import or upgrade) -> translate; Rename -> move
our own sidecar subtitles; Test -> acknowledge. Everything else is ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Item:
    video_path: str
    title: str
    series_key: str | None


@dataclass
class Rename:
    old_path: str
    new_path: str


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def valid_key(key) -> str | None:
    """A series key as slug() makes it, or None. Series keys become file names
    (context/<key>.md, feedback/<key>.jsonl): never trust one from a request."""
    k = str(key or "")
    return k if len(k) <= 120 and _KEY.fullmatch(k) else None


def _join(folder: str | None, rel: str | None, full: str | None) -> str | None:
    if full:
        return full
    if folder and rel:
        return folder.rstrip("/") + "/" + rel
    return None


def parse_sonarr(p: dict) -> tuple[str, list[Item], list[Rename]]:
    ev = p.get("eventType", "")
    series = p.get("series") or {}
    title = series.get("title") or "?"
    if ev == "Download":
        f = p.get("episodeFile") or {}
        path = _join(series.get("path"), f.get("relativePath"), f.get("path"))
        eps = p.get("episodes") or []
        tag = ", ".join(f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}" for e in eps)
        return ev, ([Item(path, f"{title} {tag}".strip(), slug(title))] if path else []), []
    if ev == "Rename":
        rn = [Rename(r.get("previousPath"), r.get("path")) for r in p.get("renamedEpisodeFiles") or []
              if r.get("previousPath") and r.get("path")]
        return ev, [], rn
    return ev, [], []


def parse_radarr(p: dict) -> tuple[str, list[Item], list[Rename]]:
    ev = p.get("eventType", "")
    movie = p.get("movie") or {}
    title = f"{movie.get('title') or '?'} ({movie.get('year') or ''})".replace(" ()", "")
    if ev == "Download":
        f = p.get("movieFile") or {}
        path = _join(movie.get("folderPath"), f.get("relativePath"), f.get("path"))
        return ev, ([Item(path, title, slug(movie.get("title") or "movie"))] if path else []), []
    if ev == "Rename":
        rn = [Rename(r.get("previousPath"), r.get("path")) for r in p.get("renamedMovieFiles") or []
              if r.get("previousPath") and r.get("path")]
        return ev, [], rn
    return ev, [], []
