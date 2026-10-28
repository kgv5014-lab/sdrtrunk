"""Address autocorrect: snaps a house number (or street) Whisper got slightly wrong to a real address.

Uses address-ranges.json (house numbers on each side of every street, from the US Census; see tiger.py,
written by fetch-places.bat) and the street list (addresses.Gazetteer). For "4500 Main Street":

1. Main Street has a 4500 (or something close to it): left alone.
2. It doesn't: the usual Whisper slips are tried, cheapest first. Fifteen heard as fifty (1550 / 5050), a
   digit too many or too few, two digits swapped. Then a similar-sounding street in the area that has 4500.
3. The fix is made only when exactly one answer is cheapest. Ties are settled by an incident already on
   the map nearby, otherwise nothing changes.

Without address-ranges.json nothing changes. The raw Whisper text is kept either way.

    python -m sdrtrunk_transcriber.address_fix --days 30     # what it would have fixed on the call log
"""

import argparse
import json
import logging
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .addresses import SUFFIXES, UNIT_WORDS, Gazetteer, Projection, _in_towns, _name_key, split_name, town_key, words
from .tiger import along

log = logging.getLogger(__name__)

# A heard number this close to a real range (5% of it, at least 10) is left alone: the Census ranges
# don't list every house.
NEAR_SHARE = 0.05
NEAR_MIN = 10
# A street this similar to the heard one can be swapped in when only it has the heard house number.
STREET_RATIO = 0.8
STREET_COST = 3
# A tie is settled by an incident on the map within this distance.
NEAR_INCIDENT_M = 500

TOKEN = re.compile(r"\d+(?:-\d+)+|[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
# Between the words of an address: spaces, or "N. 3rd".
GAP = re.compile(r"\.?\s+")
HOUSE = re.compile(r"\d{1,5}")


@dataclass
class Side:
    first: int
    last: int
    line: list
    town: str

    def has(self, n: int) -> bool:
        lo, hi = min(self.first, self.last), max(self.first, self.last)
        if not lo <= n <= hi:
            return False
        if self.first % 2 == self.last % 2:
            return n % 2 == self.first % 2
        return True

    def gap(self, n: int) -> int:
        lo, hi = min(self.first, self.last), max(self.first, self.last)
        return 0 if lo <= n <= hi else min(abs(n - lo), abs(n - hi))

    def block_has(self, n: int) -> bool:
        lo, hi = min(self.first, self.last), max(self.first, self.last)
        return lo <= n + 99 and n <= hi


class AddressBook:
    """House-number ranges by street name, re-read when the file changes."""

    def __init__(self, path: Path | None):
        self.path = path
        self._stamp = None
        self._lock = threading.Lock()
        self.by_name: dict[str, list[Side]] = {}
        self.proj = Projection()
        self.reload_if_changed()

    @property
    def loaded(self) -> bool:
        return bool(self.by_name)

    def reload_if_changed(self):
        try:
            stat = self.path.stat() if self.path else None
            stamp = (stat.st_mtime_ns, stat.st_size) if stat else None
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        with self._lock:
            self._stamp = stamp
            by_name: dict[str, list[Side]] = {}
            if stamp:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    for street in data.get("streets", []):
                        sides = by_name.setdefault(_name_key(street["name"]), [])
                        town = town_key(street.get("town") or "")
                        for lf, lt, rf, rt, line in street.get("segments", []):
                            line = [tuple(p) for p in line]
                            sides += [Side(f, t, line, town) for f, t in ((lf, lt), (rf, rt)) if f and t]
                    log.info("House-number ranges: %d streets", len(by_name))
                except Exception:
                    log.exception("Could not read %s", self.path)
            self.by_name = by_name

    def sides(self, name: str, towns: set[str] | None = None) -> list[Side]:
        sides = self.by_name.get(_name_key(name), [])
        if towns:
            local = [s for s in sides if s.town in towns]
            if local:
                return local
        return sides

    def has(self, name: str, n: int, towns: set[str] | None = None, block: bool = False) -> bool:
        return any(s.block_has(n) if block else s.has(n) for s in self.sides(name, towns))

    def gap(self, name: str, n: int, towns: set[str] | None = None) -> int | None:
        """How far a number is from the nearest range on the street, None when the street has none."""
        sides = self.sides(name, towns)
        return min((s.gap(n) for s in sides), default=None)

    def locate(self, name: str, n: int, towns: set[str] | None = None, block: bool = False) -> tuple | None:
        """Where a house number is, worked out from where it falls in its range. None when unknown or
        when the street has that number in two far-apart places (two towns) and nothing says which."""
        n = n + 50 if block else n
        points = []
        for side in self.sides(name, towns):
            if not side.has(n) and not (block and side.block_has(n - 50)):
                continue
            span = side.last - side.first
            fraction = (n - side.first) / span if span else 0.5
            points.append(along(side.line, fraction, self.proj))
        if not points:
            return None
        lat, lon = points[0]
        if any(self._distance(lat, lon, *p) > NEAR_INCIDENT_M for p in points[1:]):
            return None
        return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)

    def _distance(self, lat1, lon1, lat2, lon2) -> float:
        x1, y1 = self.proj.xy(lat1, lon1)
        x2, y2 = self.proj.xy(lat2, lon2)
        return math.hypot(x2 - x1, y2 - y1)


