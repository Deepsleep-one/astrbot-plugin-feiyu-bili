"""Reading evidence store: readings, segments, frames, journal and reflections.

Additive schema on the plugin's own SQLite file (WAL, versioned with
PRAGMA user_version). Old tables from core.Store are never altered or dropped, so
rolling back to the previous plugin version stays safe. Nothing here stores
cookies, signed CDN URLs, tokens or private chat text.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 7

# Additive column migrations: an older file keeps its rows, we only add columns.
_ADDED_COLUMNS = {
    "replies": (("pinned", "TEXT NOT NULL DEFAULT '0'"),
                ("nested", "TEXT NOT NULL DEFAULT '0'")),
}

# Background vision decisions: what a round *would* look at, and why it did or did not.
SHADOW_WOULD_LOOK = "would_look"
SHADOW_LOOKED = "looked"
SHADOW_LOOK_FAILED = "look_failed"
SHADOW_SKIP = "skip"

JOURNAL_FIELDS = ("encounter", "material_facts", "author_view", "ai_inference",
                  "prior_judgment", "revision", "omissions", "next_response")
JOURNAL_LABELS = ("遇见与处境", "材料事实", "作者观点", "AI推测",
                  "先前判断", "认识修正", "遗漏", "下次如何回应")

STATE_PENDING = "pending_review"
STATE_SELECTED = "selected"
STATE_SKIPPED = "skipped"
STATE_WITHDRAWN = "withdrawn"
STATE_SUPERSEDED = "superseded"
STATE_ACTIVE = "active"
STATE_INVALIDATED = "invalidated"

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS readings(
  reading_id TEXT PRIMARY KEY, bvid TEXT NOT NULL, page INTEGER NOT NULL, cid INTEGER NOT NULL,
  revision INTEGER NOT NULL, scope TEXT NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL,
  part_title TEXT NOT NULL, duration REAL, fetched_at TEXT NOT NULL, source_type TEXT NOT NULL,
  subtitle_state TEXT NOT NULL, subtitle_reason TEXT NOT NULL, evidence TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS segments(
  reading_id TEXT NOT NULL, revision INTEGER NOT NULL, idx INTEGER NOT NULL,
  from_ms INTEGER NOT NULL, to_ms INTEGER NOT NULL, text TEXT NOT NULL, lang TEXT NOT NULL,
  digest TEXT NOT NULL, PRIMARY KEY(reading_id, revision, idx));
CREATE TABLE IF NOT EXISTS frame_jobs(
  job_id TEXT PRIMARY KEY, reading_id TEXT NOT NULL, revision INTEGER NOT NULL,
  generation INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
  source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  budget_json TEXT NOT NULL, result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS frames(
  frame_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, reading_id TEXT NOT NULL,
  revision INTEGER NOT NULL, idx INTEGER NOT NULL, target_ms INTEGER NOT NULL,
  actual_ms INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
  bytes INTEGER NOT NULL, digest TEXT NOT NULL, path TEXT NOT NULL, source TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visual_batches(
  batch_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, reading_id TEXT NOT NULL,
  revision INTEGER NOT NULL, provider_id TEXT NOT NULL, frame_ids TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT NOT NULL, usage_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(
  obs_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, reading_id TEXT NOT NULL,
  revision INTEGER NOT NULL, frame_id TEXT NOT NULL, actual_ms INTEGER NOT NULL,
  visible TEXT NOT NULL, interpretation TEXT NOT NULL, uncertainty TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journals(
  journal_id TEXT PRIMARY KEY, reading_id TEXT NOT NULL, revision INTEGER NOT NULL,
  version INTEGER NOT NULL, state TEXT NOT NULL, fields_json TEXT NOT NULL,
  refs_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reflections(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, topic TEXT NOT NULL, old_view TEXT NOT NULL,
  new_view TEXT NOT NULL, journal_id TEXT NOT NULL, refs_json TEXT NOT NULL,
  state TEXT NOT NULL, supersedes INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS interests(
  scope TEXT NOT NULL, topic TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(scope, topic));
CREATE TABLE IF NOT EXISTS memory_links(
  kind TEXT NOT NULL, bili_id TEXT NOT NULL, memory_id TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(kind, bili_id));
CREATE TABLE IF NOT EXISTS proposals(
  proposal_id TEXT PRIMARY KEY, scope TEXT NOT NULL, bvid TEXT NOT NULL,
  page INTEGER NOT NULL DEFAULT 1, title TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '', duration REAL, reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_proposals_scope ON proposals(scope, state, created_at);
CREATE TABLE IF NOT EXISTS vision_shadow(
  decision_id TEXT PRIMARY KEY, scope TEXT NOT NULL, bvid TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL DEFAULT '', subtitle_state TEXT NOT NULL DEFAULT '',
  interested TEXT NOT NULL DEFAULT '0', verdict TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', frames INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_shadow_scope ON vision_shadow(scope, created_at);
CREATE TABLE IF NOT EXISTS replies(
  reading_id TEXT NOT NULL, revision INTEGER NOT NULL, idx INTEGER NOT NULL,
  is_up TEXT NOT NULL DEFAULT '0', likes INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL, digest TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  pinned TEXT NOT NULL DEFAULT '0', nested TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY(reading_id, revision, idx));
CREATE TABLE IF NOT EXISTS danmaku(
  reading_id TEXT NOT NULL, revision INTEGER NOT NULL, idx INTEGER NOT NULL,
  at_ms INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL,
  repeated INTEGER NOT NULL DEFAULT 1, digest TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  PRIMARY KEY(reading_id, revision, idx));
CREATE TABLE IF NOT EXISTS asr_jobs(
  job_id TEXT PRIMARY KEY, reading_id TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 0, bvid TEXT NOT NULL DEFAULT '',
  cid INTEGER NOT NULL DEFAULT 0, scope TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
  engine TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '', declared_ms INTEGER NOT NULL DEFAULT 0,
  decoded_ms INTEGER NOT NULL DEFAULT 0, processed_ms INTEGER NOT NULL DEFAULT 0,
  coverage TEXT NOT NULL DEFAULT '', segments INTEGER NOT NULL DEFAULT 0,
  chars INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_asr_reading ON asr_jobs(reading_id, revision);
CREATE INDEX IF NOT EXISTS idx_asr_scope ON asr_jobs(scope, created_at);
CREATE TABLE IF NOT EXISTS asr_segments(
  job_id TEXT NOT NULL, idx INTEGER NOT NULL,
  from_ms INTEGER NOT NULL DEFAULT 0, to_ms INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL, digest TEXT NOT NULL DEFAULT '',
  avg_logprob REAL, no_speech REAL,
  PRIMARY KEY(job_id, idx));
CREATE TABLE IF NOT EXISTS asr_daily(
  day TEXT NOT NULL, scope TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, PRIMARY KEY(day, scope));
'''


def _now(moment=None) -> str:
    return (moment or datetime.now()).isoformat()


def _json_text(value) -> str:
    """Store JSON once: a caller may hand us a dict or an already-encoded string."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value or "{}"
    return json.dumps(value, ensure_ascii=False)


