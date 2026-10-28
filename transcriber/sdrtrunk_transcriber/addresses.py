"""Finding addresses, intersections and landmarks in transcripts, and where they are.

Reads the two files fetch_places.py builds from OpenStreetMap:
    places-geo.json  road lines, landmark points and town names; needed to place intersections
    places.csv       the hand-editable name list (see places.py); adds spoken forms, hand-made streets
                     and landmarks

Matching works on normalized words, so "N. 3rd St", "North Third Street" and "north 3rd st" are the same.
Both files are re-read when they change.
"""

import csv
import io
import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .codes import ONES, TENS
from .fetch_places import route_names

log = logging.getLogger(__name__)

# Spoken and written street words, folded to one form. "st" stands for both Street and Saint, which
# never matters because a street's core name and its suffix are matched separately.
WORDS = {
    "street": "st", "saint": "st", "str": "st",
    "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln", "boulevard": "blvd",
    "court": "ct", "place": "pl", "turnpike": "tpke", "highway": "hwy", "parkway": "pkwy",
    "circle": "cir", "terrace": "ter", "alley": "aly", "trail": "trl", "square": "sq",
    "mount": "mt", "fort": "ft", "north": "n", "south": "s", "east": "e", "west": "w",
    "township": "twp", "borough": "boro", "extension": "ext",
}
SUFFIXES = {"st", "ave", "rd", "dr", "ln", "blvd", "ct", "pl", "tpke", "hwy", "pkwy", "cir", "ter", "aly",
            "way", "trl", "sq", "pike", "row", "run", "pass", "path", "loop", "ext", "bypass", "plz", "plaza"}
DIRECTIONS = {"n", "s", "e", "w"}
ROUTE_WORDS = {"route", "rt", "rte", "sr", "us", "pa", "nj", "interstate", "i", "highway", "hwy"}
TOWN_TYPES = {"twp": "twp", "boro": "", "city": ""}

ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50,
}

# Words between two street names that make them an intersection: "Main and 3rd", "Route 22 at Oak",
# "Main Street between 3rd and 4th" (Main & 3rd).
JOINERS = {"and", "at", "near", "by", "off", "of", "with", "x", "the", "on", "between"}
UNIT_WORDS = {"engine", "medic", "ladder", "tanker", "truck", "rescue", "squad", "ems", "unit", "car", "chief",
              "station", "ambulance", "qrs", "qrv", "brush", "tower", "utility", "box", "number", "battalion",
              "deputy", "assistant", "officer", "badge", "sector", "zone", "company", "tac", "channel", "ops"}
TRANSPORT_WORDS = {"transport", "transporting", "transported", "transports", "enroute", "arriving", "arrived",
                   "admitted", "releasing", "clear"}
NUMBER = re.compile(r"^\d{1,5}[a-z]?$")

# Points along each street, this far apart, for finding where two streets meet.
STEP_M = 12.0
# Two streets closer than this meet at an intersection.
MEET_M = 20.0
CELL_M = 30.0


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def words(text: str) -> list[str]:
    """Normalized words: lowercase, no punctuation, street words folded, ordinals as "22nd",
    route numbers as "route 22"."""
    text = text.lower().replace("&", " and ").replace("'", "").replace("’", "")
    # "10-4" and "20-24" stay one word, so a ten-code or a time is never read as a house number.
    raw = re.findall(r"\d+(?:-\d+)+|[a-z0-9]+", text)
    out: list[str] = []
    i = 0
    while i < len(raw):
        w = raw[i]
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        # "twenty second" -> 22nd, "second" -> 2nd
        if w in TENS and nxt in ORDINAL_WORDS and ORDINAL_WORDS[nxt] < 10:
            out.append(ordinal(TENS[w] + ORDINAL_WORDS[nxt]))
            i += 2
            continue
        if w in ORDINAL_WORDS:
            out.append(ordinal(ORDINAL_WORDS[w]))
            i += 1
            continue
        # "3 rd", "22 nd" (not "22 st", which could be 22 Street)
        if w.isdigit() and len(w) <= 3 and nxt in ("nd", "rd", "th") and ordinal(int(w)).endswith(nxt):
            out.append(ordinal(int(w)))
            i += 2
            continue
        # "Route 22", "Rt. 248", "US 22", "PA 33", "I-78", "Interstate 78", "Route twenty two"
        m = re.fullmatch(r"(i|us|pa|sr|rt|rte)(\d{1,3})", w)
        if m:
            out += ["route", m.group(2)]
            i += 1
            continue
        if w in ROUTE_WORDS and nxt:
            spelled = w in ("route", "rt", "rte", "sr", "interstate")
            number, used = (nxt, 2) if nxt.isdigit() else (None, 2)
            if number is None and spelled and nxt in TENS:
                after = raw[i + 2] if i + 2 < len(raw) else ""
                if after in ONES and ONES[after] < 10:
                    number, used = str(TENS[nxt] + ONES[after]), 3
                else:
                    number = str(TENS[nxt])
            elif number is None and spelled and nxt in ONES:
                number = str(ONES[nxt])
            if number and len(number) <= 3:
                out += ["route", number]
                i += used
                continue
        m = re.fullmatch(r"(\d{1,3})(st|nd|rd|th)", w)
        if m:
            out.append(ordinal(int(m.group(1))))
        else:
            out.append(WORDS.get(w, w))
        i += 1
    return out


