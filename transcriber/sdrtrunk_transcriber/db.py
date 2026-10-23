"""SQLite call log with full-text search over transcripts."""

import logging
import sqlite3
import threading
import time
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
    transcript TEXT,                     -- with spoken codes rewritten ("ten four" -> "10-4")
    transcript_raw TEXT,                 -- exactly what Whisper returned
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

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    page_time REAL NOT NULL,             -- unix seconds, from SDRTrunk
    received_at INTEGER NOT NULL,
    tone_a REAL NOT NULL,                -- Hz
    tone_b REAL,                         -- Hz, NULL for a single long tone
    tone_a_seconds REAL,
    label TEXT,                          -- station or group from SDRTrunk's paging_tones.csv, NULL if not listed
    system_label TEXT,
    site TEXT,
    channel TEXT,
    frequency INTEGER
);
CREATE INDEX IF NOT EXISTS pages_time ON pages(page_time);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    first_time INTEGER NOT NULL,         -- unix seconds of the first and latest call placed here
    last_time INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    label TEXT,                          -- "1234 Northampton St, Easton"
    category TEXT,                       -- fire, ems, police or NULL
    nature TEXT,                         -- "Structure fire", from words in the calls
    hidden INTEGER NOT NULL DEFAULT 0    -- removed from the map by the user
);
CREATE INDEX IF NOT EXISTS incidents_time ON incidents(last_time);

CREATE TABLE IF NOT EXISTS call_places (
    id INTEGER PRIMARY KEY,
    call_id INTEGER NOT NULL,
    incident_id INTEGER,
    kind TEXT NOT NULL,                  -- address, intersection, landmark
    heard TEXT,                          -- the transcript words it came from
    label TEXT,
    lat REAL,
    lon REAL,
    precision TEXT                       -- address, intersection, landmark, street (approximate)
);
CREATE INDEX IF NOT EXISTS call_places_call ON call_places(call_id);
CREATE INDEX IF NOT EXISTS call_places_incident ON call_places(incident_id);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query TEXT PRIMARY KEY,
    lat REAL,                            -- NULL when nothing was found
    lon REAL,
    kind TEXT,
    display TEXT,
    fetched_at INTEGER NOT NULL
);
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


