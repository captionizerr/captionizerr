"""Housekeeping, run daily by the worker and on demand from the Settings page.

What grows, and how it is kept in check:
- config/tmp, cache/src-*      leftovers of interrupted extractions -> removed after a day
- config/cache                 extracted source tracks -> dropped after 60 days unused, capped at 1 GB (LRU)
- config/history               editor backups -> newest `keep_history` per file, none older than `keep_days`
                               (the newest always stays); folders of files that no longer exist go after 30 days
- config/sources               the source each translation was made from (for re-timing after an upgrade)
                               -> removed 30 days after its translation file is gone
- config/feedback              correction logs -> last 1000 lines per series
- config/backup                daily consistent DB snapshot (newest 7): back up this folder, not the live db
- jobs table                   unpaid skipped/no-source/failed rows older than `keep_days` (paid rows = ledger, kept)
- library orphans              our `*.captionizerr.*` files whose video is gone (Sonarr/Radarr upgrade or
                               delete) -> moved to config/orphans after being seen twice 24 h apart, deleted
                               from there after 30 days. Nothing else in the library is ever touched.
Docker image and log cleanup happen outside the container (deploy script, compose logging limits).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from .writer import _SIDECAR

log = logging.getLogger("captionizerr.maintenance")
DAY = 86400
CACHE_CAP = 1 << 30
MAX_ORPHANS = 20
# download/working folders inside a library root: never hold finished videos, can be huge
SKIP_DIRS = {"torrents", "incomplete", "downloading", "temp", "tmp", "@eadir", "#recycle", "$recycle.bin", ".recycle"}


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def storage(s) -> dict[str, int]:
    d = Path(s.data_dir)
    return {k: _size(d / k) if (d / k).exists() else 0
            for k in ("cache", "history", "orphans", "feedback", "tmp", "context", "backup", "sources")} | {
        "db": sum(_size(p) for p in d.glob("captionizerr.db*"))}


def _old(p: Path, days: float) -> bool:
    try:
        return p.stat().st_mtime < time.time() - days * DAY
    except OSError:
        return False


def clean_tmp(s) -> int:
    n = 0
    for base, pat in ((Path(s.data_dir) / "tmp", "*"), (Path(s.cache_dir), "src-*")):
        if base.is_dir():
            for p in base.glob(pat):
                if _old(p, 1):
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                    n += 1
    return n


def clean_cache(s, days: int = 60, cap: int = CACHE_CAP) -> int:
    c = Path(s.cache_dir)
    if not c.is_dir():
        return 0
    files = [p for p in c.iterdir() if p.is_file()]
    n = 0
    for p in files:
        if _old(p, days):
            p.unlink(missing_ok=True)
            n += 1
    files = sorted((p for p in c.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while files and total > cap:
        p = files.pop(0)
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        n += 1
    return n


def clean_history(s) -> int:
    h = Path(s.history_dir)
    if not h.is_dir():
        return 0
    n = 0
    for d in h.iterdir():
        if not d.is_dir():
            continue
        versions = sorted((p for p in d.iterdir() if p.name != "path.txt"), key=lambda p: p.name, reverse=True)
        for i, p in enumerate(versions):
            if i == 0:
                continue                      # the newest backup always stays
            if i >= s.keep_history or _old(p, s.keep_days):
                p.unlink(missing_ok=True)
                n += 1
        target = d / "path.txt"
        gone = target.is_file() and not Path(target.read_text(encoding="utf-8").strip()).exists()
        if gone and _old(target, 30):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def clean_sources(s) -> int:
    d = Path(s.data_dir) / "sources"
    n = 0
    if d.is_dir():
        for meta in d.glob("*.path"):
            out = Path(meta.read_text(encoding="utf-8").strip())
            if not out.exists() and _old(meta, 30):
                for f in list(d.glob(meta.stem + ".*")):
                    f.unlink(missing_ok=True)
                n += 1
    return n


def clean_feedback(s, keep: int = 1000) -> int:
    f = Path(s.feedback_dir)
    n = 0
    if f.is_dir():
        for p in f.glob("*.jsonl"):
            lines = p.read_text(encoding="utf-8").splitlines()
            if len(lines) > keep:
                p.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
                n += len(lines) - keep
    return n


def orphans(s) -> list[Path]:
    """Our subtitle files whose video no longer exists (scan of the library roots)."""
    out = []
    for root in s.library_roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            ours = [f for f in files if _SIDECAR.match(f)]
            if not ours:
                continue
            stems = {os.path.splitext(f)[0] for f in files if f.lower().endswith(s.VIDEO_EXT)}
            for f in ours:
                if _SIDECAR.match(f).group("stem") not in stems:
                    out.append(Path(dirpath) / f)
    return out


def quarantine(s, p: Path) -> Path | None:
    """Move one of OUR files out of the library into config/orphans/<date>/<original path>."""
    p = Path(p)
    if not (p.is_file() and not p.is_symlink() and _SIDECAR.match(p.name)):
        return None
    rel = p.relative_to("/") if p.is_absolute() else p
    dest = Path(s.data_dir) / "orphans" / time.strftime("%Y-%m-%d") / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest)
    p.unlink()
    return dest


def clean_orphans(s, state: dict) -> tuple[int, int]:
    """Quarantine orphans seen in two sweeps at least 20 h apart (a rename in progress is not an orphan).
    Returns (moved, purged). `state` maps path -> first-seen time and is persisted by the caller."""
    now, moved = time.time(), 0
    seen = {}
    q = Path(s.data_dir) / "orphans"
    found = orphans(s)
    if len(found) > MAX_ORPHANS:
        # dozens of videos vanishing at once is a broken mount or a library move, not upgrades
        raise RuntimeError(f"{len(found)} orphans in one sweep (> {MAX_ORPHANS}); nothing moved, check the library mount")
    for p in found:
        first = state.get(str(p), now)
        seen[str(p)] = first
        if now - first < 20 * 3600:
            continue
        if quarantine(s, p):
            moved += 1
            seen.pop(str(p), None)
            log.info("orphan moved out of the library: %s", p)
    state.clear()
    state.update(seen)
    purged = 0
    if q.is_dir():
        for d in q.iterdir():
            if d.is_dir() and _old(d, 30):
                shutil.rmtree(d, ignore_errors=True)
                purged += 1
    return moved, purged


def run(s, store) -> dict:
    t0 = time.time()
    rep: dict = {}
    for name, fn in (("tmp", clean_tmp), ("cache", clean_cache), ("history", clean_history),
                     ("sources", clean_sources), ("feedback_lines", clean_feedback)):
        try:
            rep[name] = fn(s)
        except Exception as e:                    # one failing step must not stop the others
            rep[name] = f"error: {e}"
    try:
        rep["db_snapshot"] = store.snapshot(Path(s.data_dir) / "backup").name
    except Exception as e:
        rep["db_snapshot"] = f"error: {e}"
    try:
        rep["jobs"] = store.prune_jobs(s.keep_days)
    except Exception as e:
        rep["jobs"] = f"error: {e}"
    try:
        st = store.load_settings().get("_orphan_seen", {})
        moved, purged = clean_orphans(s, st)
        store.save_settings({"_orphan_seen": st})
        rep["orphans_moved"], rep["orphans_purged"], rep["orphans_pending"] = moved, purged, len(st)
    except Exception as e:
        rep["orphans_moved"] = f"error: {e}"
    rep["seconds"] = round(time.time() - t0, 1)
    rep["at"] = int(time.time())
    store.save_settings({"_maintenance_last": rep})
    log.info("maintenance: %s", json.dumps(rep))
    return rep
