"""Who is talking in a call: the dispatcher or a unit.

SDRTrunk sends the radio ID of the talker, but on most of Ken's talkgroups it is 0 (unknown). So each
call is labelled from three things, strongest first:

1. A radio ID that isn't 0 is a unit. Its call sign is learned when it says one ("1781 County",
   "2851 from 2857") and used on its later calls that don't.
2. How people call each other here, which differs by talkgroup. On police talkgroups a unit names
   itself, then County ("1770 County, I'm clear") and the dispatcher names County first ("County 1784
   for your status"). On fire and EMS talkgroups it's the other way: a unit calls "County, 2851,
   responding" and the dispatcher answers "1691, go ahead, County". "X from Y" is Y everywhere.
3. What dispatch sounds like (an address and a call type, "respond to", "year-old", "show you on
   scene", "any units") against what a unit sounds like ("I'm", "we're", "show us"). On a talkgroup
   where units usually do send their radio ID, a call with ID 0 leans dispatch. Talkgroups that are
   only dispatch (config: [speakers] dispatch_talkgroups) lean dispatch too.

A call with nothing to go on is left unlabelled rather than guessed. Labels are stored on the call
(speaker_kind dispatch/unit, speaker the name shown, speaker_how what decided) for the calls page,
the incident map, the unit board and the summaries.
"""

import re
import threading

from .incidents import is_dispatch

DISPATCH = "dispatch"
UNIT = "unit"

# A call sign as Whisper writes it: "1781", "17-81", "16-12-10", "Engine 2011", "Medic 27", "27 EMS 1".
_SIGN = (r"(?:(?:engine|medic|ems|rescue|ladder|truck|tanker|squad|chief|car|unit|battalion|tower|"
         r"brush|utility|ambulance)\s+)?\d{1,4}(?:[- ]\d{1,2}){0,2}(?:\s+(?:ems|medic|engine|rescue)\s+\d{1,2})?")
COUNTY = r"(?:northampton\s+)?(?:county|count(?:y|ing|ry)?|dispatch)"
# "1770 County, ..." / "17-81 to the County": a call sign, then County.
SIGN_THEN_COUNTY = re.compile(rf"^\W*(?:10-4,?\s+)?(?P<unit>{_SIGN})[,.]?\s+"
                              rf"(?:go ahead,?\s+|to\s+(?:the\s+)?|the\s+)?{COUNTY}\b", re.IGNORECASE)
# "2851 from 2857" / "County from 1781".
FROM = re.compile(rf"\b(?:{_SIGN}|{COUNTY}),?\s+from\s+(?P<unit>{_SIGN}|{COUNTY}|command)\b", re.IGNORECASE)
# "County, 1784 for your status" / "Dispatch to 1382": County, then maybe a call sign.
COUNTY_THEN = re.compile(rf"^\W*(?:northampton\s+)?(?:county|dispatch|communications)\b(?!\s+(?:police|fire|ems)\b)"
                         rf"(?:,?\s+(?:to\s+)?(?P<unit>{_SIGN}))?", re.IGNORECASE)
# What Whisper writes for a ten-code or a clock time is not a call sign.
NOT_A_SIGN = re.compile(r"^(10-\d{1,2}|\d{1,2}:\d{2})$")

DISPATCH_WORDS = [re.compile(p, re.IGNORECASE) for p in (
    r"\byear[- ]old\b",
    r"\b(any|all) units\b|\bunits? calling\b",
    r"\b(i'?ll |i will )?show (you|ya|him|them)\b|\bhave you (on scene|on location|out|clear|available|en ?route|10-)",
    r"\bfor your status\b|\bwhat'?s your (status|location|20)\b",
    r"\bcaller (is|was|states|reports|reported|reporting|advises|said|says)\b|\breporting party\b|\brp (is|states)\b",
    r"\bcross(es)? (street|st\b|of)\b|\bbetween .{3,30} and\b",
    r"\b(you can|go ahead and) (clear|cancel|disregard|switch|go to|respond)\b(?! (me|us)\b)",
    r"\bfor (a|an) (\d+|report|possible|unknown|male|female|alarm|fire|mva|disabled)\b",
    r"\b\d{1,2}[:\-]?\d{2} (hours|hrs)\b",
)]
UNIT_WORDS = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(i'?m|i am|we'?re|we are|i'?ll be|we'?ll be|i'?ve|we'?ve)\b",
    r"\b(show|put|mark|give|clear|cancel|add|dispatch|send) (me|us)\b|\bcan you (show|put|mark|run|check) (me|us|a|this)\b",
    r"\b(we|i) (have|got|need|see|copy|cleared?|checked)\b",
    r"\b(my|our) (unit|car|location|crew|patient)\b",
    r"\b(on|at) (the )?scene\b(?!.{0,15}\bhours\b)",
    r"\bnothing (found|showing|seen)\b|\bnegative (contact|results)\b",
    r"\bcommand\b.{0,20}\b(established|terminated)\b|\bestablishing command\b",
)]
# A dispatch tone-out read in full: address repeated ("650 Washington Boulevard, 650 Washington Boulevard").
REPEATED_ADDRESS = re.compile(r"\b(\d{1,5},? [a-z]+(?: [a-z]+)?)\b.{0,40}\b\1\b", re.IGNORECASE)

