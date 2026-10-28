"""Local streets, routes and landmarks: Whisper hints and street-name cleanup.

Everything comes from one CSV (places.csv), built by fetch_places.py (fetch-places.bat) from OpenStreetMap and
editable by hand, with columns:
    name          how the place is written, e.g. "Northampton Street", "Route 22", "Easton Area High School"
    kind          street | route | landmark
    municipality  town or township it is in; a street in two towns gets a row for each
    spoken        other ways it is said, separated by |, e.g. "the Circle|Centre Square"
    prompt        yes to make it a candidate for Whisper's hint list
    rank          bigger = more important (road size, length); picks hints when there are too many
    osm_id        OpenStreetMap id, for the map
    source        osm for rows the fetch script made; anything else is kept when the script runs again

The file is re-read whenever it changes, so edits apply without restarting.
"""

import csv
import difflib
import io
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .codes import ONES, TENS, spoken_number

log = logging.getLogger(__name__)

SUFFIXES = {
    "street": "street", "st": "street", "avenue": "avenue", "ave": "avenue", "av": "avenue",
    "road": "road", "rd": "road", "drive": "drive", "dr": "drive", "lane": "lane", "ln": "lane",
    "boulevard": "boulevard", "blvd": "boulevard", "pike": "pike", "highway": "highway", "hwy": "highway",
    "way": "way", "court": "court", "ct": "court", "place": "place", "pl": "place", "circle": "circle",
    "terrace": "terrace", "parkway": "parkway", "pkwy": "parkway", "alley": "alley", "square": "square",
}
SUFFIX_WORD = re.compile(r"\b(" + "|".join(SUFFIXES) + r")\b\.?", re.IGNORECASE)

ORDINAL_ONES = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
}
ORDINAL_TENS = {"twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60,
                "seventieth": 70, "eightieth": 80, "ninetieth": 90}
_ORDINAL = (rf"(?:(?:{'|'.join(TENS)})[\s-])?(?:{'|'.join(ORDINAL_ONES)})|(?:{'|'.join(ORDINAL_TENS)})")
# "twenty second street" -> "22nd Street". Only street and avenue: "the second road" is just an ordinal.
SPOKEN_ORDINAL_STREET = re.compile(rf"\b({_ORDINAL})(\s+)(street|st\b\.?|avenue|ave\b\.?)", re.IGNORECASE)

# "route twenty two", "route two forty eight", "interstate seventy eight". Not "en route twenty minutes".
_ROUTE_NUMBER = rf"(?:\d{{1,3}}|(?:{'|'.join(ONES)})(?:[\s-](?:{'|'.join(TENS)})(?:[\s-](?:{'|'.join(ONES)}))?|[\s-](?:{'|'.join(ONES)}))?|(?:{'|'.join(TENS)})(?:[\s-](?:{'|'.join(ONES)}))?)"
# "exit seventy five", "exit fifty one b". Only for exits in the list, so "the exit two miles up" stays.
SPOKEN_EXIT = re.compile(rf"\bexit\s+({_ROUTE_NUMBER})(?:\s*-?\s*([ab])\b)?\b", re.IGNORECASE)
SPOKEN_ROUTE = re.compile(rf"(?<!\ben )\b(route|interstate)\s+({_ROUTE_NUMBER})\b", re.IGNORECASE)

# Words that end a street name when looking backwards from "Street".
STOP_WORDS = {"the", "on", "at", "to", "of", "and", "in", "from", "for", "by", "near", "off", "a", "an",
              "is", "be", "between", "onto", "into", "with", "en", "route", "block", "or"}
# Words before a street word that make it no name at all ("Same Road", "Main Road").
GENERIC_WORDS = {"this", "that", "same", "your", "my", "his", "her", "their", "our", "back", "dirt", "side",
                 "main", "one", "each", "every", "other", "next", "cross", "private", "wrong", "old", "new",
                 "which", "what", "any", "no", "county", "state", "city", "township"}
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.]*")
MIN_FUZZY_LENGTH = 5
FUZZY_RATIO = 0.8


@dataclass
class Place:
    name: str
    kind: str
    municipality: str
    spoken: list[str]
    prompt: bool
    rank: int


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def spoken_ordinal(text: str) -> int | None:
    parts = text.lower().replace("-", " ").split()
    if len(parts) == 1:
        return ORDINAL_ONES.get(parts[0], ORDINAL_TENS.get(parts[0]))
    if len(parts) == 2 and parts[0] in TENS and parts[1] in ORDINAL_ONES and ORDINAL_ONES[parts[1]] < 10:
        return TENS[parts[0]] + ORDINAL_ONES[parts[1]]
    return None


def route_number(text: str) -> int | None:
    """"22", "twenty two", "two forty eight" (248), "five twelve" (512)."""
    text = text.lower().replace("-", " ").strip()
    if text.isdigit():
        return int(text)
    whole = spoken_number(text)
    if whole is not None:
        return whole
    first, _, rest = text.partition(" ")
    if first in ONES and 1 <= ONES[first] <= 9:
        tail = spoken_number(rest)
        if tail is not None and tail >= 10:
            return ONES[first] * 100 + tail
    return None


