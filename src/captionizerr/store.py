"""SQLite job store and spend ledger. One file, WAL mode, safe for one worker thread + the web app."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  source TEXT NOT NULL,          -- sonarr | radarr | manual
  title TEXT NOT NULL,
  series_key TEXT,               -- context file key, e.g. "northwind"
  video_path TEXT NOT NULL,
  lang TEXT NOT NULL,
  state TEXT NOT NULL,           -- see STATES
  detail TEXT DEFAULT '',
  output_path TEXT DEFAULT '',
  cost_usd REAL DEFAULT 0,
  usage TEXT DEFAULT '{}',
  month TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS alerts (month TEXT, level TEXT, PRIMARY KEY (month, level));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shares (
  path TEXT PRIMARY KEY,         -- our subtitle file
  url TEXT DEFAULT '',
  at REAL NOT NULL,
  already INTEGER DEFAULT 0      -- OpenSubtitles already had this exact file
);
CREATE TABLE IF NOT EXISTS series (
  key TEXT PRIMARY KEY,          -- slug of the title, same as jobs.series_key
  title TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  langs TEXT DEFAULT '',         -- JSON list; empty = use the global languages
  tone TEXT DEFAULT ''           -- empty = use the global tone
);
"""

STATES = ("queued", "running", "batched", "waiting_source", "done", "needs_review", "failed", "paused_budget",
          "skipped", "no_source")
ACTIVE = ("queued", "running", "paused_budget", "batched", "waiting_source")
# columns added after M1; created on start if missing
MIGRATIONS = {"mode": "TEXT DEFAULT 'live'", "batch_id": "TEXT DEFAULT ''", "wait_since": "REAL DEFAULT 0",
              "est_usd": "REAL DEFAULT 0", "model": "TEXT DEFAULT ''", "redo": "INTEGER DEFAULT 0",
              "source_pin": "TEXT DEFAULT ''"}


def month_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


