"""SQLite call log with full-text search over transcripts."""

import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    call_time INTEGER NOT NULL,          -- unix seconds, from SDRTrunk
    received_at INTEGER NOT NULL,        -- unix seconds, when the upload arrived
    system_id TEXT,
    system_label TEXT,
    talkgroup TEXT,
    talkgroup_label TEXT,
    talkgroup_group TEXT,
    source TEXT,                         -- radio ID
    talker_alias TEXT,
    frequency INTEGER,
    patches TEXT,
    audio_path TEXT NOT NULL,            -- relative to the data directory
    duration_s REAL,
    transcript TEXT,
    confidence REAL,                     -- mean Whisper avg_logprob, closer to 0 is better
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, processing, done, skipped, failed
    error TEXT,
    backend TEXT,
    model TEXT,
    transcribe_ms INTEGER
);
CREATE INDEX IF NOT EXISTS calls_time ON calls(call_time);
CREATE INDEX IF NOT EXISTS calls_talkgroup ON calls(talkgroup);
CREATE INDEX IF NOT EXISTS calls_source ON calls(source);
CREATE INDEX IF NOT EXISTS calls_status ON calls(status);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS calls_fts USING fts5(
    transcript, talkgroup_label, talker_alias, content='calls', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS calls_ai AFTER INSERT ON calls BEGIN
    INSERT INTO calls_fts(rowid, transcript, talkgroup_label, talker_alias)
    VALUES (new.id, new.transcript, new.talkgroup_label, new.talker_alias);
END;
CREATE TRIGGER IF NOT EXISTS calls_ad AFTER DELETE ON calls BEGIN
    INSERT INTO calls_fts(calls_fts, rowid, transcript, talkgroup_label, talker_alias)
    VALUES ('delete', old.id, old.transcript, old.talkgroup_label, old.talker_alias);
END;
CREATE TRIGGER IF NOT EXISTS calls_au AFTER UPDATE ON calls BEGIN
    INSERT INTO calls_fts(calls_fts, rowid, transcript, talkgroup_label, talker_alias)
    VALUES ('delete', old.id, old.transcript, old.talkgroup_label, old.talker_alias);
    INSERT INTO calls_fts(rowid, transcript, talkgroup_label, talker_alias)
    VALUES (new.id, new.transcript, new.talkgroup_label, new.talker_alias);
END;
"""

CALL_FIELDS = (
    "call_time", "received_at", "system_id", "system_label", "talkgroup", "talkgroup_label",
    "talkgroup_group", "source", "talker_alias", "frequency", "patches", "audio_path",
)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        try:
            self._conn.executescript(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            log.warning("SQLite has no FTS5 support; transcript search falls back to plain matching")
            self.has_fts = False
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def insert_call(self, call: dict) -> int:
        columns = ", ".join(CALL_FIELDS)
        marks = ", ".join("?" for _ in CALL_FIELDS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO calls ({columns}) VALUES ({marks})",
                [call.get(name) for name in CALL_FIELDS],
            )
            self._conn.commit()
            return cur.lastrowid

    def update_call(self, call_id: int, **values):
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._lock:
            self._conn.execute(
                f"UPDATE calls SET {assignments} WHERE id = ?", [*values.values(), call_id]
            )
            self._conn.commit()

    def get_call(self, call_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return dict(row) if row else None

    def pending_ids(self) -> list[int]:
        """Calls left unfinished by a previous run, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM calls WHERE status IN ('queued', 'processing') ORDER BY id"
            ).fetchall()
        return [row["id"] for row in rows]

    def search(self, q: str = "", talkgroup: str = "", source: str = "",
               before_id: int | None = None, limit: int = 100) -> list[dict]:
        where, params = [], []

        if q:
            if self.has_fts:
                where.append("id IN (SELECT rowid FROM calls_fts WHERE calls_fts MATCH ?)")
                params.append(_fts_query(q))
            else:
                where.append("(transcript LIKE ? OR talkgroup_label LIKE ? OR talker_alias LIKE ?)")
                params.extend([f"%{q}%"] * 3)
        if talkgroup:
            where.append("(talkgroup = ? OR talkgroup_label LIKE ?)")
            params.extend([talkgroup, f"%{talkgroup}%"])
        if source:
            where.append("(source = ? OR talker_alias LIKE ?)")
            params.extend([source, f"%{source}%"])
        if before_id:
            where.append("id < ?")
            params.append(before_id)

        sql = "SELECT * FROM calls"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))

        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM calls GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}


def _fts_query(q: str) -> str:
    """Treats each word as a literal prefix search so punctuation can't break FTS syntax."""
    words = [w.replace('"', '""') for w in q.split()]
    return " ".join(f'"{w}"*' for w in words)
