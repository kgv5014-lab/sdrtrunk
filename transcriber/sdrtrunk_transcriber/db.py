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
    hidden INTEGER NOT NULL DEFAULT 0,   -- removed from the map by the user
    channel TEXT,                        -- the channel it was moved to, "Tac 2"
    channel_time INTEGER                 -- when that was heard
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

-- Calls the user moved by hand on the map. Kept when the map is rebuilt, so their fixes stay.
CREATE TABLE IF NOT EXISTS map_edits (
    call_id INTEGER PRIMARY KEY,
    anchor_call_id INTEGER,              -- goes with this call (the group's first); itself: starts its own; NULL: off the map
    lat REAL,                            -- where the group was, for calls that name no place
    lon REAL,
    label TEXT,
    edited_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,                 -- random, as the ntfy phone app expects
    time INTEGER NOT NULL,               -- unix seconds, when it was sent
    kind TEXT NOT NULL,                  -- incident, page or test
    title TEXT,
    message TEXT,
    tags TEXT,                           -- comma separated ntfy tags (emoji names)
    priority INTEGER,
    click TEXT,                          -- link opened by tapping the alert
    incident_id INTEGER,
    call_id INTEGER,
    page_id INTEGER,
    page_label TEXT                      -- the tone-out's station, or the watch zone's name
);
CREATE INDEX IF NOT EXISTS alerts_time ON alerts(time);

CREATE TABLE IF NOT EXISTS summaries (
    kind TEXT NOT NULL,                  -- incident or call
    ref_id INTEGER NOT NULL,             -- the incident or call id
    basis TEXT NOT NULL,                 -- fingerprint of the calls it was made from; changes mean redo
    summary TEXT,
    tags TEXT,                           -- comma separated, from summaries.TAGS
    status TEXT,                         -- incidents: active, under control, cleared, canceled, unknown
    model TEXT,
    made_at INTEGER NOT NULL,
    ms INTEGER,                          -- how long the model took
    last_call_id INTEGER,                -- incidents: newest call it read, so later calls count as new
    PRIMARY KEY (kind, ref_id)
);

-- Short notes the model writes when an incident changes ("Second alarm called", "Patient transported").
CREATE TABLE IF NOT EXISTS incident_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    time INTEGER NOT NULL,               -- the newest call it is about
    note TEXT NOT NULL,
    status TEXT,
    made_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS incident_updates_incident ON incident_updates(incident_id);

CREATE TABLE IF NOT EXISTS signal_hours (
    hour_start INTEGER NOT NULL,         -- unix seconds, start of the hour SDRTrunk counted
    tuner TEXT NOT NULL,                 -- dongle, like "R820T 00000011"
    status TEXT,                         -- Enabled, Error, Disabled, Removed
    channel TEXT NOT NULL,               -- '' for a dongle with no channels that hour
    decoder TEXT,
    frequency INTEGER,
    minutes INTEGER,                     -- how much of the hour the channel ran
    messages_good INTEGER,
    messages_bad INTEGER,
    good_percent REAL,                   -- NULL with too few messages to judge
    sync_lost_s REAL,                    -- control channels only
    signal_db REAL,                      -- median channel power with a signal present
    noise_db REAL,                       -- median channel power with only noise (analog channels)
    ppm REAL,                            -- dongle frequency correction at the end of the hour
    ppm_change REAL,
    channel_error_hz INTEGER,
    usb_errors INTEGER,
    PRIMARY KEY (hour_start, tuner, channel)
);

CREATE TABLE IF NOT EXISTS signal_problems (
    tuner TEXT NOT NULL,
    channel TEXT NOT NULL,
    problem TEXT NOT NULL,               -- decode, sync, signal, noise, drift, usb, error, missing
    since INTEGER NOT NULL,              -- hour_start of the report that found it
    PRIMARY KEY (tuner, channel, problem)
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query TEXT PRIMARY KEY,
    lat REAL,                            -- NULL when nothing was found
    lon REAL,
    kind TEXT,
    display TEXT,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS helicopters (
    hex TEXT PRIMARY KEY,                -- ICAO hex code from ADS-B, the same for the aircraft's whole life
    reg TEXT,
    flight TEXT,
    operator TEXT,
    type TEXT,
    kind TEXT,                           -- medevac, police or other, picked by hand on the map; NULL = learned
    hospital_landings INTEGER NOT NULL DEFAULT 0,
    incident_landings INTEGER NOT NULL DEFAULT 0,
    first_seen INTEGER,
    last_seen INTEGER,
    radio_name TEXT,                     -- the medevac call sign a pilot used on the air band, "Life Flight 2"
    radio_calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS helicopter_landings (
    id INTEGER PRIMARY KEY,
    hex TEXT NOT NULL,
    landing_time INTEGER NOT NULL,
    name TEXT,                           -- what the aircraft was called then
    place TEXT,                          -- the hospital it set down at, else NULL
    incident_id INTEGER,                 -- the incident it came down near, else NULL
    miles REAL
);
CREATE INDEX IF NOT EXISTS helicopter_landings_incident ON helicopter_landings(incident_id);
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

# Bumped when a change to the incident map means existing incidents should be rebuilt.
# 2: EMS type, ALS/BLS, ten-codes no longer read as house numbers.
# 3: incidents followed onto Tac channels, calls joined by channel and cross street.
# 4: police, fire and EMS dispatches to the same scene joined.
# 5: who is talking (speakers.py): a unit's own words aren't a dispatch, and join by the unit talking.
MAP_VERSION = 5

CALL_FIELDS = (
    "call_time", "received_at", "system_id", "system_label", "talkgroup", "talkgroup_label",
    "talkgroup_group", "source", "talker_alias", "frequency", "patches", "audio_path", "air",
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
        if "corrected_at" not in columns:
            # When the transcript was fixed by hand on the call log page (unix seconds), else NULL.
            self._conn.execute("ALTER TABLE calls ADD COLUMN corrected_at INTEGER")
        if "address_fix" not in columns:
            # What an address fixed by address_fix.py was heard as, else NULL.
            self._conn.execute("ALTER TABLE calls ADD COLUMN address_fix TEXT")
        if "audio_deleted_at" not in columns:
            # When old audio was deleted to save space (unix seconds), else NULL. The call stays logged.
            self._conn.execute("ALTER TABLE calls ADD COLUMN audio_deleted_at INTEGER")
        if "map_status" not in columns:
            # NULL: not looked at yet; none: no place heard; placed; unplaced: heard but not found.
            self._conn.execute("ALTER TABLE calls ADD COLUMN map_status TEXT")
        if "check_status" not in columns:
            # The self-check (selfcheck.py): ok, rechecked (a careful retry fixed it) or unsure, with
            # what looked wrong, and the first try's transcript when the retry replaced it.
            self._conn.execute("ALTER TABLE calls ADD COLUMN check_status TEXT")
            self._conn.execute("ALTER TABLE calls ADD COLUMN check_notes TEXT")
            self._conn.execute("ALTER TABLE calls ADD COLUMN first_transcript TEXT")
        if "speaker_kind" not in columns:
            # Who is talking (speakers.py): dispatch, unit or NULL; the name shown; what decided it.
            self._conn.execute("ALTER TABLE calls ADD COLUMN speaker_kind TEXT")
            self._conn.execute("ALTER TABLE calls ADD COLUMN speaker TEXT")
            self._conn.execute("ALTER TABLE calls ADD COLUMN speaker_how TEXT")
        helicopter_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(helicopters)")}
        if "radio_name" not in helicopter_columns:
            self._conn.execute("ALTER TABLE helicopters ADD COLUMN radio_name TEXT")
            self._conn.execute("ALTER TABLE helicopters ADD COLUMN radio_calls INTEGER NOT NULL DEFAULT 0")
        summary_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(summaries)")}
        if "last_call_id" not in summary_columns:
            self._conn.execute("ALTER TABLE summaries ADD COLUMN last_call_id INTEGER")
        if "air" not in columns:
            # Aviation channels (aviation.py): aviation or medevac, else NULL for a county call; and the
            # aircraft the call was matched to on ADS-B, as JSON.
            self._conn.execute("ALTER TABLE calls ADD COLUMN air TEXT")
            self._conn.execute("ALTER TABLE calls ADD COLUMN aircraft TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS calls_air ON calls(air)")
        incident_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(incidents)")}
        if "channel" not in incident_columns:
            self._conn.execute("ALTER TABLE incidents ADD COLUMN channel TEXT")
            self._conn.execute("ALTER TABLE incidents ADD COLUMN channel_time INTEGER")
        # Crashes were called "Vehicle crash" before they became MVA.
        self._conn.execute("UPDATE incidents SET nature = 'MVA' WHERE nature = 'Vehicle crash'")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < MAP_VERSION:
            # How calls are placed and sorted changed: place every call again (lookups stay cached).
            self._conn.execute("DELETE FROM call_places")
            self._conn.execute("DELETE FROM incidents")
            self._conn.execute("DELETE FROM summaries WHERE kind = 'incident'")
            self._conn.execute("DELETE FROM incident_updates")
            self._conn.execute("UPDATE calls SET map_status = NULL WHERE map_status IS NOT NULL")
            self._conn.execute(f"PRAGMA user_version = {MAP_VERSION}")

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

    def audio_to_delete(self, before: int | None = None, keep_unplaced: bool = False) -> list[dict]:
        """Calls whose audio may be deleted, oldest first: transcribed, not on an incident, not alerted.
        With keep_unplaced, calls the map hasn't looked at yet are kept too, as they may join one."""
        where = ["audio_deleted_at IS NULL", "status NOT IN ('queued', 'processing')",
                 "id NOT IN (SELECT call_id FROM call_places WHERE incident_id IS NOT NULL)",
                 "id NOT IN (SELECT call_id FROM alerts WHERE call_id IS NOT NULL)"]
        params = []
        if keep_unplaced:
            where.append("map_status IS NOT NULL")
        if before is not None:
            where.append("call_time < ?")
            params.append(before)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, call_time, audio_path FROM calls WHERE {' AND '.join(where)} ORDER BY call_time, id",
                params).fetchall()
        return [dict(row) for row in rows]

    def mark_audio_deleted(self, call_ids: list[int], when: int):
        with self._lock:
            self._conn.executemany("UPDATE calls SET audio_deleted_at = ? WHERE id = ?",
                                   [(when, call_id) for call_id in call_ids])
            self._conn.commit()

    def pending_ids(self) -> list[int]:
        """Calls left unfinished by a previous run, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM calls WHERE status IN ('queued', 'processing') ORDER BY id"
            ).fetchall()
        return [row["id"] for row in rows]

    def search(self, q: str = "", talkgroup: str = "", source: str = "",
               before_id: int | None = None, limit: int = 100, aviation: bool = False) -> list[dict]:
        """The call log, newest first. Aviation channel calls are only in the aviation list (aviation=True),
        except medevac calls, which are in both."""
        # Morse-code station IDs are hidden (worker.STATION_ID).
        where, params = ["(error IS NULL OR error != 'station ID')"], []
        where.append("air IS NOT NULL" if aviation else "(air IS NULL OR air = 'medevac')")

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
            where.append("(source = ? OR talker_alias LIKE ? OR speaker LIKE ?)")
            params.extend([source, f"%{source}%", source])
        if before_id:
            where.append("id < ?")
            params.append(before_id)

        sql = "SELECT * FROM calls WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))

        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def mark_air(self, names: list[str], emergency_hz: int) -> int:
        """Marks unmarked calls on these talkgroups (numbers or names, lower case) or on 121.5 as aviation."""
        marks = ", ".join("?" for _ in names)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE calls SET air = 'aviation' WHERE air IS NULL AND (LOWER(talkgroup) IN ({marks}) "
                f"OR LOWER(talkgroup_label) IN ({marks}) OR LOWER(talkgroup_group) IN ({marks}) "
                f"OR ABS(COALESCE(frequency, 0) - ?) < 5000)", [*names, *names, *names, emergency_hz])
            self._conn.commit()
            return cur.rowcount

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

    def pages_between(self, since: float, until: float) -> list[dict]:
        """All tone-outs in [since, until), oldest first."""
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                "SELECT * FROM pages WHERE page_time >= ? AND page_time < ? ORDER BY page_time",
                (since, until)).fetchall()]

    def rename_pages(self, page_ids: list[int], old: str, new: str):
        """A learned name that turned out wrong: only the tone-outs that carry it."""
        with self._lock:
            self._conn.executemany("UPDATE pages SET label = ? WHERE id = ? AND label = ?",
                                   [(new, i, old) for i in page_ids])
            self._conn.commit()

    def name_pages(self, page_ids: list[int], label: str):
        with self._lock:
            self._conn.executemany("UPDATE pages SET label = ? WHERE id = ? AND label IS NULL",
                                   [(label, i) for i in page_ids])
            self._conn.commit()

    def transcripts_between(self, start: float, end: float) -> list[dict]:
        """Transcribed calls starting in [start, end), oldest first: what was said around a tone-out."""
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                "SELECT id, call_time, duration_s, talkgroup, talkgroup_label, transcript FROM calls "
                "WHERE call_time >= ? AND call_time < ? AND status = 'done' AND transcript IS NOT NULL "
                "ORDER BY call_time", (start, end)).fetchall()]

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
        names = ("first_time", "last_time", "lat", "lon", "label", "category", "nature", "channel", "channel_time")
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
                    "c.talkgroup_label, c.talkgroup_group, c.talker_alias, c.source, c.speaker, c.speaker_kind, c.speaker_how, "
                    "c.transcript, c.duration_s "
                    "FROM call_places p JOIN calls c ON c.id = p.call_id WHERE p.incident_id = ? "
                    "ORDER BY c.call_time", (incident["id"],)).fetchall()
                incident["calls"] = [dict(row) for row in rows]
        return incidents

    def get_incident(self, incident_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def incident_calls_brief(self, incident_id: int) -> list[dict]:
        """Transcript, talker alias, speaker, talkgroup and time of each call in an incident, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.call_time, c.transcript, c.talker_alias, c.speaker, c.speaker_kind, c.speaker_how, "
                "c.talkgroup, c.talkgroup_label, c.talkgroup_group, p.precision, p.lat, p.lon, p.label "
                "FROM call_places p JOIN calls c ON c.id = p.call_id WHERE p.incident_id = ? "
                "ORDER BY c.call_time, c.id", (incident_id,)).fetchall()
        return [dict(row) for row in rows]

    def incident_of_call(self, call_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT incident_id FROM call_places WHERE call_id = ? AND incident_id IS NOT NULL "
                                     "ORDER BY id LIMIT 1", (call_id,)).fetchone()
        return row["incident_id"] if row else None

    def recent_talkgroups(self, since: int) -> list[dict]:
        """Talkgroups heard since then, busiest first, for the listen page's picker."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT talkgroup, MAX(talkgroup_label) AS label, MAX(talkgroup_group) AS grp, COUNT(*) AS calls, "
                "MAX(call_time) AS last_call FROM calls WHERE call_time >= ? GROUP BY talkgroup "
                "ORDER BY calls DESC", (since,)).fetchall()
        return [dict(r) for r in rows]

    def last_call_on(self, talkgroup: str, before: int, call_id: int) -> dict | None:
        """The call on a talkgroup just before this one, with the incident it is in, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT c.id, c.call_time, (SELECT p.incident_id FROM call_places p WHERE p.call_id = c.id "
                "AND p.incident_id IS NOT NULL LIMIT 1) AS incident_id FROM calls c "
                "WHERE c.talkgroup = ? AND c.id != ? AND c.call_time <= ? AND c.status = 'done' "
                "ORDER BY c.call_time DESC, c.id DESC LIMIT 1", (talkgroup, call_id, before)).fetchone()
        return dict(row) if row else None

    def move_call_places(self, call_id: int, incident_id: int | None, place: dict | None = None):
        """Puts a call in another incident (None: off the map). Adds place for a call that had none."""
        with self._lock:
            if incident_id is None:
                self._conn.execute("DELETE FROM call_places WHERE call_id = ?", (call_id,))
            else:
                cur = self._conn.execute("UPDATE call_places SET incident_id = ? WHERE call_id = ?",
                                         (incident_id, call_id))
                if cur.rowcount == 0 and place is not None:
                    names = ("call_id", "incident_id", "kind", "heard", "label", "lat", "lon", "precision")
                    values = {**place, "call_id": call_id, "incident_id": incident_id}
                    self._conn.execute(f"INSERT INTO call_places ({', '.join(names)}) VALUES "
                                       f"({', '.join('?' for _ in names)})", [values.get(n) for n in names])
            self._conn.commit()

    def delete_incident(self, incident_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
            self._conn.commit()

    def map_edit(self, call_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM map_edits WHERE call_id = ?", (call_id,)).fetchone()
        return dict(row) if row else None

    def set_map_edits(self, call_ids: list[int], anchor_call_id: int | None, lat: float | None = None,
                      lon: float | None = None, label: str | None = None):
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO map_edits (call_id, anchor_call_id, lat, lon, label, edited_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(c, anchor_call_id, lat, lon, label, int(time.time())) for c in call_ids])
            self._conn.commit()

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
            # Incident ids are handed out again; their summaries are written afresh.
            self._conn.execute("DELETE FROM summaries WHERE kind = 'incident'")
            self._conn.execute("DELETE FROM incident_updates")
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

    # ---- summaries

    SUMMARY_FIELDS = ("kind", "ref_id", "basis", "summary", "tags", "status", "model", "made_at", "ms",
                      "last_call_id")

    def store_summary(self, summary: dict):
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO summaries ({', '.join(self.SUMMARY_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in self.SUMMARY_FIELDS)})",
                [summary.get(n) for n in self.SUMMARY_FIELDS])
            self._conn.commit()

    def get_summary(self, kind: str, ref_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM summaries WHERE kind = ? AND ref_id = ?",
                                     (kind, ref_id)).fetchone()
        return dict(row) if row else None

    def summaries(self, kind: str, ref_ids: list[int]) -> dict[int, dict]:
        """Summaries of these incidents or calls, by id."""
        found = {}
        ids = list(ref_ids)
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                rows = self._conn.execute(
                    f"SELECT * FROM summaries WHERE kind = ? AND ref_id IN ({', '.join('?' for _ in chunk)})",
                    [kind, *chunk]).fetchall()
                found.update({row["ref_id"]: dict(row) for row in rows})
        return found

    def delete_summary(self, kind: str, ref_id: int):
        with self._lock:
            self._conn.execute("DELETE FROM summaries WHERE kind = ? AND ref_id = ?", (kind, ref_id))
            if kind == "incident":
                self._conn.execute("DELETE FROM incident_updates WHERE incident_id = ?", (ref_id,))
            self._conn.commit()

    def add_incident_update(self, update: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO incident_updates (incident_id, time, note, status, made_at) VALUES (?, ?, ?, ?, ?)",
                [update.get(n) for n in ("incident_id", "time", "note", "status", "made_at")])
            self._conn.commit()

    def incident_updates(self, incident_ids: list[int]) -> dict[int, list[dict]]:
        """Update notes of these incidents, oldest first, by incident id."""
        found: dict[int, list[dict]] = {}
        ids = list(incident_ids)
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                rows = self._conn.execute(
                    f"SELECT incident_id, time, note, status FROM incident_updates "
                    f"WHERE incident_id IN ({', '.join('?' for _ in chunk)}) ORDER BY time, id", chunk).fetchall()
                for row in rows:
                    found.setdefault(row["incident_id"], []).append(dict(row))
        return found

    def incident_calls_for_summary(self, incident_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT c.id, c.call_time, c.transcript, c.talker_alias, c.speaker, c.speaker_kind, c.speaker_how, "
                "c.talkgroup, c.talkgroup_label "
                "FROM call_places p JOIN calls c ON c.id = p.call_id WHERE p.incident_id = ? "
                "ORDER BY c.call_time, c.id", (incident_id,)).fetchall()
        return [dict(row) for row in rows]

    def incidents_needing_summary(self, since: int) -> list[int]:
        """Recent incidents with no summary yet, or one older than their latest call."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT i.id FROM incidents i LEFT JOIN summaries s ON s.kind = 'incident' AND s.ref_id = i.id "
                "WHERE i.last_time >= ? AND (s.ref_id IS NULL OR s.made_at < i.last_time) "
                "ORDER BY i.last_time", (since,)).fetchall()
        return [row["id"] for row in rows]

    def calls_needing_summary(self, since: int, min_words: int) -> list[int]:
        """Recent transcribed calls long enough for their own summary that don't have one."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.id, c.transcript FROM calls c LEFT JOIN summaries s ON s.kind = 'call' AND s.ref_id = c.id "
                "WHERE c.status = 'done' AND c.call_time >= ? AND s.ref_id IS NULL ORDER BY c.id",
                (since,)).fetchall()
        return [row["id"] for row in rows if len((row["transcript"] or "").split()) >= min_words]

    # ---- phone alerts

    ALERT_FIELDS = ("id", "time", "kind", "title", "message", "tags", "priority", "click", "incident_id",
                    "call_id", "page_id", "page_label")

    def add_alert(self, alert: dict):
        with self._lock:
            self._conn.execute(
                f"INSERT INTO alerts ({', '.join(self.ALERT_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in self.ALERT_FIELDS)})",
                [alert.get(n) for n in self.ALERT_FIELDS])
            self._conn.commit()

    def alerted(self, incident_id: int | None = None, call_id: int | None = None,
                page_label: str | None = None, since: float | None = None, kind: str | None = None) -> bool:
        """Whether an alert (of this kind) already went out for this incident or call, or for this station
        or zone since then."""
        of_kind, args = ("kind = ? AND ", [kind]) if kind else ("", [])
        with self._lock:
            if page_label is not None:
                row = self._conn.execute(f"SELECT 1 FROM alerts WHERE {of_kind}page_label = ? AND time >= ?",
                                         (*args, page_label, since or 0)).fetchone()
            else:
                row = self._conn.execute(f"SELECT 1 FROM alerts WHERE {of_kind}(incident_id = ? OR call_id = ?)",
                                         (*args, incident_id, call_id)).fetchone()
        return row is not None

    def alerts(self, since: float, after_id: str | None = None, alert_id: str | None = None,
               limit: int = 100) -> list[dict]:
        """Alerts oldest first: one by id, or those sent after another one, or since a time."""
        with self._lock:
            if alert_id is not None:
                rows = self._conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchall()
            else:
                after = self._conn.execute("SELECT rowid FROM alerts WHERE id = ?", (after_id,)).fetchone() \
                    if after_id else None
                rows = self._conn.execute(
                    "SELECT * FROM alerts WHERE time >= ? AND rowid > ? ORDER BY rowid DESC LIMIT ?",
                    (since, after[0] if after else 0, limit)).fetchall()[::-1]
        return [dict(row) for row in rows]

    # ---- helicopters from ADS-B (aircraft.py)

    HELICOPTER_FIELDS = ("hex", "reg", "flight", "operator", "type", "kind", "hospital_landings",
                         "incident_landings", "first_seen", "last_seen", "radio_name", "radio_calls")

    def helicopters(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM helicopters").fetchall()
        return {row["hex"]: dict(row) for row in rows}

    def save_helicopter(self, heli: dict):
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO helicopters ({', '.join(self.HELICOPTER_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in self.HELICOPTER_FIELDS)})",
                [heli.get(n) or (0 if n.endswith(("_landings", "_calls")) else None)
                 for n in self.HELICOPTER_FIELDS])
            self._conn.commit()

    def add_helicopter_landing(self, landing: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO helicopter_landings (hex, landing_time, name, place, incident_id, miles) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [landing.get(n) for n in ("hex", "landing_time", "name", "place", "incident_id", "miles")])
            self._conn.commit()

    def helicopter_landings(self, incident_ids: list[int]) -> dict[int, list[dict]]:
        """Landings near these incidents, oldest first, with what the helicopter is known as now."""
        if not incident_ids:
            return {}
        found: dict[int, list[dict]] = {}
        with self._lock:
            rows = self._conn.execute(
                f"SELECT l.*, h.kind AS hand_kind, h.operator, h.hospital_landings, h.incident_landings, "
                f"h.radio_name, h.radio_calls "
                f"FROM helicopter_landings l LEFT JOIN helicopters h ON h.hex = l.hex "
                f"WHERE l.incident_id IN ({', '.join('?' for _ in incident_ids)}) ORDER BY l.landing_time",
                list(incident_ids)).fetchall()
        for row in rows:
            found.setdefault(row["incident_id"], []).append(dict(row))
        return found

    # ---- signal quality per dongle

    SIGNAL_FIELDS = ("hour_start", "tuner", "status", "channel", "decoder", "frequency", "minutes", "messages_good",
                     "messages_bad", "good_percent", "sync_lost_s", "signal_db", "noise_db", "ppm", "ppm_change",
                     "channel_error_hz", "usb_errors")

    def store_signal_hours(self, rows: list[dict], keep_before: int):
        with self._lock:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO signal_hours ({', '.join(self.SIGNAL_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in self.SIGNAL_FIELDS)})",
                [[row.get(n) for n in self.SIGNAL_FIELDS] for row in rows])
            self._conn.execute("DELETE FROM signal_hours WHERE hour_start < ?", (keep_before,))
            self._conn.commit()

    def signal_hours(self, since: int, until: int | None = None) -> list[dict]:
        """Hourly rows from since up to (not including) until, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM signal_hours WHERE hour_start >= ? AND hour_start < ? ORDER BY hour_start, tuner, "
                "channel", (since, until if until is not None else 2 ** 62)).fetchall()
        return [dict(row) for row in rows]

    def previous_signal_hour(self, before: int) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(hour_start) FROM signal_hours WHERE hour_start < ?",
                                     (before,)).fetchone()
        return row[0] if row else None

    def signal_problems(self) -> set[tuple[str, str, str]]:
        with self._lock:
            rows = self._conn.execute("SELECT tuner, channel, problem FROM signal_problems").fetchall()
        return {(row[0], row[1], row[2]) for row in rows}

    def set_signal_problems(self, found: set[tuple[str, str, str]], tuners: set[str], since: int):
        """Keeps the problems found for these dongles and forgets their others, so a cleared problem alerts again
        if it comes back. Problems of dongles not in the report are left alone."""
        with self._lock:
            for tuner, channel, problem in self._conn.execute(
                    "SELECT tuner, channel, problem FROM signal_problems").fetchall():
                if tuner in tuners and (tuner, channel, problem) not in found:
                    self._conn.execute("DELETE FROM signal_problems WHERE tuner = ? AND channel = ? AND problem = ?",
                                       (tuner, channel, problem))
            self._conn.executemany(
                "INSERT OR IGNORE INTO signal_problems (tuner, channel, problem, since) VALUES (?, ?, ?, ?)",
                [(*key, since) for key in found])
            self._conn.commit()

    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM calls GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}


def _fts_query(q: str) -> str:
    """Treats each word as a literal prefix search so punctuation can't break FTS syntax."""
    words = [w.replace('"', '""') for w in q.split()]
    return " ".join(f'"{w}"*' for w in words)