PAGE_FIELDS = (
    "page_time", "received_at", "tone_a", "tone_b", "tone_a_seconds", "label", "system_label", "site",
    "channel", "frequency",
)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        try:
            had_fts = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'calls_fts'").fetchone() is not None
            self._conn.executescript(FTS_SCHEMA)
            if not had_fts:
                # Index calls that were logged before the search index existed.
                self._conn.execute("INSERT INTO calls_fts(calls_fts) VALUES ('rebuild')")
            self.has_fts = True
        except sqlite3.OperationalError:
            log.warning("SQLite has no FTS5 support; transcript search falls back to plain matching")
            self.has_fts = False
        self._conn.commit()

    def _migrate(self):
        """Adds columns introduced after a call log was first created."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(calls)")}
        if "transcript_raw" not in columns:
            self._conn.execute("ALTER TABLE calls ADD COLUMN transcript_raw TEXT")
        if "map_status" not in columns:
            # NULL: not looked at yet; none: no place heard; placed; unplaced: heard but not found.
            self._conn.execute("ALTER TABLE calls ADD COLUMN map_status TEXT")
        # Crashes were called "Vehicle crash" before they became MVA.
        self._conn.execute("UPDATE incidents SET nature = 'MVA' WHERE nature = 'Vehicle crash'")

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

    def insert_page(self, page: dict) -> int:
        columns = ", ".join(PAGE_FIELDS)
        marks = ", ".join("?" for _ in PAGE_FIELDS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO pages ({columns}) VALUES ({marks})",
                [page.get(name) for name in PAGE_FIELDS],
            )
            self._conn.commit()
            return cur.lastrowid

    def pages(self, since: float | None = None, until: float | None = None, q: str = "",
              limit: int = 200) -> list[dict]:
        """Tone-out pages, newest first, optionally within [since, until) and matching q."""
        where, params = [], []
        if since is not None:
            where.append("page_time >= ?")
            params.append(since)
        if until is not None:
            where.append("page_time < ?")
            params.append(until)
        if q:
            where.append("(label LIKE ? OR channel LIKE ?)")
            params.extend([f"%{q}%"] * 2)
        sql = "SELECT * FROM pages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY page_time DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # ---- incident map

    def calls_to_map(self) -> list[int]:
        """Transcribed calls the map hasn't looked at yet, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM calls WHERE status = 'done' AND map_status IS NULL ORDER BY id").fetchall()
        return [row["id"] for row in rows]

    def set_map_status(self, call_id: int, status: str | None):
        with self._lock:
            # A direct UPDATE of one column; the search-index trigger only re-indexes the same text.
            self._conn.execute("UPDATE calls SET map_status = ? WHERE id = ?", (status, call_id))
            self._conn.commit()

    def add_call_place(self, place: dict) -> int:
        names = ("call_id", "incident_id", "kind", "heard", "label", "lat", "lon", "precision")
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO call_places ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                [place.get(n) for n in names])
            self._conn.commit()
            return cur.lastrowid

    def nearby_incidents(self, since: int, until: int) -> list[dict]:
        """Incidents (hidden ones too) active in [since, until]."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE last_time >= ? AND first_time <= ? ORDER BY last_time DESC",
                (since, until)).fetchall()
        return [dict(row) for row in rows]

    def create_incident(self, incident: dict) -> int:
        names = ("first_time", "last_time", "lat", "lon", "label", "category", "nature")
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO incidents ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                [incident.get(n) for n in names])
            self._conn.commit()
            return cur.lastrowid

    def update_incident(self, incident_id: int, **values):
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._lock:
            cur = self._conn.execute(f"UPDATE incidents SET {assignments} WHERE id = ?",
                                     [*values.values(), incident_id])
            self._conn.commit()
            return cur.rowcount

    def incidents(self, since: int | None = None, until: int | None = None, hidden: bool = False,
                  limit: int = 300) -> list[dict]:
        """Incidents newest first, each with its placed calls."""
        where, params = [], []
        if since is not None:
            where.append("last_time >= ?")
            params.append(since)
        if until is not None:
            where.append("first_time < ?")
            params.append(until)
        if not hidden:
            where.append("hidden = 0")
        sql = "SELECT * FROM incidents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_time DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self._lock:
            incidents = [dict(row) for row in self._conn.execute(sql, params).fetchall()]
            for incident in incidents:
                rows = self._conn.execute(
                    "SELECT p.kind, p.heard, p.label, p.lat, p.lon, p.precision, c.id, c.call_time, c.talkgroup, "
                    "c.talkgroup_label, c.talkgroup_group, c.talker_alias, c.source, c.transcript, c.duration_s "
                    "FROM call_places p JOIN calls c ON c.id = p.call_id WHERE p.incident_id = ? "
                    "ORDER BY c.call_time", (incident["id"],)).fetchall()
                incident["calls"] = [dict(row) for row in rows]
        return incidents

    def incident_precisions(self, incident_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT precision FROM call_places WHERE incident_id = ?",
                                      (incident_id,)).fetchall()
        return [row["precision"] for row in rows]

    def clear_map(self):
        """Forgets every incident so all calls are placed again (after editing the street list)."""
        with self._lock:
            self._conn.execute("DELETE FROM call_places")
            self._conn.execute("DELETE FROM incidents")
            self._conn.execute("UPDATE calls SET map_status = NULL WHERE map_status IS NOT NULL")
            self._conn.commit()

    def geocode_cached(self, query: str) -> dict | None:
        """The saved answer: a dict, {} for "not found", None when never asked."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM geocode_cache WHERE query = ?", (query,)).fetchone()
        if row is None:
            return None
        if row["lat"] is None:
            return {}
        return {"lat": row["lat"], "lon": row["lon"], "kind": row["kind"], "display": row["display"]}

    def geocode_store(self, query: str, result: dict):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (query, lat, lon, kind, display, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (query, result.get("lat"), result.get("lon"), result.get("kind"), result.get("display"),
                 int(time.time())))
            self._conn.commit()

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM calls GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}


def _fts_query(q: str) -> str:
    """Treats each word as a literal prefix search so punctuation can't break FTS syntax."""
    words = [w.replace('"', '""') for w in q.split()]
    return " ".join(f'"{w}"*' for w in words)