def town_key(name: str) -> str:
    """"Palmer Twp", "Palmer Township" -> "palmer twp"; "City of Easton", "Easton" -> "easton";
    "Nazareth Borough" -> "nazareth"."""
    w = words(name)
    if w[:2] == ["city", "of"]:
        w = w[2:]
    township = bool(w) and w[-1] == "twp"
    while w and w[-1] in TOWN_TYPES:
        w = w[:-1]
    return " ".join(w + (["twp"] if township else []))


def split_name(name: str) -> tuple[tuple[str, ...], str]:
    """Street name -> (core words, suffix). "North 3rd Street" -> (("n", "3rd"), "st")."""
    w = words(name)
    if len(w) > 1 and w[-1] in SUFFIXES:
        return tuple(w[:-1]), w[-1]
    return tuple(w), ""


@dataclass(eq=False)
class Street:
    name: str
    town: str
    road_class: str = ""
    lines: list = field(default_factory=list, repr=False)  # [[(lat, lon), ...], ...]
    _points: list | None = field(default=None, repr=False)
    _cells: dict | None = field(default=None, repr=False)

    @property
    def town_key(self) -> str:
        return town_key(self.town) if self.town else ""


@dataclass(eq=False)
class Landmark:
    name: str
    town: str
    lat: float | None = None
    lon: float | None = None


@dataclass
class Mention:
    start: int
    end: int
    streets: list
    strong: bool          # had a suffix ("Street") or is a numbered route
    text: str


@dataclass
class Found:
    kind: str             # address, intersection, landmark
    heard: str            # the words as transcribed
    label: str            # "1234 Northampton St, Easton"
    query: str | None = None          # free-text search for the online lookup
    streets: list = field(default_factory=list)
    points: list = field(default_factory=list)   # [(lat, lon)] candidates already known offline
    towns: set = field(default_factory=set)
    number: str | None = None         # the house number of an address
    block: bool = False               # "1200 block of"


class Projection:
    """Flat x/y metres around the area's middle; plenty accurate across one or two counties."""

    def __init__(self, lat0: float = 40.65):
        self.kx = 111_320 * math.cos(math.radians(lat0))
        self.ky = 110_574

    def xy(self, lat, lon):
        return lon * self.kx, lat * self.ky

    def latlon(self, x, y):
        return y / self.ky, x / self.kx


