"""Unit status board: what each fire, EMS and police unit was last heard doing.

Each finished transcript is read for the call signs codes.csv knows ("1382", "20-11", police "2741")
and for status words: dispatched, responding, on scene, transporting, at the hospital, available.
Every unit named in the call takes that status. A call with no status words only updates when the
unit was last heard. Nothing here is certain: it is what the radio said, as Whisper heard it.
"""

import logging
import re
import threading
import time

from .codes import CodeBook
from .db import Database
from .incidents import CLEARED, is_dispatch

log = logging.getLogger(__name__)

# In the order they are checked: the first that matches a call wins. A dispatch names many units
# and comes first; "clear of the hospital, available" is available, not at the hospital.
STATUSES = [
    ("dispatched", None),
    ("available", CLEARED),
    ("transporting", re.compile(
        r"\btransport\w*|\b(en ?route|enroute|responding|going|headed) to (the )?(st\.? luke|lvh|lehigh valley|"
        r"good shep|easton hospital|sacred heart|hospital|er\b|trauma)|\b(one|1|two|2) (patient|pt)s? (on board|aboard)",
        re.IGNORECASE)),
    ("at hospital", re.compile(
        r"\b(at|arriv\w* at|on location at|out at) (the )?(st\.? luke'?s?|lvh|lehigh valley|good shep\w*|"
        r"easton hospital|sacred heart|hospital|er\b|emergency)", re.IGNORECASE)),
    ("on scene", re.compile(r"\bon (the )?(scene|location)\b|\b10-23\b|\barriv(ed|ing)\b|\bon site\b|"
                            r"\bwe'?re here\b|\bestablishing command\b", re.IGNORECASE)),
    ("responding", re.compile(r"\bresponding\b|\ben ?route\b|\benroute\b|\b10-17\b|\bon (the|our) way\b|"
                              r"\bgoing in service and responding\b", re.IGNORECASE)),
]
ORDER = [name for name, _ in STATUSES]
# Kept on the board this long after the unit was last heard.
DEFAULT_HOURS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    unit TEXT PRIMARY KEY,               -- call sign as digits: "1382", "2011", "2741"
    shown TEXT,                          -- as first heard: "20-11"
    meaning TEXT,                        -- from codes.csv: "Easton (Station 20), Engine 1"
    agency TEXT,                         -- fire, ems, police or NULL
    status TEXT,                         -- dispatched, available, transporting, at hospital, on scene, responding
    status_time INTEGER,
    status_call_id INTEGER,              -- the call that set the status (its incident is the unit's)
    last_heard INTEGER,
    last_call_id INTEGER,
    talkgroup TEXT,
    said TEXT                            -- the words that set the status
);
"""


def status_of(text: str | None) -> str | None:
    if not text:
        return None
    if is_dispatch(text):
        return "dispatched"
    for name, pattern in STATUSES[1:]:
        if pattern.search(text):
            return name
    return None


def _agency(meaning: str, talkgroup: str) -> str | None:
    # "Easton Fire/EMS, Engine": the unit type after the comma decides before the station's name.
    kind = meaning.rsplit(",", 1)[-1] if "," in meaning else ""
    text = f"{kind} {meaning} {talkgroup}".lower()
    police = re.compile(r"\bcar\b|police|\bpd\b|pd\d")
    # The talkgroup only decides for a unit codes.csv says nothing about.
    if police.search(meaning.lower()) or (not meaning and police.search(talkgroup.lower())):
        return "police"
    ems = re.compile(r"ambulance|medic|\bems\b|\bals\b|\bbls\b|\bqrs\b")
    fire = re.compile(r"engine|ladder|truck|tanker|rescue|squad|brush|chief|fire|tower|utility")
    for part in (kind.lower(), text):
        if ems.search(part):
            return "ems"
        if fire.search(part):
            return "fire"
    return None


def _snippet(text: str, code: str) -> str:
    """The part of the call around the call sign, for the board."""
    at = text.find(code)
    if at < 0 or len(text) <= 120:
        return text[:120]
    start = max(0, at - 50)
    return ("…" if start else "") + text[start:start + 120] + ("…" if start + 120 < len(text) else "")


class UnitBoard:
    def __init__(self, db: Database, codebook: CodeBook):
        self.db = db
        self.codebook = codebook
        self._lock = threading.Lock()
        with db._lock:
            db._conn.executescript(SCHEMA)
            db._conn.commit()

    def units_in(self, call: dict) -> list[dict]:
        """The call signs codes.csv knows in a call: [{unit, shown, meaning}]."""
        context = (call.get("talkgroup_label"), call.get("talkgroup_group"))
        found = {}
        for item in self.codebook.annotate(call.get("transcript"), *context):
            code = item.get("code") or ""
            if item.get("kind") != "unit" or not re.fullmatch(r"\d{2}-?\d{1,2}", code):
                continue
            key = code.replace("-", "")
            found.setdefault(key, {"unit": key, "shown": code, "meaning": item.get("meaning") or ""})
        return list(found.values())

    def call_done(self, call_id: int):
        call = self.db.get_call(call_id)
        if call and call["status"] == "done":
            self.process(call)

    def process(self, call: dict):
        text = call.get("transcript") or ""
        units = self.units_in(call)
        if not units:
            return
        status = status_of(text)
        when = call["call_time"]
        talkgroup = call.get("talkgroup_label") or call.get("talkgroup") or ""
        with self._lock, self.db._lock:
            conn = self.db._conn
            for unit in units:
                row = conn.execute("SELECT status_time, last_heard FROM units WHERE unit = ?", (unit["unit"],)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO units (unit, shown, meaning, agency, last_heard, last_call_id, talkgroup) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (unit["unit"], unit["shown"], unit["meaning"], _agency(unit["meaning"], talkgroup),
                                  when, call["id"], talkgroup))
                elif row["last_heard"] is None or when >= row["last_heard"]:
                    conn.execute("UPDATE units SET last_heard = ?, last_call_id = ?, talkgroup = ?, "
                                 "meaning = COALESCE(NULLIF(?, ''), meaning) WHERE unit = ?",
                                 (when, call["id"], talkgroup, unit["meaning"], unit["unit"]))
                # Calls can be transcribed out of order (fire and EMS first); an older call never
                # overwrites a newer status.
                if status and (row is None or row["status_time"] is None or when >= row["status_time"]):
                    conn.execute("UPDATE units SET status = ?, status_time = ?, status_call_id = ?, said = ? "
                                 "WHERE unit = ?", (status, when, call["id"], _snippet(text, unit["shown"]),
                                                    unit["unit"]))
            conn.commit()

    def rebuild(self, hours: float = DEFAULT_HOURS, now: float | None = None) -> int:
        """Starts the board over from the calls of the last `hours`."""
        since = int((time.time() if now is None else now) - hours * 3600)
        with self.db._lock:
            self.db._conn.execute("DELETE FROM units")
            rows = [dict(r) for r in self.db._conn.execute(
                "SELECT * FROM calls WHERE status = 'done' AND call_time >= ? ORDER BY call_time, id", (since,))]
            self.db._conn.commit()
        for call in rows:
            self.process(call)
        return len(rows)

    def board(self, hours: float = DEFAULT_HOURS, now: float | None = None) -> list[dict]:
        """Units heard in the last `hours`, each with its incident when the status call was placed on the map."""
        since = int((time.time() if now is None else now) - hours * 3600)
        with self.db._lock:
            rows = [dict(r) for r in self.db._conn.execute(
                "SELECT u.*, p.incident_id, i.label AS incident_label, i.nature AS incident_nature "
                "FROM units u LEFT JOIN call_places p ON p.call_id = u.status_call_id "
                "LEFT JOIN incidents i ON i.id = p.incident_id "
                "WHERE u.last_heard >= ? GROUP BY u.unit ORDER BY u.status_time DESC", (since,))]
        return rows