def number_slips(heard: str) -> dict[str, int]:
    """Numbers Whisper may have meant, with a cost: 1 for fifteen/fifty, 2 for a digit too many, too few
    or swapped."""
    out: dict[str, int] = {}

    def add(number: str, cost: int):
        if number and number[0] != "0" and number != heard and len(number) <= 5:
            out[number] = min(cost, out.get(number, cost))

    for k in range(len(heard) - 1):
        a, b = heard[k], heard[k + 1]
        if a == "1" and b in "3456789":
            add(heard[:k] + b + "0" + heard[k + 2:], 1)      # "fifteen" heard, "fifty" said
        if b == "0" and a in "3456789":
            add(heard[:k] + "1" + a + heard[k + 2:], 1)      # "fifty" heard, "fifteen" said
        add(heard[:k] + b + a + heard[k + 2:], 2)
    for k in range(len(heard)):
        add(heard[:k] + heard[k + 1:], 2)                    # a digit too many
        add(heard[:k] + heard[k] + heard[k:], 2)             # a doubled digit missed
    add(heard + "0", 2)
    return out


@dataclass
class Fix:
    start: int
    end: int
    heard: str
    text: str


class AddressFixer:
    def __init__(self, gazetteer: Gazetteer, book: AddressBook, near_incidents=None):
        self.gazetteer = gazetteer
        self.book = book
        # Called with a call time, returns (lat, lon) of incidents on the map then (or None).
        self.near_incidents = near_incidents

    def fix(self, text: str, context: str | None = None, call_time: int | None = None) -> tuple[str, list[str]]:
        """The transcript with addresses fixed, and what each fixed address was heard as."""
        if not text:
            return text, []
        self.book.reload_if_changed()
        self.gazetteer.reload_if_changed()
        if not self.book.loaded or not self.gazetteer.loaded:
            return text, []
        w = words(text)
        towns = self.gazetteer.towns_in(w)
        if not towns and context:
            towns = self.gazetteer._towns_anywhere(words(context))
        fixes = self.find(text, towns, call_time)
        for fix in reversed(fixes):
            text = text[:fix.start] + fix.text + text[fix.end:]
        return text, [fix.heard for fix in fixes]

    def find(self, text: str, towns: set[str], call_time: int | None) -> list[Fix]:
        tokens = list(TOKEN.finditer(text))
        fixes = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            before = tokens[i - 1].group(0).lower() if i else ""
            if not HOUSE.fullmatch(token.group(0)) or before in UNIT_WORDS or (before.isdigit() and i):
                i += 1
                continue
            j = i + 1
            block = j + 1 < len(tokens) and [t.group(0).lower() for t in tokens[j:j + 2]] == ["block", "of"] \
                and self._joined(text, tokens, i, j + 2)
            if block:
                j += 2
            fix, end = self._address(text, tokens, i, j, block, towns, call_time)
            if fix:
                fixes.append(fix)
            i = max(end, i + 1)
        return fixes

    @staticmethod
    def _joined(text, tokens, first, last) -> bool:
        """Tokens first..last-1 are separated only by spaces."""
        return all(GAP.fullmatch(text[tokens[k].end():tokens[k + 1].start()]) for k in range(first, last - 1))

    def _address(self, text, tokens, i, j, block, towns, call_time) -> tuple[Fix | None, int]:
        number = tokens[i].group(0)
        # The street: the longest run of words after the number that is a street in the list.
        for length in range(min(6, len(tokens) - j), 0, -1):
            if not self._joined(text, tokens, i, j + length):
                continue
            phrase_start, phrase_end = tokens[j].start(), tokens[j + length - 1].end()
            w = words(text[phrase_start:phrase_end])
            mentions = self.gazetteer.mentions(w)
            if mentions and mentions[0].start == 0 and mentions[0].end == len(w) and mentions[0].strong:
                streets = mentions[0].streets
                if split_name(streets[0].name)[0][:1] == ("route",) and len(number) < 3:
                    return None, j + length   # "Engine 4 Route 22"
                fix = self._check(text, tokens[i], (phrase_start, phrase_end), number, block,
                                  _in_towns(streets, towns), towns, call_time)
                return fix, j + length
        # Not a street in the list: "1234 Freemansberg Avenue". Find the street word and look for a
        # similar street that has this house number.
        for length in range(2, min(5, len(tokens) - j) + 1):
            if not self._joined(text, tokens, i, j + length):
                break
            last = words(tokens[j + length - 1].group(0))
            if last and last[-1] in SUFFIXES:
                phrase = (tokens[j].start(), tokens[j + length - 1].end())
                core = " ".join(words(text[phrase[0]:tokens[j + length - 2].end()]))
                fix = self._similar_street(text, tokens[i], phrase, number, block, core, last[-1], towns, call_time)
                return fix, j + length
        return None, j

    def _check(self, text, number_token, phrase, number, block, streets, towns, call_time) -> Fix | None:
        names = list(dict.fromkeys(s.name for s in streets))
        known = [name for name in names if self.book.sides(name)]
        if not known:
            return None                  # no house numbers for this street: nothing to check against
        n = int(number)
        street_towns = {s.town_key for s in streets if s.town_key} or towns
        # Anywhere on the street, in any town: a real address somewhere is never "fixed".
        if any(self.book.has(name, n, block=block) for name in known):
            return None
        gaps = [g for g in (self.book.gap(name, n) for name in known) if g is not None]
        if gaps and min(gaps) <= max(NEAR_MIN, n * NEAR_SHARE):
            return None
        candidates = []
        for slip, cost in number_slips(number).items():
            for name in known:
                if self.book.has(name, int(slip), street_towns, block):
                    candidates.append((cost, slip, name))
        core, suffix = split_name(names[0])
        for street in self._similar(" ".join(core), suffix, towns, exclude=set(names)):
            if self.book.has(street, n, towns, block):
                candidates.append((STREET_COST, number, street))
        heard_street = text[phrase[0]:phrase[1]]
        return self._pick(text, number_token, phrase, candidates, heard_street, block, street_towns, call_time)

    def _similar_street(self, text, number_token, phrase, number, block, core, suffix, towns, call_time):
        n = int(number)
        candidates = [(STREET_COST, number, name) for name in self._similar(core, suffix, towns)
                      if self.book.has(name, n, towns, block)]
        return self._pick(text, number_token, phrase, candidates, None, block, towns, call_time)

    def _similar(self, core: str, suffix: str, towns: set[str], exclude=()) -> list[str]:
        out = []
        for street in self.gazetteer.streets:
            if street.name in exclude or street.name in out:
                continue
            street_core, street_suffix = split_name(street.name)
            if street_suffix != suffix or (towns and street.town_key not in towns):
                continue
            other = " ".join(street_core)
            if other[:1] != core[:1] or abs(len(other) - len(core)) > 3:
                continue
            if SequenceMatcher(None, core, other).ratio() >= STREET_RATIO:
                out.append(street.name)
        return out

    def _pick(self, text, number_token, phrase, candidates, heard_street, block, towns, call_time) -> Fix | None:
        if not candidates:
            return None
        cheapest = min(c[0] for c in candidates)
        answers = list(dict.fromkeys((number, name) for cost, number, name in candidates if cost == cheapest))
        if len(answers) > 1 and self.near_incidents and call_time is not None:
            incidents = self.near_incidents(call_time) or []
            close = [a for a in answers if self._near(a, block, towns, incidents)]
            answers = close if len(close) == 1 else answers
        if len(answers) != 1:
            return None
        number, name = answers[0]
        start, end = number_token.start(), phrase[1]
        heard = text[start:end]
        # Only the number changed: keep the street as written ("Main St" stays "Main St").
        street = heard_street if heard_street is not None and _name_key(name) == _name_key(heard_street) else name
        middle = text[number_token.end():phrase[0]]
        fixed = f"{number}{middle}{street}"
        return Fix(start, end, heard, fixed) if fixed != heard else None

    def _near(self, answer, block, towns, incidents) -> bool:
        point = self.book.locate(answer[1], int(answer[0]), towns, block)
        return point is not None and any(self.book._distance(*point, lat, lon) <= NEAR_INCIDENT_M
                                         for lat, lon in incidents)