@dataclass
class Job:
    id: int
    created: float
    updated: float
    source: str
    title: str
    series_key: str | None
    video_path: str
    lang: str
    state: str
    detail: str
    output_path: str
    cost_usd: float
    usage: str
    month: str
    mode: str = "live"           # live | batch (backlog through the Batch API)
    batch_id: str = ""
    wait_since: float = 0.0      # waiting_source: when we asked Bazarr
    est_usd: float = 0.0         # estimate of a submitted batch job (reserved against the cap until paid)
    model: str = ""              # the model that did the work (per-model cost averages)
    redo: int = 0                # 1: replace our existing translation (old one goes to editor history)
    source_pin: str = ""         # JSON {path, lang, origin}: the source a submitted batch was built from


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)").fetchall()}
        for col, decl in MIGRATIONS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    def requeue_interrupted(self) -> int:
        """A crash or restart mid-job leaves it 'running': requeue it. Only the service calls this,
        at its own start; a CLI or script opening the same database must not touch a live job."""
        return self._q("UPDATE jobs SET state='queued', updated=? WHERE state='running'", (time.time(),)).rowcount

    def _q(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args)

    def enqueue(self, source: str, title: str, video_path: str, lang: str, series_key: str | None,
                mode: str = "live", redo: bool = False) -> Job | None:
        """Add a job unless the same video+language is already active. Returns the new job or None."""
        dup = self._q(f"SELECT id FROM jobs WHERE video_path=? AND lang=? AND state IN ({','.join('?' * len(ACTIVE))})",
                      (video_path, lang, *ACTIVE)).fetchone()
        if dup:
            return None
        now = time.time()
        cur = self._q("INSERT INTO jobs (created, updated, source, title, series_key, video_path, lang, state, mode, redo) "
                      "VALUES (?,?,?,?,?,?,?, 'queued', ?, ?)", (now, now, source, title, series_key, video_path, lang,
                                                                "batch" if mode == "batch" else "live", int(redo)))
        return self.get(cur.lastrowid)

    def get(self, job_id: int) -> Job | None:
        r = self._q("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job(**dict(r)) if r else None

    def next_queued(self, include_batch: bool = False) -> Job | None:
        """Next live job (batch-mode jobs too when the provider can't batch); marks it running."""
        where = "state='queued'" + ("" if include_batch else " AND mode='live'")
        with self._lock:
            r = self.db.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY id LIMIT 1").fetchone()
            if not r:
                return None
            self.db.execute("UPDATE jobs SET state='running', updated=? WHERE id=?", (time.time(), r["id"]))
            d = dict(r)
            d["state"] = "running"
            return Job(**d)

    def finish(self, job_id: int, state: str, detail: str = "", output_path: str = "",
               cost_usd: float = 0.0, usage: dict | None = None, model: str = ""):
        self._q("UPDATE jobs SET state=?, detail=?, output_path=?, cost_usd=?, usage=?, month=?, updated=?, "
                "model=CASE WHEN ?<>'' THEN ? ELSE model END WHERE id=?",
                (state, detail[:2000], output_path, cost_usd, json.dumps(usage or {}), month_now(), time.time(),
                 model, model, job_id))

    def record(self, source: str, title: str, video_path: str, lang: str, series_key: str | None,
               detail: str, cost_usd: float, usage: dict | None = None, model: str = "") -> int:
        """A finished, already-paid request outside the queue (line re-translation, series notes)."""
        now = time.time()
        cur = self._q("INSERT INTO jobs (created, updated, source, title, series_key, video_path, lang, state, detail,"
                      " output_path, cost_usd, usage, month, model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now, now, source, title, series_key, video_path, lang, "done", detail[:2000], "",
                       cost_usd, json.dumps(usage or {}), month_now(), model))
        return cur.lastrowid

    # ---- backlog through the Batch API
    def queued_batch(self) -> list[Job]:
        return [Job(**dict(r)) for r in self._q("SELECT * FROM jobs WHERE state='queued' AND mode='batch' ORDER BY id").fetchall()]

    def claim(self, job_id: int, from_state: str, to_state: str, detail: str = "") -> bool:
        return self._q("UPDATE jobs SET state=?, detail=?, updated=? WHERE id=? AND state=?",
                       (to_state, detail[:2000], time.time(), job_id, from_state)).rowcount == 1

    def set_batched(self, job_id: int, batch_id: str, detail: str, est_usd: float = 0.0, source_pin: str = ""):
        self._q("UPDATE jobs SET state='batched', batch_id=?, detail=?, est_usd=?, source_pin=?, updated=? WHERE id=?",
                (batch_id, detail[:2000], est_usd, source_pin, time.time(), job_id))

    def reserved(self) -> float:
        """Estimated cost of batch requests already submitted but not yet paid."""
        r = self._q("SELECT COALESCE(SUM(est_usd),0) FROM jobs WHERE state='batched'").fetchone()
        return float(r[0])

    def batched(self) -> dict[str, list[Job]]:
        out: dict[str, list[Job]] = {}
        for r in self._q("SELECT * FROM jobs WHERE state='batched' ORDER BY id").fetchall():
            out.setdefault(r["batch_id"], []).append(Job(**dict(r)))
        return out

    def to_live(self, job_id: int, detail: str):
        self._q("UPDATE jobs SET mode='live', state='queued', batch_id='', detail=?, updated=? WHERE id=?",
                (detail[:2000], time.time(), job_id))

    # ---- waiting for Bazarr to deliver a source subtitle
    def set_waiting(self, job_id: int, detail: str):
        self._q("UPDATE jobs SET state='waiting_source', detail=?, wait_since=CASE WHEN wait_since>0 THEN wait_since "
                "ELSE ? END, updated=? WHERE id=?", (detail[:2000], time.time(), time.time(), job_id))

    def waiting(self) -> list[Job]:
        return [Job(**dict(r)) for r in self._q("SELECT * FROM jobs WHERE state='waiting_source' ORDER BY id").fetchall()]

    # ---- shared on OpenSubtitles
    def record_share(self, path: str, url: str, already: bool) -> None:
        self._q("INSERT INTO shares (path, url, at, already) VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "url=excluded.url, at=excluded.at, already=excluded.already", (path, url, time.time(), int(already)))

    def replaced_by(self, job) -> int | None:
        """The id of a later finished translation of the same video and language, if any (its file replaced this one's)."""
        if job.source in ("edit", "notes", "forced", "sync"):
            return None
        r = self._q("SELECT id FROM jobs WHERE video_path=? AND lang=? AND id>? AND source NOT IN ('edit','notes','forced','sync') "
                    "AND state IN ('done','needs_review') ORDER BY id LIMIT 1", (job.video_path, job.lang, job.id)).fetchone()
        return r[0] if r else None

    def model_for(self, video_path: str, lang: str, forced: bool = False) -> str:
        """The model of the latest finished job that wrote this video's translation (or its forced track)."""
        src = "source='forced'" if forced else "source NOT IN ('edit','notes','forced','sync')"
        r = self._q(f"SELECT model FROM jobs WHERE video_path=? AND lang=? AND {src} AND model<>'' "
                    "AND state IN ('done','needs_review') ORDER BY updated DESC LIMIT 1", (video_path, lang)).fetchone()
        return r[0] if r else ""

    def jobs_for(self, video_path: str, lang: str, limit: int = 30) -> list[Job]:
        """Everything that happened to one file's translation, oldest first."""
        return [Job(**dict(r)) for r in self._q("SELECT * FROM jobs WHERE video_path=? AND lang=? ORDER BY created "
                                                "LIMIT ?", (video_path, lang, limit)).fetchall()]

    def follow_rename(self, old_video: str, new_video: str, subs: list[tuple[str, str]]) -> int:
        """Sonarr/Radarr renamed a video and we moved our subtitle files: keep the job history,
        Edit links and share records pointing at the new names. Returns the jobs updated."""
        n = self._q("UPDATE jobs SET video_path=? WHERE video_path=?", (new_video, old_video)).rowcount
        for old, new in subs:
            self._q("UPDATE jobs SET output_path=? WHERE output_path=?", (new, old))
            self._q("UPDATE shares SET path=? WHERE path=?", (new, old))
        return n

    def review_job(self, video_path: str, lang: str) -> Job | None:
        """The translation job of this file that is waiting for review, if any."""
        r = self._q("SELECT * FROM jobs WHERE video_path=? AND lang=? AND state='needs_review' "
                    "AND source NOT IN ('edit','notes','forced','sync') ORDER BY created DESC LIMIT 1",
                    (video_path, lang)).fetchone()
        return Job(**dict(r)) if r else None

    def mark_reviewed(self, job_id: int, how: str) -> bool:
        """A person looked at it: needs_review becomes done; the detail says how."""
        return self._q("UPDATE jobs SET state='done', detail=detail || ?, updated=? WHERE id=? AND state='needs_review'",
                       (f"; {how} {time.strftime('%d %b %H:%M')}", time.time(), job_id)).rowcount == 1

    def jobs_by_video(self, paths: list[str], lang: str) -> dict[str, list[Job]]:
        """video_path -> its jobs in `lang`, oldest first (one query for a whole season)."""
        out: dict[str, list[Job]] = {}
        paths = [p for p in paths if p]
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            for r in self._q(f"SELECT * FROM jobs WHERE lang=? AND video_path IN ({','.join('?' * len(chunk))}) "
                             "ORDER BY created", (lang, *chunk)).fetchall():
                j = Job(**dict(r))
                out.setdefault(j.video_path, []).append(j)
        return out

    def share(self, path: str) -> dict | None:
        r = self._q("SELECT * FROM shares WHERE path=?", (path,)).fetchone()
        return dict(r) if r else None

    # ---- housekeeping
    def snapshot(self, dest: Path, keep: int = 7) -> Path:
        """Consistent copy of the live database (SQLite online backup; safe while running).
        Keeps the newest `keep` copies. File backups should copy these, not the live db/-wal/-shm."""
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"captionizerr-{time.strftime('%Y%m%d')}.db"
        tmp = out.with_suffix(".db.tmp")
        tmp.unlink(missing_ok=True)
        target = sqlite3.connect(str(tmp))
        with self._lock:
            self.db.backup(target)
        target.close()
        tmp.replace(out)
        for old in sorted(dest.glob("captionizerr-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
        return out

    def prune_jobs(self, days: int) -> int:
        """Forget old jobs that carry no money: skipped / no source / failed with zero cost.
        Paid rows stay (they are the spend ledger)."""
        cutoff = time.time() - days * 86400
        n = self._q("DELETE FROM jobs WHERE updated < ? AND cost_usd = 0 AND state IN ('skipped','no_source','failed')",
                    (cutoff,)).rowcount
        self._q("PRAGMA wal_checkpoint(TRUNCATE)")
        return n

    def set_state(self, job_id: int, state: str, detail: str = ""):
        self._q("UPDATE jobs SET state=?, detail=?, updated=? WHERE id=?", (state, detail[:2000], time.time(), job_id))

    def resume_paused(self) -> int:
        return self._q("UPDATE jobs SET state='queued', updated=? WHERE state='paused_budget'", (time.time(),)).rowcount

    def spent(self, month: str | None = None) -> float:
        r = self._q("SELECT COALESCE(SUM(cost_usd),0) FROM jobs WHERE month=?", (month or month_now(),)).fetchone()
        return float(r[0])

    def list(self, limit: int = 100) -> list[Job]:
        return [Job(**dict(r)) for r in self._q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._q("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()}

    # ---- settings (UI overrides of environment defaults)
    def load_settings(self) -> dict:
        return {r[0]: json.loads(r[1]) for r in self._q("SELECT key, value FROM settings").fetchall()}

    def delete_settings(self, keys: list[str]) -> None:
        for k in keys:
            self._q("DELETE FROM settings WHERE key=?", (k,))

    def save_settings(self, values: dict) -> None:
        for k, v in values.items():
            self._q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)))

    # ---- per-series overrides
    def series(self, key: str) -> dict | None:
        r = self._q("SELECT * FROM series WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["langs"] = json.loads(d["langs"]) if d["langs"] else []
        d["enabled"] = bool(d["enabled"])
        return d

    def save_series(self, key: str, title: str, enabled: bool, langs: list[str], tone: str) -> None:
        self._q("INSERT INTO series (key, title, enabled, langs, tone) VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "title=excluded.title, enabled=excluded.enabled, langs=excluded.langs, tone=excluded.tone",
                (key, title, int(enabled), json.dumps(langs) if langs else "", tone or ""))

    def series_rows(self) -> dict[str, str]:
        """key -> title of every series that has saved settings."""
        return {r["key"]: r["title"] for r in self._q("SELECT key, title FROM series").fetchall()}

    def delete_series(self, key: str) -> None:
        self._q("DELETE FROM series WHERE key=?", (key,))

    def requeue(self, job_id: int) -> bool:
        # a needs_review job already wrote its file, so its retry must be allowed to replace it
        return self._q("UPDATE jobs SET state='queued', detail='', wait_since=0, updated=?, "
                       "redo=CASE WHEN state='needs_review' THEN 1 ELSE redo END WHERE id=? AND state IN "
                       "('failed','needs_review','no_source','paused_budget','waiting_source')", (time.time(), job_id)).rowcount == 1

    def avg_cost(self, default: float = 0.31, model: str | None = None, min_jobs: int = 1):
        """Average cost of the last 50 translations; with `model`, only jobs that model did (so switching
        vendor or model doesn't carry the old model's prices into estimates). Returns `default` when
        there are fewer than `min_jobs` such jobs."""
        where = "state IN ('done','needs_review') AND source NOT IN ('edit','notes','forced','sync') AND cost_usd > 0"
        args: tuple = ()
        if model is not None:
            where += " AND model=?"
            args = (model,)
        r = self._q(f"SELECT AVG(cost_usd), COUNT(*) FROM (SELECT cost_usd FROM jobs WHERE {where} "
                    "ORDER BY id DESC LIMIT 50)", args).fetchone()
        return float(r[0]) if r and r[1] and r[1] >= min_jobs else default

    def spend_by_month(self, n: int = 6) -> list[tuple[str, float, int]]:
        return [(r[0], float(r[1]), int(r[2])) for r in self._q(
            "SELECT month, SUM(cost_usd), COUNT(*) FROM jobs WHERE month != '' GROUP BY month ORDER BY month DESC LIMIT ?",
            (n,)).fetchall()]

    def alert_once(self, level: str, month: str | None = None) -> bool:
        """True the first time this alert level fires in the month."""
        try:
            self._q("INSERT INTO alerts (month, level) VALUES (?,?)", (month or month_now(), level))
            return True
        except sqlite3.IntegrityError:
            return False