class Gazetteer:
    def __init__(self, geo_path: Path | None, places_path: Path | None):
        self.geo_path = geo_path
        self.places_path = places_path
        self._stamp = None
        self._lock = threading.Lock()
        self.proj = Projection()
        self.streets: list[Street] = []
        self.by_core: dict[tuple, list[Street]] = {}
        self.towns: dict[tuple, set[str]] = {}       # spoken words -> town keys
        self.town_names: dict[str, str] = {}         # town key -> display name
        self.landmarks: dict[tuple, list[Landmark]] = {}
        self.has_shapes = False
        self.reload_if_changed()

    # ---- loading

    def _file_stamp(self):
        stamp = []
        for path in (self.geo_path, self.places_path):
            try:
                stat = path.stat() if path else None
                stamp.append((stat.st_mtime_ns, stat.st_size) if stat else None)
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def reload_if_changed(self) -> bool:
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return False
        with self._lock:
            self._stamp = stamp
            try:
                self._load()
            except Exception:
                log.exception("Could not read the street lists")
        return True

    @property
    def loaded(self) -> bool:
        return bool(self.streets)

    def _load(self):
        streets: dict[tuple[str, str], Street] = {}
        aliases: list[tuple[str, str, str]] = []
        town_names: dict[str, str] = {}
        landmarks: dict[tuple, list[Landmark]] = {}
        has_shapes = False

        if self.geo_path and self.geo_path.exists():
            data = json.loads(self.geo_path.read_text(encoding="utf-8"))
            lats = [p[0] for way in data.get("ways", [])[:2000] for p in way.get("geometry", [])[:1]]
            if lats:
                self.proj = Projection(sum(lats) / len(lats))
            for way in data.get("ways", []):
                line = [tuple(p) for p in way.get("geometry") or []]
                if len(line) < 2:
                    continue
                towns = way.get("municipalities") or [""]
                names = [(way["name"], towns)] if way.get("name") else []
                # A numbered route is one road, whatever town it passes through.
                names += [(name, [""]) for name in route_names(way.get("ref") or "")]
                for name, name_towns in names:
                    for town in name_towns:
                        key = (_name_key(name), town_key(town))
                        street = streets.get(key)
                        if street is None:
                            street = streets[key] = Street(name, town, way.get("highway") or "")
                        street.lines.append(line)
                        has_shapes = True
            for mark in data.get("landmarks", []):
                if mark.get("name") and mark.get("lat") is not None:
                    point = Landmark(mark["name"], mark.get("municipality") or "", mark["lat"], mark["lon"])
                    landmarks.setdefault(tuple(words(point.name)), []).append(point)
            for town in data.get("municipalities", []):
                if town.get("name"):
                    town_names.setdefault(town_key(town["name"]), town["name"])

        for row in self._read_places():
            name = (row.get("name") or "").strip()
            kind = (row.get("kind") or "street").strip().lower()
            town = (row.get("municipality") or "").strip()
            if not name:
                continue
            if town:
                town_names.setdefault(town_key(town), town)
            spoken = [s.strip() for s in (row.get("spoken") or "").split("|") if s.strip()]
            if kind == "landmark":
                known = [m for m in landmarks.get(tuple(words(name)), []) if town_key(m.town) == town_key(town)]
                mark = known[0] if known else Landmark(name, town)
                for form in [name, *spoken]:
                    key = tuple(words(form))
                    if key and mark not in landmarks.get(key, []):
                        landmarks.setdefault(key, []).append(mark)
                continue
            if kind == "route":
                town = ""
            elif kind != "street":
                continue
            key = (_name_key(name), town_key(town))
            if key not in streets:
                # Same street spelled differently ("Northampton St" vs "Northampton Street") or
                # listed without a town: reuse the downloaded one.
                streets[key] = Street(name, town)
            for form in spoken:
                aliases.append((form, key[0], key[1]))

        by_core: dict[tuple, list[Street]] = {}
        for street in streets.values():
            core, _ = split_name(street.name)
            for key in _core_keys(core):
                by_core.setdefault(key, []).append(street)
        by_name: dict[str, list[Street]] = {}
        for street in streets.values():
            by_name.setdefault(_name_key(street.name), []).append(street)
        for form, name_key, town in aliases:
            core, _ = split_name(form)
            matches = [s for s in by_name.get(name_key, []) if not town or s.town_key == town]
            for street in matches:
                group = by_core.setdefault(core, [])
                if street not in group:
                    group.append(street)

        towns: dict[tuple, set[str]] = {}
        for key, name in town_names.items():
            w = tuple(key.split())
            towns.setdefault(w, set()).add(key)
            if w and w[-1] == "twp":
                towns.setdefault(w[:-1] + ("township",), set()).add(key)
                # "in Palmer" for Palmer Township; "in Bethlehem" still means the city (see towns_in).
                towns.setdefault(w[:-1], set()).add(key)

        self.streets = list(streets.values())
        self.by_core = by_core
        self.towns = towns
        self.town_names = town_names
        self.landmarks = landmarks
        self.has_shapes = has_shapes
        log.info("Street list: %d streets in %d towns (%s), %d landmarks", len(self.streets), len(town_names),
                 "with shapes" if has_shapes else "names only", sum(len(v) for v in landmarks.values()))

    def _read_places(self) -> list[dict]:
        if not self.places_path or not self.places_path.exists():
            return []
        raw = self.places_path.read_bytes().decode("utf-8-sig", errors="replace")
        return [{(k or "").strip().lower(): v for k, v in row.items()} for row in csv.DictReader(io.StringIO(raw))]

    # ---- finding places in text

    def find(self, text: str, context: str | None = None) -> Found | None:
        """The best address, intersection or landmark in a transcript, or None. `context` (the
        talkgroup name) can name the town."""
        self.reload_if_changed()
        if not text:
            return None
        w = words(text)
        towns = self.towns_in(w)
        if not towns and context:
            towns = self._towns_anywhere(words(context))
        mentions = self._mentions(w)

        address = self._address(w, mentions, towns)
        if address:
            return address
        crossing = self._intersection(w, mentions, towns)
        if crossing:
            return crossing
        return self._landmark(w, towns)

    def towns_in(self, w: list[str]) -> set[str]:
        """Towns named after "in" or with Township/Borough/City: "in Easton", "Palmer Township"."""
        found = set()
        for i in range(len(w)):
            for length in range(min(4, len(w) - i), 0, -1):
                key = tuple(w[i:i + length])
                if key not in self.towns:
                    continue
                before = w[i - 1] if i else ""
                after = w[i + length] if i + length < len(w) else ""
                if key[-1] in ("twp", "township"):
                    found |= self.towns[key]
                elif after in ("twp", "township"):
                    found |= self.towns.get(key + ("twp",), set())
                elif before == "in" or after in ("boro", "city"):
                    # "in Bethlehem" means the city; "in Bethlehem Township" was caught above.
                    keys = self.towns[key]
                    found |= {k for k in keys if not k.endswith(" twp")} or keys
                else:
                    continue
                break
        return found

    def _towns_anywhere(self, w: list[str]) -> set[str]:
        found = set()
        for i in range(len(w)):
            for length in range(min(4, len(w) - i), 0, -1):
                key = tuple(w[i:i + length])
                if key in self.towns:
                    found |= {k for k in self.towns[key] if not k.endswith(" twp")} or self.towns[key]
                    break
        return found

    def _mentions(self, w: list[str]) -> list[Mention]:
        out = []
        i = 0
        while i < len(w):
            hit = None
            for length in range(min(6, len(w) - i), 0, -1):
                core = tuple(w[i:i + length])
                streets = self.by_core.get(core)
                if not streets:
                    continue
                end = i + length
                suffix = w[end] if end < len(w) and w[end] in SUFFIXES else ""
                strong = core[0] == "route"
                if suffix:
                    same = [s for s in streets if split_name(s.name)[1] == suffix]
                    if same:
                        streets, end, strong = same, end + 1, True
                    elif not all(t.isdigit() for t in core):
                        # "Main Rd" when only "Main St" exists: still Main, but say so with the suffix.
                        end += 1
                if not strong and len(core) == 1 and (core[0] in JOINERS or core[0] in DIRECTIONS):
                    continue
                hit = Mention(i, end, streets, strong, " ".join(w[i:end]))
                break
            if hit:
                out.append(hit)
                i = hit.end
            else:
                i += 1
        return out

    def _address(self, w, mentions, towns) -> Found | None:
        for m in mentions:
            if not m.strong:
                continue
            j = m.start
            # "1234 Main St", "1200 block of Main St", "12 hundred block of Main"
            if j >= 3 and w[j - 2:j] == ["block", "of"]:
                j -= 2
                if w[j - 1] == "hundred" and j >= 2 and w[j - 2].isdigit():
                    number = str(int(w[j - 2]) * 100)
                else:
                    number = w[j - 1] if NUMBER.match(w[j - 1]) else None
                block = True
            else:
                number = w[j - 1] if j >= 1 and NUMBER.match(w[j - 1]) else None
                block = False
            if not number or (number.isdigit() and int(number) == 0):
                continue
            # "Engine 4 Route 22", "Medic 2081 Main St": a unit number, not a house number.
            unit_at = j - (3 if block and w[j - 1] == "hundred" else 2)
            if unit_at >= 0 and w[unit_at] in UNIT_WORDS:
                continue
            if m.streets[0].name and split_name(m.streets[0].name)[0][:1] == ("route",) and len(number) < 3:
                continue
            streets = _in_towns(m.streets, towns)
            street = streets[0]
            town_names = {s.town for s in streets if s.town}
            town = next(iter(town_names)) if len(town_names) == 1 else ""
            label = f"{number} {'block of ' if block else ''}{street.name}" + (f", {town}" if town else "")
            heard_from = j - 1 - (1 if block and w[j - 1] == "hundred" else 0)
            query = f"{number} {street.name}, {town + ', ' if town else ''}Pennsylvania"
            return Found("address", " ".join(w[max(0, heard_from):m.end]), label, query, streets,
                         towns={s.town_key for s in streets}, number=number, block=block)
        return None

    def _intersection(self, w, mentions, towns) -> Found | None:
        for a, b in zip(mentions, mentions[1:]):
            gap = w[a.end:b.start]
            if not gap or len(gap) > 2 or any(t not in JOINERS for t in gap):
                continue
            if not {"and", "at", "near", "x", "between"} & set(gap):
                continue
            points, pairs = self.meeting_points(a.streets, b.streets, towns)
            shapeless = not any(s.lines for s in a.streets) or not any(s.lines for s in b.streets)
            if not points and shapeless and a.strong and b.strong:
                # A street with no downloaded shape: let the online lookup try.
                sa, sb = _in_towns(a.streets, towns)[0], _in_towns(b.streets, towns)[0]
                town = sa.town if sa.town and sa.town == sb.town else ""
                label = f"{sa.name} & {sb.name}" + (f", {town}" if town else "")
                return Found("intersection", " ".join(w[a.start:b.end]), label,
                             f"{sa.name} & {sb.name}, {town + ', ' if town else ''}Pennsylvania", [sa, sb])
            if not points:
                continue
            sa, sb = pairs[0]
            town_list = sorted({s.town for pair in pairs for s in pair if s.town})
            town = town_list[0] if len(town_list) == 1 else (" / ".join(town_list[:2]) if town_list else "")
            label = f"{sa.name} & {sb.name}" + (f", {town}" if town else "")
            return Found("intersection", " ".join(w[a.start:b.end]), label, None, [sa, sb], points,
                         {s.town_key for pair in pairs for s in pair})
        return None

    def _landmark(self, w, towns) -> Found | None:
        # "Medic 49 transporting one to Easton Hospital" is where the patient goes, not the incident.
        if TRANSPORT_WORDS & set(w):
            return None
        for i in range(len(w)):
            # One-word names ("Target", "Wawa", a church called "Grace") are too often ordinary words.
            for length in range(min(6, len(w) - i), 1, -1):
                marks = self.landmarks.get(tuple(w[i:i + length]))
                if not marks:
                    continue
                if towns:
                    marks = [m for m in marks if town_key(m.town) in towns] or marks
                mark = marks[0]
                label = mark.name + (f", {mark.town}" if mark.town else "")
                points = [(mark.lat, mark.lon)] if mark.lat is not None and mark.lon is not None else []
                query = None if points else f"{mark.name}, {mark.town + ', ' if mark.town else ''}Pennsylvania"
                return Found("landmark", " ".join(w[i:i + length]), label, query, [], points)
        return None

    # ---- geometry

    def meeting_points(self, first: list[Street], second: list[Street], towns: set[str]):
        """Where any of `first` meets any of `second`. Returns (points, street pairs); points
        are grouped so one crossing counts once even when the streets run through two towns.
        Several far-apart crossings (Main St & 3rd St in three towns) are narrowed by `towns`,
        and stay ambiguous (more than one point) when that doesn't settle it."""
        hits = []
        for a in first:
            for b in second:
                if a is b or not a.lines or not b.lines:
                    continue
                for point in self._meet(a, b):
                    hits.append((point, a, b))
        if not hits:
            return [], []
        if towns:
            local = [h for h in hits if h[1].town_key in towns or h[2].town_key in towns]
            hits = local or hits
        groups: list[list] = []
        for hit in hits:
            x, y = self.proj.xy(*hit[0])
            for group in groups:
                gx, gy = self.proj.xy(*group[0][0])
                if math.hypot(x - gx, y - gy) < 150:
                    group.append(hit)
                    break
            else:
                groups.append([hit])
        points = [(sum(h[0][0] for h in g) / len(g), sum(h[0][1] for h in g) / len(g)) for g in groups]
        pairs = [(h[1], h[2]) for h in groups[0]] if len(groups) == 1 else [(g[0][1], g[0][2]) for g in groups]
        return points, pairs

    def _meet(self, a: Street, b: Street) -> list[tuple[float, float]]:
        pa, cells = self._points(a), self._cells(b)
        pb = self._points(b)
        close = []
        for x, y in pa:
            cx, cy = int(x // CELL_M), int(y // CELL_M)
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for k in cells.get((cx + dx, cy + dy), ()):
                        d = math.hypot(pb[k][0] - x, pb[k][1] - y)
                        if d < MEET_M and (best is None or d < best[0]):
                            best = (d, (x + pb[k][0]) / 2, (y + pb[k][1]) / 2)
            if best:
                close.append(best)
        # One point per crossing: keep the closest point of each run that lies within 100 m.
        out: list[tuple[float, float, float]] = []
        for d, x, y in close:
            for n, (od, ox, oy) in enumerate(out):
                if math.hypot(x - ox, y - oy) < 100:
                    if d < od:
                        out[n] = (d, x, y)
                    break
            else:
                out.append((d, x, y))
        return [self.proj.latlon(x, y) for _, x, y in out]

    def _points(self, street: Street) -> list[tuple[float, float]]:
        if street._points is None:
            points = []
            for line in street.lines:
                xy = [self.proj.xy(lat, lon) for lat, lon in line]
                for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
                    steps = max(1, int(math.hypot(x2 - x1, y2 - y1) // STEP_M))
                    points += [(x1 + (x2 - x1) * s / steps, y1 + (y2 - y1) * s / steps) for s in range(steps)]
                if xy:
                    points.append(xy[-1])
            street._points = points
        return street._points

    def _cells(self, street: Street) -> dict:
        if street._cells is None:
            cells: dict[tuple[int, int], list[int]] = {}
            for k, (x, y) in enumerate(self._points(street)):
                cells.setdefault((int(x // CELL_M), int(y // CELL_M)), []).append(k)
            street._cells = cells
        return street._cells

    def points(self, street: Street) -> list[tuple[float, float]]:
        """Points every few metres along a street, as flat x/y metres (see self.proj)."""
        return self._points(street)

    def mentions(self, w: list[str]) -> list[Mention]:
        """Street names in these words."""
        return self._mentions(w)

    def distance_to(self, streets: list[Street], lat: float, lon: float) -> float | None:
        """Metres from a point to the nearest of these streets, None without shapes."""
        x, y = self.proj.xy(lat, lon)
        best = None
        for street in streets:
            for px, py in self._points(street):
                d = math.hypot(px - x, py - y)
                if best is None or d < best:
                    best = d
        return best

    def middle(self, street: Street) -> tuple[float, float] | None:
        """A point halfway along a street, and its length in metres."""
        points = self._points(street)
        if not points:
            return None
        return self.proj.latlon(*points[len(points) // 2])

    def length_m(self, street: Street) -> float:
        return len(self._points(street)) * STEP_M


def _name_key(name: str) -> str:
    core, suffix = split_name(name)
    return " ".join(core + ((suffix,) if suffix else ()))


def _core_keys(core: tuple[str, ...]) -> list[tuple[str, ...]]:
    """A street can be called by its full core ("n 3rd") or without the direction ("3rd")."""
    keys = [core]
    if len(core) > 1 and core[0] in DIRECTIONS:
        keys.append(core[1:])
    return keys


def _in_towns(streets: list[Street], towns: set[str]) -> list[Street]:
    if towns:
        local = [s for s in streets if s.town_key in towns]
        if local:
            return local
    return streets

