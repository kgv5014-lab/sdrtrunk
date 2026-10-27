"""Whisper hints learned from the call log: the streets and units of what is going on right now, and
the ones each talkgroup names most.

- Live: places and units from calls placed on the incident map in the last hour ([hints]
  active_minutes). Follow-up calls ("2011 on location, Northampton Street") get them spelled right.
- Most heard: over the last month ([hints] history_days), the places found most often in each
  talkgroup's calls, and the unit call signs said most often. Talkgroups with few calls of their own
  get the county-wide favorites.

Nothing here is written to a file. The log is read on its own read-only connection, so it works the
same in the service and in compare.py, which asks for the hints a call would have had at its time.
"""

import logging
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from .config import HintsConfig
from .incidents import units_in

log = logging.getLogger(__name__)

# "1234 block of Northampton Street", "1234 Northampton Street": the street without the house number.
HOUSE_NUMBER = re.compile(r"^\d+[a-z]?\s+(block of\s+)?", re.IGNORECASE)
# Unit words worth hinting. "Car" and "Unit" numbers are too often something else.
UNIT_WORDS = {"engine", "medic", "ladder", "truck", "tanker", "rescue", "squad", "ambulance", "chief", "brush",
              "utility", "qrs", "tower", "battalion", "deputy"}
SPELLED = {"qrs": "QRS"}
# Live hints come from at most this many incidents, the newest first.
MAX_INCIDENTS = 4


def place_names(label: str | None) -> list[str]:
    """Names in a map label: "1234 Northampton Street, Easton" -> ["Northampton Street"],
    "Main Street & 3rd Street, Wilson" -> ["Main Street", "3rd Street"], "Wawa, Palmer" -> ["Wawa"]."""
    names = []
    for part in (label or "").split(",")[0].split(" & "):
        name = HOUSE_NUMBER.sub("", part.strip()).strip()
        if name and not name.isdigit():
            names.append(name)
    return names


def unit_names(text: str | None) -> list[str]:
    """Unit call signs in a transcript, as hints: "medic 2081" -> "Medic 2081"."""
    found = []
    for unit in sorted(units_in(text)):
        word, _, number = unit.partition(" ")
        if number and word in UNIT_WORDS:
            found.append(f"{SPELLED.get(word, word.title())} {number}")
    return found


def fit(terms: list[str], chars: int) -> list[str]:
    """The first terms (most important first) that fit in chars once joined with ", "."""
    picked, used, seen = [], 0, set()
    for term in terms:
        if term.lower() in seen:
            continue
        if used + len(term) + 2 > chars:
            break
        seen.add(term.lower())
        picked.append(term)
        used += len(term) + 2
    return picked


class LearnedHints:
    def __init__(self, db_path: Path, config: HintsConfig):
        self.db_path = db_path
        self.config = config
        self._lock = threading.Lock()
        self._history: dict | None = None
        self._history_until = 0

    def _connect(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def terms(self, talkgroup: str | None, when: float | None = None, live: bool = True) -> list[str]:
        """Learned hints for a call on this talkgroup, least important first (like the other hint
        lists, so the prompt's end, which Whisper weighs most, gets the live incidents).
        when: the call's time; hints come only from calls before it. live=False skips the hour's
        incidents (for comparing)."""
        if not self.config.enabled or self.config.prompt_chars <= 0:
            return []
        when = when if when is not None else time.time()
        try:
            active = self.active(when) if live else []
            history = self.history(when)
        except sqlite3.Error as e:
            log.warning("Could not read learned hints from the call log: %s", e)
            return []
        talkgroup = str(talkgroup or "")
        heard = history["places"].get(talkgroup, []) + history["units"].get(talkgroup, [])
        everywhere = history["places"].get("", []) + history["units"].get("", [])
        return list(reversed(fit(active + heard + everywhere, self.config.prompt_chars)))

    def active(self, when: float) -> list[str]:
        """Places and units of incidents with a placed call in the last active_minutes, the newest
        incident first."""
        since = when - self.config.active_minutes * 60
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT p.incident_id, p.label, c.transcript, c.talker_alias, c.call_time FROM call_places p "
                "JOIN calls c ON c.id = p.call_id JOIN incidents i ON i.id = p.incident_id "
                "WHERE c.call_time >= ? AND c.call_time < ? AND i.hidden = 0 ORDER BY c.call_time DESC",
                (since, when)).fetchall()
        finally:
            conn.close()
        incidents: dict[int, list[str]] = {}
        for row in rows:
            if row["incident_id"] not in incidents and len(incidents) == MAX_INCIDENTS:
                continue
            terms = incidents.setdefault(row["incident_id"], [])
            terms += place_names(row["label"])
            terms += unit_names(" ".join(t for t in (row["transcript"], row["talker_alias"]) if t))
        return [t for terms in incidents.values() for t in terms]

    def history(self, when: float) -> dict:
        """Most heard places and units per talkgroup ("" = all of them), most heard first. Rebuilt
        every refresh_minutes, or when asked about another time (compare.py)."""
        with self._lock:
            fresh = self._history is not None and 0 <= when - self._history_until < self.config.refresh_minutes * 60
            if not fresh:
                started = time.perf_counter()
                self._history = self._build_history(when)
                self._history_until = when
                log.info("Learned hints: %d places and %d units from the last %d days, in %.1f s",
                         len(self._history["places"].get("", [])), len(self._history["units"].get("", [])),
                         self.config.history_days, time.perf_counter() - started)
            return self._history

    def _build_history(self, when: float) -> dict:
        since = when - self.config.history_days * 86400
        places: dict[str, Counter] = defaultdict(Counter)
        units: dict[str, Counter] = defaultdict(Counter)
        conn = self._connect()
        if conn is None:
            return {"places": {}, "units": {}}
        try:
            for row in conn.execute(
                    "SELECT c.talkgroup, p.label FROM call_places p JOIN calls c ON c.id = p.call_id "
                    "WHERE c.call_time >= ? AND c.call_time < ?", (since, when)):
                for name in place_names(row["label"]):
                    places[str(row["talkgroup"] or "")][name] += 1
            for row in conn.execute(
                    "SELECT talkgroup, transcript FROM calls WHERE call_time >= ? AND call_time < ? "
                    "AND transcript IS NOT NULL AND transcript != ''", (since, when)):
                for name in unit_names(row["transcript"]):
                    units[str(row["talkgroup"] or "")][name] += 1
        finally:
            conn.close()
        return {"places": self._ranked(places), "units": self._ranked(units)}

    def _ranked(self, counts: dict[str, Counter]) -> dict[str, list[str]]:
        everywhere = Counter()
        for counter in counts.values():
            everywhere.update(counter)
        ranked = {"": [name for name, n in everywhere.most_common() if n >= self.config.min_count]}
        for talkgroup, counter in counts.items():
            ranked[talkgroup] = [name for name, n in counter.most_common() if n >= self.config.min_count]
        return ranked