# Below this many recent calls a talkgroup's share of radio IDs is not trusted.
MIN_CALLS_FOR_SHARE = 30
# A talkgroup where at least this share of calls carries a real radio ID sends units' IDs; there a 0
# is most likely the dispatch console. Not on Tac channels: there units without an ID do most of the
# talking.
ID_SHARE = 0.25
# Recent calls per talkgroup counted for that share.
SHARE_WINDOW = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_units (
    source TEXT PRIMARY KEY,             -- radio ID from SDRTrunk
    unit TEXT NOT NULL,                  -- call sign as digits: "1781"
    shown TEXT NOT NULL,                 -- as heard: "17-81"
    times INTEGER NOT NULL DEFAULT 1,    -- how often it said this call sign
    last_seen INTEGER
);
"""


def has_radio_id(source) -> bool:
    return bool(source) and str(source).strip() not in ("", "0")


def sign_key(sign: str) -> str:
    """"17-81" and "1781" are the same unit; "Medic 27" keeps its word."""
    words = re.sub(r"[^a-z0-9 ]", " ", sign.lower()).split()
    return " ".join(w for w in words if not w.isdigit()) + "".join(w for w in words if w.isdigit())


def _sign(sign: str | None) -> str | None:
    sign = (sign or "").strip(" ,.")
    return sign if sign and not NOT_A_SIGN.match(sign) else None


def _caller(text: str, police: bool) -> tuple[str | None, str | None]:
    """(kind, call sign) when the words say who is calling, else (None, None)."""
    m = FROM.search(text)
    if m:
        unit = m.group("unit")
        if re.fullmatch(COUNTY, unit, re.IGNORECASE):
            return DISPATCH, None
        return UNIT, _sign(unit) if unit.lower() != "command" else "Command"
    first, then = SIGN_THEN_COUNTY.match(text), COUNTY_THEN.match(text)
    if police:
        if first and _sign(first.group("unit")):
            return UNIT, _sign(first.group("unit"))
        if then:
            return DISPATCH, None
    else:
        # A tone-out read by the dispatcher can start "County, EMS 810, ..." too.
        if is_dispatch(text) or REPEATED_ADDRESS.search(text):
            return None, None
        if then:
            return UNIT, _sign(then.group("unit"))
        if first and _sign(first.group("unit")):
            return DISPATCH, None
    return None, None


def word_scores(text: str) -> tuple[int, int]:
    """(dispatch, unit): how many of each kind of phrase a transcript has."""
    dispatch = sum(1 for p in DISPATCH_WORDS if p.search(text))
    if is_dispatch(text):
        dispatch += 2
    if REPEATED_ADDRESS.search(text):
        dispatch += 2
    unit = sum(1 for p in UNIT_WORDS if p.search(text))
    return dispatch, unit


class SpeakerBook:
    def __init__(self, db, dispatch_talkgroups=("Countywide",), police_talkgroups=("PD", "Police", "Sheriff")):
        self.db = db
        self.dispatch_talkgroups = [str(t).lower() for t in dispatch_talkgroups if str(t).strip()]
        # Part of a talkgroup name: "PD" is in "Co PD N1 Patch" and in "Co EPD1 Patch".
        self.police_talkgroups = [str(t).lower() for t in police_talkgroups if str(t).strip()]
        self._lock = threading.Lock()
        # talkgroup -> recent (has radio id) flags, newest last.
        self._ids: dict[str, list[bool]] = {}
        with db._lock:
            db._conn.executescript(SCHEMA)
            db._conn.commit()
            for row in db._conn.execute(
                    "SELECT talkgroup, source FROM (SELECT talkgroup, source, id, ROW_NUMBER() OVER "
                    "(PARTITION BY talkgroup ORDER BY id DESC) AS n FROM calls) WHERE n <= ? ORDER BY id",
                    (SHARE_WINDOW,)):
                self._ids.setdefault(row["talkgroup"] or "", []).append(has_radio_id(row["source"]))

    def id_share(self, talkgroup: str | None) -> float | None:
        flags = self._ids.get(talkgroup or "", [])
        return sum(flags) / len(flags) if len(flags) >= MIN_CALLS_FOR_SHARE else None

    def _count_id(self, call: dict):
        flags = self._ids.setdefault(call.get("talkgroup") or "", [])
        flags.append(has_radio_id(call.get("source")))
        del flags[:-SHARE_WINDOW]

    def _learned(self, source: str) -> str | None:
        with self.db._lock:
            row = self.db._conn.execute("SELECT shown FROM radio_units WHERE source = ?", (source,)).fetchone()
        return row["shown"] if row else None

    def _learn(self, source: str, sign: str, when):
        key = sign_key(sign)
        with self.db._lock:
            conn = self.db._conn
            row = conn.execute("SELECT unit, times FROM radio_units WHERE source = ?", (source,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO radio_units (source, unit, shown, times, last_seen) VALUES (?, ?, ?, 1, ?)",
                             (source, key, sign, when))
            elif row["unit"] == key:
                conn.execute("UPDATE radio_units SET times = times + 1, shown = ?, last_seen = ? WHERE source = ?",
                             (sign, when, source))
            elif row["times"] <= 1:
                # One mishearing doesn't stick; a radio handed to another unit takes two calls to change.
                conn.execute("UPDATE radio_units SET unit = ?, shown = ?, times = 1, last_seen = ? WHERE source = ?",
                             (key, sign, when, source))
            else:
                conn.execute("UPDATE radio_units SET times = times - 1 WHERE source = ?", (source,))
            conn.commit()

    def label(self, call: dict, learn: bool = True) -> tuple[str | None, str | None, str | None]:
        """(kind, name, how) for a finished call: ("unit", "1781", "radio id"), ("dispatch",
        "Dispatch", "words"), or (None, None, None) when there's nothing to go on. learn=False for a
        call looked at again: its radio ID and call sign were counted the first time."""
        text = (call.get("transcript") or "").strip()
        source = str(call.get("source") or "").strip()
        with self._lock:
            share = self.id_share(call.get("talkgroup"))
            if learn:
                self._count_id(call)
        names = " ".join(str(call.get(k) or "") for k in ("talkgroup_label", "talkgroup_group")).lower()
        police = any(t in names for t in self.police_talkgroups)
        kind, sign = _caller(text, police) if text else (None, None)

        if has_radio_id(source):
            if kind == UNIT and sign:
                if learn:
                    self._learn(source, sign, call.get("call_time"))
                return UNIT, sign, "radio id"
            return UNIT, self._learned(source) or "Unit", "radio id"
        if not text:
            return None, None, None
        if kind == UNIT:
            return UNIT, sign or "Unit", "words"
        if kind == DISPATCH:
            return DISPATCH, "Dispatch", "words"

        dispatch, unit = word_scores(text)
        if any(t in names for t in self.dispatch_talkgroups):
            dispatch += 1
        if share is not None and share >= ID_SHARE and "tac" not in names:
            # Units here send their ID; no ID is most likely the console.
            dispatch += 1
        if dispatch > unit:
            return DISPATCH, "Dispatch", "words"
        if unit > dispatch:
            return UNIT, "Unit", "words"
        return None, None, None

    def apply(self, call_id: int, learn: bool = True) -> dict | None:
        """Labels a finished call and stores the label on it. Returns the call as stored."""
        call = self.db.get_call(call_id)
        if call is None or call["status"] != "done":
            return call
        kind, name, how = self.label(call, learn)
        how = how or "none"
        self.db.update_call(call_id, speaker_kind=kind, speaker=name, speaker_how=how)
        call.update(speaker_kind=kind, speaker=name, speaker_how=how)
        return call

    def backfill(self, limit: int | None = None) -> int:
        """Labels finished calls logged before speaker labels existed, oldest first, so radio IDs are
        learned in order. Returns how many were labelled."""
        with self.db._lock:
            rows = [dict(r) for r in self.db._conn.execute(
                "SELECT id, call_time, talkgroup, talkgroup_label, talkgroup_group, source, transcript, status "
                "FROM calls WHERE status = 'done' AND speaker_how IS NULL ORDER BY id"
                + (" LIMIT ?" if limit else ""), (limit,) if limit else ())]
        # Each call already counted towards its talkgroup's ID share at start; don't count it twice.
        saved, self._ids = self._ids, {}
        try:
            labels = [(*self.label(call), call["id"]) for call in rows]
        finally:
            self._ids = saved
        with self.db._lock:
            # "none" marks a call looked at with nothing to go on, so it isn't looked at again.
            self.db._conn.executemany("UPDATE calls SET speaker_kind = ?, speaker = ?, speaker_how = "
                                      "COALESCE(?, 'none') WHERE id = ?", labels)
            self.db._conn.commit()
        return len(rows)