def core_name(municipality: str) -> str:
    """"City of Bethlehem" -> "bethlehem", "Palmer Township" -> "palmer"."""
    name = municipality.lower()
    name = re.sub(r"^(city|borough|township|town) of\s+", "", name)
    name = re.sub(r"\s+(township|borough|city|town)$", "", name)
    return name.strip()


def _key(name: str) -> str:
    words = [w.lower().rstrip(".") for w in WORD.findall(name)]
    if words and words[-1] in SUFFIXES:
        words[-1] = SUFFIXES[words[-1]]
    return " ".join(words)


class PlaceBook:
    def __init__(self, path: Path | None, prompt_chars: int = 400, areas: dict | None = None):
        self.path = path
        self.prompt_chars = prompt_chars
        self.areas = {k.lower(): list(v) for k, v in (areas or {}).items()}
        self._lock = threading.Lock()
        self._mtime = None
        self._places: list[Place] = []
        self._by_area: dict[str, list[Place]] = {}
        self._streets: dict[str, dict[str, Place]] = {}
        self._spoken: list[tuple[re.Pattern, str]] = []
        self._exits: set[str] = set()
        self.reload_if_changed()

    def reload_if_changed(self):
        try:
            mtime = self.path.stat().st_mtime if self.path else None
        except FileNotFoundError:
            mtime = None
        with self._lock:
            if mtime == self._mtime:
                return
            self._mtime = mtime
            self._load()

    def _load(self):
        places, by_area, streets, spoken, exits = [], {}, {}, [], set()
        for row in self._read_rows():
            name = (row.get("name") or "").strip()
            if not name:
                continue
            try:
                rank = int(float((row.get("rank") or "0").strip() or 0))
            except ValueError:
                rank = 0
            place = Place(
                name=name,
                kind=(row.get("kind") or "street").strip().lower() or "street",
                municipality=(row.get("municipality") or "").strip(),
                spoken=[s.strip() for s in (row.get("spoken") or "").split("|") if s.strip()],
                prompt=(row.get("prompt") or "").strip().lower() in ("yes", "y", "true", "1"),
                rank=rank,
            )
            places.append(place)
            if re.fullmatch(r"Exit \d+[A-Z]?", name):
                exits.add(name)
            by_area.setdefault(place.municipality.lower(), []).append(place)
            key = _key(name)
            suffix = key.rsplit(" ", 1)[-1]
            if place.kind == "street" and suffix in SUFFIXES.values():
                current = streets.setdefault(suffix, {}).get(key)
                if current is None or place.rank > current.rank:
                    streets[suffix][key] = place
            for alias in place.spoken:
                spoken.append((re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE), name))

        for area in by_area.values():
            area.sort(key=lambda p: -p.rank)
        # Longest aliases first so "the Easton Circle" wins over "the Circle".
        spoken.sort(key=lambda pair: -len(pair[0].pattern))
        self._places, self._by_area, self._streets, self._spoken = places, by_area, streets, spoken
        self._exits = exits
        log.info("Loaded %d places in %d municipalities from %s", len(places), len(by_area), self.path)

    def _read_rows(self) -> list[dict]:
        if self._mtime is None:
            return []
        try:
            raw = self.path.read_bytes()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("cp1252", errors="replace")  # Excel's "CSV" on Windows
            return list(csv.DictReader(io.StringIO(content, newline="")))
        except (OSError, csv.Error) as e:
            log.error("Could not read %s: %s", self.path, e)
            return []

    def areas_for(self, *context: str | None) -> list[str]:
        """Municipalities a talkgroup covers: from [places] areas in config.toml when the talkgroup
        name contains one of its keys, else any municipality named in the talkgroup name. "Bethlehem"
        in a talkgroup name matches both Bethlehem and Bethlehem Township."""
        text = " ".join(c for c in context if c).lower()
        if not text:
            return []
        for key, wanted in self.areas.items():
            if key in text:
                found = []
                for name in wanted:
                    name = name.lower()
                    found += [name] if name in self._by_area else \
                        [area for area in self._by_area if area and core_name(area) == core_name(name)]
                return found
        return [area for area in self._by_area
                if area and re.search(rf"\b{re.escape(core_name(area))}\b", text)]

    def prompt_terms(self, *context: str | None) -> list[str]:
        """Hint terms for one call, least important first (build_prompt drops from the front).
        Places in the talkgroup's own area come first in priority, then the biggest roads overall."""
        self.reload_if_changed()
        local = [p for area in self.areas_for(*context) for p in self._by_area.get(area, []) if p.prompt]
        everywhere = sorted((p for p in self._places if p.prompt), key=lambda p: -p.rank)
        picked, seen, used = [], set(), 0
        for place in sorted(local, key=lambda p: -p.rank) + everywhere:
            if place.name.lower() in seen:
                continue
            if used + len(place.name) + 2 > self.prompt_chars:
                break
            seen.add(place.name.lower())
            picked.append(place.name)
            used += len(place.name) + 2
        return list(reversed(picked))

    def normalize(self, text: str, *context: str | None) -> str:
        """Writes spoken street numbers and routes the way dispatch writes them, swaps in the written
        name for spoken aliases, and fixes near-miss spellings of streets in the list."""
        if not text:
            return text
        self.reload_if_changed()
        text = SPOKEN_ORDINAL_STREET.sub(self._ordinal_street, text)
        text = SPOKEN_ROUTE.sub(_route, text)
        if self._exits:
            text = SPOKEN_EXIT.sub(self._exit, text)
        for pattern, name in self._spoken:
            text = pattern.sub(name, text)
        if self._streets:
            text = self._fix_streets(text, set(self.areas_for(*context)))
        return text

    def unknown_streets(self, text: str) -> list[str]:
        """Street names in a finished transcript that aren't in the list, such as "Northbampton Street",
        for the self-check. Only capitalized names ending in a capitalized street word, so "on the way"
        and "we'll drive" don't count."""
        if not text:
            return []
        self.reload_if_changed()
        if not self._streets:
            return []
        found = []
        for match in SUFFIX_WORD.finditer(text):
            if not match.group(1)[0].isupper():
                continue
            suffix = SUFFIXES[match.group(1).lower()]
            words = _words_before(text, match.start(), 0)
            while words and not words[0][1][0].isupper():
                words = words[1:]
            if not words or words[-1][1].lower() in GENERIC_WORDS:
                continue
            names = self._streets.get(suffix, {})
            stems = [" ".join(w.lower().rstrip(".") for _, w in words[-n:]) for n in range(1, len(words) + 1)]
            if not any(f"{stem} {suffix}" in names for stem in stems):
                found.append(text[words[0][0]:match.end()].rstrip("."))
        return found

    def _exit(self, match):
        n = route_number(match.group(1))
        name = f"Exit {n}{(match.group(2) or '').upper()}" if n else None
        return name if name in self._exits else match.group(0)

    @staticmethod
    def _ordinal_street(match):
        n = spoken_ordinal(match.group(1))
        if n is None:
            return match.group(0)
        word = match.group(3)
        return f"{ordinal(n)}{match.group(2)}{word[0].upper()}{word[1:]}"

    def _fix_streets(self, text: str, areas: set[str]) -> str:
        out, last, previous = [], 0, 0
        for match in SUFFIX_WORD.finditer(text):
            suffix = SUFFIXES[match.group(1).lower()]
            names = self._streets.get(suffix)
            floor, previous = previous, match.end()
            if not names:
                continue
            words = _words_before(text, match.start(), floor)
            replacement = self._best(words, suffix, names, areas)
            if replacement is None:
                continue
            start, name = replacement
            out.append(text[last:start])
            out.append(name)
            last = match.end()
        out.append(text[last:])
        return "".join(out)

    @staticmethod
    def _best(words, suffix, names, areas):
        """words: (start, word) pairs right before the street word, nearest last."""
        best = None
        for count in range(1, min(3, len(words)) + 1):
            window = words[-count:]
            stem = " ".join(w.lower().rstrip(".") for _, w in window)
            key = f"{stem} {suffix}"
            if key in names:
                return None  # already spelled like a street in the list
            if len(stem.replace(" ", "")) < MIN_FUZZY_LENGTH:
                continue
            for name_key, place in names.items():
                name_stem = name_key[:-len(suffix) - 1]
                if abs(len(name_stem) - len(stem)) > 3 or name_stem[:1] != stem[:1]:
                    continue
                ratio = difflib.SequenceMatcher(None, stem, name_stem).ratio()
                if ratio < FUZZY_RATIO:
                    continue
                score = (ratio, place.municipality.lower() in areas, place.rank)
                if best is None or score > best[0]:
                    best = (score, window[0][0], place.name)
        return (best[1], best[2]) if best else None


def _words_before(text: str, end: int, floor: int) -> list[tuple[int, str]]:
    """Up to three words before a street word, stopping at punctuation, numbers and small words."""
    words = list(WORD.finditer(text, floor, end))
    picked = []
    position = end
    for word in reversed(words):
        between = text[word.end():position]
        if between.strip() or word.group(0).lower() in STOP_WORDS or word.group(0)[0].isdigit():
            break
        picked.append((word.start(), word.group(0)))
        position = word.start()
        if len(picked) == 3:
            break
    return list(reversed(picked))


def _route(match):
    n = route_number(match.group(2))
    if n is None or n == 0:
        return match.group(0)
    if match.group(1).lower() == "interstate":
        return f"I-{n}"
    return f"Route {n}"