def heard_note(heard: list[str]) -> str | None:
    return "; ".join(f'"{h}"' for h in heard) if heard else None


def main(argv=None):
    from .config import load_config

    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.address_fix",
                                     description="Lists the addresses the autocorrect would fix on the call log.")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--days", type=int, default=30, help="how far back to look (default 30 days)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    config = load_config(args.config if args.config.exists() else None)
    book = AddressBook(config.ranges_path)
    if not book.loaded:
        print(f"No house-number ranges yet ({config.ranges_path}). Run fetch-places.bat first.")
        return 1
    fixer = AddressFixer(Gazetteer(config.geo_path, config.places_path), book)
    conn = sqlite3.connect(f"file:{config.data_path / 'calls.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, call_time, talkgroup_label, talkgroup_group, transcript FROM calls "
                        "WHERE status = 'done' AND transcript != '' AND call_time >= strftime('%s', 'now') - ? "
                        "ORDER BY id", (args.days * 86400,)).fetchall()
    changed = 0
    for row in rows:
        context = " ".join(filter(None, (row["talkgroup_label"], row["talkgroup_group"])))
        fixed, heard = fixer.fix(row["transcript"], context)
        if heard:
            changed += 1
            print(f"Call {row['id']} ({row['talkgroup_label'] or ''}):\n  before: {row['transcript']}\n  after:  {fixed}\n")
    print(f"{changed} of {len(rows)} calls from the last {args.days} days would get an address fixed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