class ReadingStore:
    """One connection per instance; the same file is shared with core.Store."""

    def __init__(self, path, *, clock=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._clock = clock
        self.db.executescript("PRAGMA journal_mode=WAL;")
        self.db.executescript(_SCHEMA)
        self._add_missing_columns()
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            with self.db:
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)",
                                ("cursor_key", secrets.token_hex(32)))
        else:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)",
                                ("cursor_key", secrets.token_hex(32)))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _add_missing_columns(self) -> None:
        """Additive migration in one transaction: old rows keep their values.

        CREATE TABLE IF NOT EXISTS never adds a column to an existing table, so a
        file written by an older version is upgraded here instead of being rebuilt
        (rebuilding would mean copying user rows around for no reason).
        """
        for table, columns in _ADDED_COLUMNS.items():
            present = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if not present:
                continue
            missing = [(name, ddl) for name, ddl in columns if name not in present]
            if not missing:
                continue
            with self.db:
                for name, ddl in missing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ---- meta / cursor -------------------------------------------------
    def _meta(self, key: str) -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def cursor(self, reading_id: str, revision: int, offset: int, scope: str) -> str:
        """Opaque page cursor bound to one reading revision and one owner scope."""
        payload = f"{reading_id}|{int(revision)}|{int(offset)}|{scope_hash(scope)}"
        signature = hmac.new(self._meta("cursor_key").encode(), payload.encode(),
                             hashlib.sha256).hexdigest()[:24]
        return f"{reading_id}.{int(revision)}.{int(offset)}.{signature}"

    def read_cursor(self, token: str, scope: str) -> tuple[str, int, int]:
        """Validate a cursor; arbitrary paths, offsets or foreign owners are rejected."""
        text = str(token or "").strip()
        parts = text.split(".")
        if len(parts) != 4:
            raise ValueError("cursor 无效")
        reading_id, revision, offset, signature = parts
        try:
            revision_i, offset_i = int(revision), int(offset)
        except ValueError:
            raise ValueError("cursor 无效") from None
        if revision_i < 1 or offset_i < 0:
            raise ValueError("cursor 无效")
        expected = hmac.new(self._meta("cursor_key").encode(),
                            f"{reading_id}|{revision_i}|{offset_i}|{scope_hash(scope)}".encode(),
                            hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(expected, signature):
            raise ValueError("cursor 与当前阅读或会话不匹配")
        return reading_id, revision_i, offset_i

    # ---- readings ------------------------------------------------------
    def latest_revision(self, reading_id: str) -> int:
        row = self.db.execute("SELECT revision FROM readings WHERE reading_id=?",
                              (reading_id,)).fetchone()
        return row[0] if row else 0

    def begin_reading(self, **fields) -> dict:
        """Create a reading; a re-read of the same BV/page becomes revision + 1."""
        previous = self.db.execute(
            "SELECT reading_id, revision FROM readings WHERE bvid=? AND page=? ORDER BY revision DESC LIMIT 1",
            (fields["bvid"], int(fields["page"]))).fetchone()
        reading_id = fields["reading_id"]
        revision = 1
        if previous:
            reading_id = previous[0]
            revision = int(previous[1]) + 1
        record = {
            "reading_id": reading_id, "bvid": fields["bvid"], "page": int(fields["page"]),
            "cid": int(fields["cid"]), "revision": revision, "scope": scope_hash(fields["scope"]),
            "title": fields["title"][:300], "author": fields["author"][:200],
            "part_title": fields.get("part_title", "")[:300],
            "duration": fields.get("duration"), "fetched_at": fields["fetched_at"],
            "source_type": fields.get("source_type", "bvid"),
            "subtitle_state": fields.get("subtitle_state", "none"),
            "subtitle_reason": fields.get("subtitle_reason", "")[:80],
            "evidence": fields.get("evidence", "metadata_only"),
            "updated_at": fields["fetched_at"],
        }
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO readings VALUES(:reading_id,:bvid,:page,:cid,:revision,:scope,"
                ":title,:author,:part_title,:duration,:fetched_at,:source_type,:subtitle_state,"
                ":subtitle_reason,:evidence,:updated_at)", record)
            self.db.execute("DELETE FROM segments WHERE reading_id=? AND revision>=?",
                            (reading_id, revision))
        return record

    def add_segments(self, reading_id: str, revision: int, segments, lang: str = "") -> int:
        rows = []
        for idx, row in enumerate(segments or []):
            text = str(row.get("text", ""))[:2000]
            if not text.strip():
                continue
            rows.append((reading_id, int(revision), idx, int(row.get("from_ms", 0)),
                         int(row.get("to_ms", 0)), text, str(lang or "")[:16],
                         hashlib.sha256(text.encode()).hexdigest()[:16]))
        if rows:
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO segments VALUES(?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def reading(self, reading_id: str, scope: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM readings WHERE reading_id=? AND scope=?",
            (reading_id, scope_hash(scope))).fetchone()
        return dict(row) if row else None

    def segments(self, reading_id: str, revision: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM segments WHERE reading_id=? AND revision=? ORDER BY idx",
            (reading_id, int(revision)))]

    def set_evidence(self, reading_id: str, revision: int, evidence: str) -> None:
        with self.db:
            self.db.execute("UPDATE readings SET evidence=?, updated_at=? WHERE reading_id=? AND revision=?",
                            (evidence, _now(), reading_id, int(revision)))

    # ---- audience replies (read-only, never a profile) -----------------
    def add_replies(self, reading_id: str, revision: int, items, source: str = "") -> int:
        """Replace this revision's comment page with a bounded, sanitized list.

        Only floor/likes/text/is_up plus the two placement flags are kept: no uid, no
        author name, no per-person history, so reading comments can never build a
        stranger's profile. `pinned` and `nested` exist so a later journal can tell
        the uploader's own words (pinned comment, reply in a thread) from bystanders.
        """
        rows = []
        for idx, row in enumerate(items or []):
            text = str(row.get("text", "")).strip()[:300]
            if not text:
                continue
            rows.append((reading_id, int(revision), idx,
                         "1" if row.get("is_up") else "0",
                         max(0, int(row.get("likes", 0) or 0)), text,
                         hashlib.sha256(text.encode()).hexdigest()[:16],
                         str(source or "")[:40], _now(),
                         "1" if row.get("pinned") else "0",
                         "1" if row.get("nested") else "0"))
        with self.db:
            self.db.execute("DELETE FROM replies WHERE reading_id=? AND revision=?",
                            (reading_id, int(revision)))
            if rows:
                self.db.executemany(
                    "INSERT INTO replies(reading_id,revision,idx,is_up,likes,text,digest,"
                    "source,created_at,pinned,nested) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def replies(self, reading_id: str, revision: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM replies WHERE reading_id=? AND revision=? ORDER BY idx",
            (reading_id, int(revision)))]

    # ---- audience danmaku (read-only, timestamped, never a profile) -----
    def add_danmaku(self, reading_id: str, revision: int, items, source: str = "") -> int:
        """Replace this revision's danmaku sample with a bounded, timestamped list.

        Only time and text are kept — no uid, no nickname, no colour — so a sample can
        never become a profile of the people in the room. `repeated` records how many
        times the same line was spammed in its time bucket, which is the heat signal
        without storing hundreds of copies.
        """
        rows = []
        for idx, row in enumerate(items or []):
            text = str(row.get("text", "")).strip()[:60]
            if not text:
                continue
            rows.append((reading_id, int(revision), idx,
                         max(0, int(row.get("at_ms", 0) or 0)), text,
                         max(1, int(row.get("count", 1) or 1)),
                         hashlib.sha256(text.encode()).hexdigest()[:16],
                         str(source or "")[:40], _now()))
        with self.db:
            self.db.execute("DELETE FROM danmaku WHERE reading_id=? AND revision=?",
                            (reading_id, int(revision)))
            if rows:
                self.db.executemany(
                    "INSERT INTO danmaku(reading_id,revision,idx,at_ms,text,repeated,"
                    "digest,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def danmaku(self, reading_id: str, revision: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM danmaku WHERE reading_id=? AND revision=? ORDER BY idx",
            (reading_id, int(revision)))]

    # ---- ASR ------------------------------------------------------------
    def reserve_asr(self, scope: str, day: str, limit: int = 5) -> bool:
        """Atomically take one slot of today's ASR budget for this scope.

        The UPDATE...WHERE used<limit is the whole contention protocol: two racers
        cannot both see the last slot, so the cap holds even across processes.
        """
        hashed, cap = scope_hash(scope), max(0, int(limit))
        stamp = _now()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO asr_daily(day,scope,used,updated_at) "
                            "VALUES(?,?,0,?)", (day, hashed, stamp))
            cur = self.db.execute("UPDATE asr_daily SET used=used+1, updated_at=? "
                                  "WHERE day=? AND scope=? AND used<?",
                                  (stamp, day, hashed, cap))
            return cur.rowcount == 1

    def asr_used(self, scope: str, day: str) -> int:
        row = self.db.execute("SELECT used FROM asr_daily WHERE day=? AND scope=?",
                              (day, scope_hash(scope))).fetchone()
        return int(row["used"]) if row else 0

    def begin_asr_job(self, job_id: str, **fields) -> dict:
        """Insert once; an existing row is returned untouched so re-entry is idempotent."""
        row = self.asr_job(job_id)
        if row:
            return row
        stamp = _now()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO asr_jobs(job_id,reading_id,revision,bvid,cid,scope,"
                "state,reason,engine,model,language,declared_ms,decoded_ms,processed_ms,"
                "coverage,segments,chars,quality,note,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0,'',0,0,'','',?,?)",
                (job_id, fields.get("reading_id", ""), int(fields.get("revision", 0) or 0),
                 fields.get("bvid", ""), int(fields.get("cid", 0) or 0),
                 fields.get("scope", ""), fields.get("state", "queued"),
                 fields.get("reason", ""), fields.get("engine", ""),
                 fields.get("model", ""), fields.get("language", ""),
                 int(fields.get("declared_ms", 0) or 0), stamp, stamp))
        return self.asr_job(job_id) or {}

    def finish_asr_job(self, job_id: str, *, state: str, reason: str = "",
                       decoded_ms: int = 0, processed_ms: int = 0, coverage=None,
                       segments: int = 0, chars: int = 0, quality: str = "",
                       note: str = "", declared_ms: int | None = None) -> None:
        stamp = _now()
        values = (state, reason[:160], int(decoded_ms), int(processed_ms),
                  _json_text(coverage), int(segments), int(chars),
                  quality[:40], note[:400])
        with self.db:
            if declared_ms is None:
                self.db.execute(
                    "UPDATE asr_jobs SET state=?,reason=?,decoded_ms=?,processed_ms=?,coverage=?,"
                    "segments=?,chars=?,quality=?,note=?,updated_at=? WHERE job_id=?",
                    (*values, stamp, job_id))
            else:
                self.db.execute(
                    "UPDATE asr_jobs SET state=?,reason=?,decoded_ms=?,processed_ms=?,coverage=?,"
                    "segments=?,chars=?,quality=?,note=?,declared_ms=?,updated_at=? WHERE job_id=?",
                    (*values, max(0, int(declared_ms)), stamp, job_id))

    def asr_job(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM asr_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def asr_job_for(self, reading_id: str, revision: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM asr_jobs WHERE reading_id=? AND revision=? "
            "ORDER BY created_at DESC LIMIT 1", (reading_id, int(revision))).fetchone()
        return dict(row) if row else None

    def add_asr_segments(self, job_id: str, segments) -> int:
        rows = []
        for idx, item in enumerate(segments or []):
            text = str(item.get("text") or "")[:300]
            if not text.strip():
                continue
            rows.append((job_id, idx, max(0, int(item.get("from_ms", 0) or 0)),
                         max(0, int(item.get("to_ms", 0) or 0)), text,
                         hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                         item.get("avg_logprob"), item.get("no_speech")))
        with self.db:
            self.db.execute("DELETE FROM asr_segments WHERE job_id=?", (job_id,))
            self.db.executemany(
                "INSERT INTO asr_segments(job_id,idx,from_ms,to_ms,text,digest,"
                "avg_logprob,no_speech) VALUES(?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def asr_segments(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM asr_segments WHERE job_id=? ORDER BY idx", (job_id,))]

    def recent_asr(self, scope: str, limit: int = 6) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM asr_jobs WHERE scope=? ORDER BY created_at DESC LIMIT ?",
            (scope_hash(scope), max(1, int(limit))))]

    # ---- frames --------------------------------------------------------
    def begin_job(self, job_id: str, reading_id: str, revision: int, generation: int,
                  source: str, budget: dict) -> None:
        stamp = _now()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO frame_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (job_id, reading_id, int(revision), int(generation), "running", "",
                             source, stamp, stamp, json.dumps(budget, ensure_ascii=False), "{}"))

    def finish_job(self, job_id: str, state: str, reason: str = "", result=None) -> None:
        with self.db:
            self.db.execute("UPDATE frame_jobs SET state=?, reason=?, result_json=?, updated_at=? WHERE job_id=?",
                            (state, reason[:120], json.dumps(result or {}, ensure_ascii=False),
                             _now(), job_id))

    def job(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM frame_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def add_frame(self, **fields) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO frames VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (fields["frame_id"], fields["job_id"], fields["reading_id"],
                             int(fields["revision"]), int(fields["idx"]), int(fields["target_ms"]),
                             int(fields["actual_ms"]), int(fields["width"]), int(fields["height"]),
                             int(fields["bytes"]), fields["digest"], fields["path"],
                             fields["source"], fields["created_at"]))

    def frames(self, job_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM frames WHERE job_id=? ORDER BY idx", (job_id,))]

    def frames_for(self, reading_id: str, revision: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM frames WHERE reading_id=? AND revision=? ORDER BY idx",
            (reading_id, int(revision)))]

    def purge_frames(self, older_than_hours: float) -> int:
        cutoff = (datetime.now() - timedelta(hours=older_than_hours)).isoformat()
        rows = self.db.execute("SELECT frame_id, path FROM frames WHERE created_at<?",
                               (cutoff,)).fetchall()
        for row in rows:
            try:
                Path(row[1]).unlink(missing_ok=True)
            except OSError:
                pass
        if rows:
            with self.db:
                self.db.executemany("DELETE FROM frames WHERE frame_id=?", [(r[0],) for r in rows])
        return len(rows)

    # ---- batches / observations ---------------------------------------
    def add_batch(self, batch_id: str, job_id: str, reading_id: str, revision: int,
                  provider_id: str, frame_ids, state: str, reason: str = "", usage=None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO visual_batches VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (batch_id, job_id, reading_id, int(revision), provider_id,
                             json.dumps(list(frame_ids)), state, reason[:120],
                             json.dumps(usage or {}, ensure_ascii=False), _now()))

    def add_observation(self, obs_id: str, batch_id: str, reading_id: str, revision: int,
                        frame_id: str, actual_ms: int, visible: str, interpretation: str,
                        uncertainty: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (obs_id, batch_id, reading_id, int(revision), frame_id, int(actual_ms),
                             visible[:1200], interpretation[:1200], uncertainty[:600], _now()))

    def observations(self, reading_id: str, revision: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM observations WHERE reading_id=? AND revision=? ORDER BY actual_ms",
            (reading_id, int(revision)))]

    # ---- journals ------------------------------------------------------
    def add_journal(self, journal_id: str, reading_id: str, revision: int, fields: dict,
                    refs: dict, state: str = STATE_PENDING) -> str:
        version = 1 + (self.db.execute(
            "SELECT count(*) FROM journals WHERE reading_id=?", (reading_id,)).fetchone()[0])
        stamp = _now()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO journals VALUES(?,?,?,?,?,?,?,?,?)",
                            (journal_id, reading_id, int(revision), version, state,
                             json.dumps(fields, ensure_ascii=False),
                             json.dumps(refs, ensure_ascii=False), stamp, stamp))
        return journal_id

    def journal(self, journal_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM journals WHERE journal_id=?", (journal_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        record["fields"] = json.loads(record.pop("fields_json") or "{}")
        record["refs"] = json.loads(record.pop("refs_json") or "{}")
        return record

    def journals(self, *, scope: str, state: str | None = None, limit: int = 5) -> list[dict]:
        sql = ("SELECT j.* FROM journals j JOIN readings r ON r.reading_id=j.reading_id "
               "WHERE r.scope=?")
        params: list = [scope_hash(scope)]
        if state:
            sql += " AND j.state=?"
            params.append(state)
        sql += " ORDER BY j.created_at DESC LIMIT ?"
        params.append(int(limit))
        out = []
        for row in self.db.execute(sql, params):
            record = dict(row)
            record["fields"] = json.loads(record.pop("fields_json") or "{}")
            record["refs"] = json.loads(record.pop("refs_json") or "{}")
            out.append(record)
        return out

    def set_journal_state(self, journal_id: str, state: str) -> None:
        with self.db:
            self.db.execute("UPDATE journals SET state=?, updated_at=? WHERE journal_id=?",
                            (state, _now(), journal_id))

    def daily_reflections(self, day: str) -> int:
        return self.db.execute("SELECT count(*) FROM reflections WHERE substr(created_at,1,10)=?",
                               (day,)).fetchone()[0]

    # ---- reflections ---------------------------------------------------
    def add_reflection(self, *, kind: str, topic: str, old_view: str, new_view: str,
                       journal_id: str, refs: dict, supersedes: int | None = None) -> int:
        stamp = _now()
        with self.db:
            if supersedes:
                self.db.execute("UPDATE reflections SET state=?, updated_at=? WHERE id=?",
                                (STATE_SUPERSEDED, stamp, int(supersedes)))
            return self.db.execute(
                "INSERT INTO reflections(kind,topic,old_view,new_view,journal_id,refs_json,state,"
                "supersedes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (kind[:40], topic[:200], old_view[:600], new_view[:800], journal_id,
                 json.dumps(refs, ensure_ascii=False), STATE_ACTIVE, supersedes, stamp, stamp)
            ).lastrowid

    def reflection(self, rid: int) -> dict | None:
        row = self.db.execute("SELECT * FROM reflections WHERE id=?", (int(rid),)).fetchone()
        return dict(row) if row else None

    def reflections(self, *, scope: str, state: str = STATE_ACTIVE, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT f.* FROM reflections f JOIN journals j ON j.journal_id=f.journal_id "
            "JOIN readings r ON r.reading_id=j.reading_id "
            "WHERE r.scope=? AND f.state=? ORDER BY f.id DESC LIMIT ?",
            (scope_hash(scope), state, int(limit)))
        return [dict(r) for r in rows]

    def withdraw_reflection(self, rid: int) -> None:
        with self.db:
            self.db.execute("UPDATE reflections SET state=?, updated_at=? WHERE id=?",
                            (STATE_WITHDRAWN, _now(), int(rid)))

    # ---- transitive invalidation ---------------------------------------
    def invalidate_reading(self, reading_id: str, revision: int | None = None) -> dict:
        """A deleted/corrected source invalidates every journal and reflection derived from it."""
        journals, reflections = [], []
        for row in self.db.execute("SELECT journal_id, refs_json, state FROM journals WHERE reading_id=?",
                                   (reading_id,)):
            refs = json.loads(row["refs_json"] or "{}")
            if revision is not None and int(refs.get("revision", revision)) != int(revision):
                continue
            if row["state"] != STATE_WITHDRAWN:
                self.set_journal_state(row["journal_id"], STATE_INVALIDATED)
                journals.append(row["journal_id"])
        for row in self.db.execute("SELECT id, refs_json, state FROM reflections"):
            refs = json.loads(row["refs_json"] or "{}")
            if refs.get("reading_id") != reading_id:
                continue
            if revision is not None and int(refs.get("revision", revision)) != int(revision):
                continue
            if row["state"] == STATE_ACTIVE:
                with self.db:
                    self.db.execute("UPDATE reflections SET state=?, updated_at=? WHERE id=?",
                                    (STATE_INVALIDATED, _now(), row["id"]))
                reflections.append(row["id"])
        return {"journals": journals, "reflections": reflections}

    # ---- growth: interests / memory links / proposals --------------------
    def bump_interest(self, scope: str, topic: str, weight: float = 1.0) -> dict:
        """Interest weight is a signal of what she keeps coming back to, not a fact about anyone."""
        topic = (topic or "").strip()
        if not topic:
            raise ValueError("兴趣主题不能为空")
        scope = scope_hash(scope)
        now = _now(self._clock() if self._clock else None)
        with self.db:
            row = self.db.execute("SELECT weight FROM interests WHERE scope=? AND topic=?",
                                  (scope, topic)).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO interests(scope,topic,weight,first_seen,last_seen) VALUES(?,?,?,?,?)",
                    (scope, topic, float(weight), now, now))
            else:
                self.db.execute(
                    "UPDATE interests SET weight=?, last_seen=? WHERE scope=? AND topic=?",
                    (float(row["weight"]) + float(weight), now, scope, topic))
        return self._interest_row(scope, topic)

    def _interest_row(self, scope: str, topic: str) -> dict:
        row = self.db.execute("SELECT * FROM interests WHERE scope=? AND topic=?",
                              (scope, topic)).fetchone()
        return dict(row) if row else {}

    def interests(self, scope: str, limit: int = 5) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM interests WHERE scope=? ORDER BY weight DESC, last_seen DESC LIMIT ?",
            (scope_hash(scope), max(0, int(limit)))).fetchall()
        return [dict(r) for r in rows]

    def readings(self, scope: str, *, limit: int = 10) -> list[dict]:
        """Recent parts she read in this session, newest first (read-only).

        Added for the read-only bridge (`read_port.py`): the companion plugin needs
        "what did she actually read here", and poking at the table row by row from
        outside would be worse. This writes nothing and creates no revision.
        """
        rows = self.db.execute(
            "SELECT * FROM readings WHERE scope=? ORDER BY fetched_at DESC LIMIT ?",
            (scope_hash(scope), max(0, int(limit)))).fetchall()
        return [dict(r) for r in rows]

    def latest_reading(self, scope: str) -> dict:
        """The most recent part she read in this session, or {} when there is none."""
        row = self.db.execute(
            "SELECT * FROM readings WHERE scope=? ORDER BY fetched_at DESC LIMIT 1",
            (scope_hash(scope),)).fetchone()
        return dict(row) if row else {}

    def read_bvids(self, scope: str, limit: int = 60) -> list[str]:
        """Already-read videos in this session, so proposals do not re-offer the same thing."""
        rows = self.db.execute(
            "SELECT bvid, MAX(fetched_at) AS seen FROM readings WHERE scope=? "
            "GROUP BY bvid ORDER BY seen DESC LIMIT ?",
            (scope_hash(scope), max(0, int(limit)))).fetchall()
        return [r["bvid"] for r in rows]

    def link_memory(self, kind: str, bili_id: str, memory_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO memory_links(kind,bili_id,memory_id,created_at) VALUES(?,?,?,?)",
                (kind, bili_id, memory_id, _now(self._clock() if self._clock else None)))

    def memory_link(self, kind: str, bili_id: str) -> str:
        row = self.db.execute("SELECT memory_id FROM memory_links WHERE kind=? AND bili_id=?",
                              (kind, bili_id)).fetchone()
        return row["memory_id"] if row else ""

    def drop_memory_link(self, kind: str, bili_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM memory_links WHERE kind=? AND bili_id=?", (kind, bili_id))

    def add_proposal(self, proposal_id: str, scope: str, bvid: str, *, page: int = 1,
                     title: str = "", author: str = "", duration=None, reason: str = "",
                     source: str = "", state: str = "pending") -> dict:
        now = _now(self._clock() if self._clock else None)
        with self.db:
            self.db.execute(
                "INSERT INTO proposals(proposal_id,scope,bvid,page,title,author,duration,reason,"
                "source,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, scope_hash(scope), bvid, int(page), title, author, duration, reason,
                 source, state, now, now))
        return self.proposal(proposal_id, scope) or {}

    def proposal(self, proposal_id: str, scope: str) -> dict | None:
        row = self.db.execute("SELECT * FROM proposals WHERE proposal_id=? AND scope=?",
                              (proposal_id, scope_hash(scope))).fetchone()
        return dict(row) if row else None

    def proposals(self, scope: str, *, state: str | None = None, limit: int = 10) -> list[dict]:
        sql = "SELECT * FROM proposals WHERE scope=?"
        params: list = [scope_hash(scope)]
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def set_proposal_state(self, proposal_id: str, state: str) -> None:
        with self.db:
            self.db.execute("UPDATE proposals SET state=?, updated_at=? WHERE proposal_id=?",
                            (state, _now(self._clock() if self._clock else None), proposal_id))

    def proposal_at(self, scope: str, index: int) -> dict | None:
        """1-based index over pending proposals, oldest first — what the owner actually sees."""
        rows = self.db.execute(
            "SELECT * FROM proposals WHERE scope=? AND state='pending' ORDER BY created_at ASC",
            (scope_hash(scope),)).fetchall()
        if index < 1 or index > len(rows):
            return None
        return dict(rows[index - 1])

    def expire_proposals(self, scope: str, older_than_hours: float = 72.0) -> int:
        cutoff = datetime.now() - timedelta(hours=float(older_than_hours))
        changed = 0
        with self.db:
            for row in self.db.execute(
                    "SELECT proposal_id, created_at FROM proposals WHERE scope=? AND state='pending'",
                    (scope_hash(scope),)):
                try:
                    created = datetime.fromisoformat(row["created_at"])
                except (TypeError, ValueError):
                    continue
                if created < cutoff:
                    self.db.execute(
                        "UPDATE proposals SET state='expired', updated_at=? WHERE proposal_id=?",
                        (_now(), row["proposal_id"]))
                    changed += 1
        return changed

    # ---- background vision decisions (shadow mode) ---------------------
    def add_shadow(self, decision_id: str, scope: str, bvid: str, *, title: str = "",
                   author: str = "", topic: str = "", subtitle_state: str = "",
                   interested: bool = False, verdict: str = SHADOW_WOULD_LOOK,
                   reason: str = "", frames: int = 0, note: str = "") -> dict:
        """One screening decision. Verdict says what happened, never that frames existed."""
        now = _now(self._clock() if self._clock else None)
        with self.db:
            self.db.execute(
                "INSERT INTO vision_shadow(decision_id,scope,bvid,title,author,topic,"
                "subtitle_state,interested,verdict,reason,frames,note,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, scope_hash(scope), bvid, title, author, topic, subtitle_state,
                 "1" if interested else "0", verdict, reason, int(frames), note, now, now))
        return self.shadow(decision_id, scope) or {}

    def shadow(self, decision_id: str, scope: str) -> dict | None:
        row = self.db.execute("SELECT * FROM vision_shadow WHERE decision_id=? AND scope=?",
                              (decision_id, scope_hash(scope))).fetchone()
        return dict(row) if row else None

    def shadows(self, scope: str, *, limit: int = 10, verdict: str | None = None) -> list[dict]:
        sql = "SELECT * FROM vision_shadow WHERE scope=?"
        params: list = [scope_hash(scope)]
        if verdict:
            sql += " AND verdict=?"
            params.append(verdict)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def update_shadow(self, decision_id: str, *, verdict: str | None = None,
                      reason: str | None = None, frames: int | None = None,
                      note: str | None = None) -> None:
        sets: list[str] = []
        params: list = []
        for column, value in (("verdict", verdict), ("reason", reason),
                              ("frames", frames), ("note", note)):
            if value is not None:
                sets.append(f"{column}=?")
                params.append(int(value) if column == "frames" else value)
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(_now(self._clock() if self._clock else None))
        params.append(decision_id)
        with self.db:
            self.db.execute(f"UPDATE vision_shadow SET {', '.join(sets)} WHERE decision_id=?",
                            params)

    def shadow_count(self, scope: str, verdict: str | None = None) -> int:
        sql = "SELECT COUNT(1) FROM vision_shadow WHERE scope=?"
        params: list = [scope_hash(scope)]
        if verdict:
            sql += " AND verdict=?"
            params.append(verdict)
        return int(self.db.execute(sql, params).fetchone()[0])

    def first_candidate(self, scope: str) -> dict | None:
        """Oldest pending 'would_look' — at most one upgrade per background round."""
        row = self.db.execute(
            "SELECT * FROM vision_shadow WHERE scope=? AND verdict=? ORDER BY created_at ASC LIMIT 1",
            (scope_hash(scope), SHADOW_WOULD_LOOK)).fetchone()
        return dict(row) if row else None

    def recent_jobs(self, scope: str, limit: int = 8) -> list[dict]:
        """Workbench view: which part was looked at, what came out, which model saw it."""
        rows = self.db.execute(
            "SELECT j.*, r.title, r.part_title, r.bvid, r.evidence,"
            " (SELECT provider_id FROM visual_batches b WHERE b.job_id=j.job_id LIMIT 1) AS provider_id,"
            " (SELECT state FROM visual_batches b WHERE b.job_id=j.job_id LIMIT 1) AS batch_state"
            " FROM frame_jobs j JOIN readings r ON r.reading_id=j.reading_id"
            " WHERE r.scope=? ORDER BY j.created_at DESC LIMIT ?",
            (scope_hash(scope), max(0, int(limit)))).fetchall()
        return [dict(r) for r in rows]


def scope_hash(scope: str) -> str:
    """Owner scoping without storing a plaintext session id in every row."""
    return hashlib.sha256(str(scope or "").encode()).hexdigest()[:32]
