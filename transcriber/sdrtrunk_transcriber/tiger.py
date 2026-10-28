"""House-number ranges for every street, from the US Census Bureau's free TIGER/Line address features.

    python -m sdrtrunk_transcriber.fetch_places --addresses-only     # uses the places-geo.json already there

Each Census street piece says which house numbers are on its left and right side ("Main St, 100 to 198 and
101 to 199"). fetch_places.py downloads them for Northampton and Lehigh counties, matches each piece to the
OpenStreetMap road it lies on (so the names are the same ones places.csv uses), works out the town, and writes:

    address-ranges.json   {"streets": [{"name", "town", "segments": [[left from, left to, right from,
                          right to, [[lat, lon], ...]], ...]}, ...]}

address_fix.py reads it to check heard house numbers and to put map pins on the right part of a street.
"""

import io
import json
import logging
import math
import re
import struct
import time
import urllib.error
import urllib.request
import zipfile
from difflib import SequenceMatcher
from pathlib import Path

from .addresses import Projection, _name_key

log = logging.getLogger(__name__)

# Counties by FIPS code. Northampton and Lehigh, PA cover Easton to Allentown.
COUNTIES = {"42095": "Northampton County", "42077": "Lehigh County"}
# Newest first: the Census publishes a year's files the following autumn.
YEARS = (2025, 2024, 2023)
URL = "https://www2.census.gov/geo/tiger/TIGER{year}/ADDRFEAT/tl_{year}_{county}_addrfeat.zip"
USER_AGENT = "sdrtrunk-transcriber/1 (address ranges for a scanner)"
# The files change once a year; a download is reused for this long.
CACHE_SECONDS = 60 * 24 * 3600
# A Census street piece belongs to the OpenStreetMap road within this distance of it.
MATCH_M = 25.0
GRID_M = 30.0
STEP_M = 12.0
NAME_RATIO = 0.7

STATE_ROUTE = re.compile(r"^(?:State|US|PA|County|Co)\s+(?:Hwy|Highway|Rte|Route|Rt|Rd|Road)\s+(\d{1,3})\b.*$",
                         re.IGNORECASE)
INTERSTATE = re.compile(r"^I-?\s*(\d{1,3})\b.*$", re.IGNORECASE)


class CensusError(Exception):
    pass


def download(county: str, cache_dir: Path, years=YEARS) -> Path:
    """The county's address features zip, from the cache when it is recent."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(cache_dir.glob(f"tl_*_{county}_addrfeat.zip"), reverse=True):
        if time.time() - path.stat().st_mtime < CACHE_SECONDS and zipfile.is_zipfile(path):
            log.info("  using the copy downloaded on %s", time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime)))
            return path
    last_error = None
    for year in years:
        url = URL.format(year=year, county=county)
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=300) as response:
                    data = response.read()
                if not zipfile.is_zipfile(io.BytesIO(data)):
                    raise CensusError("the download isn't a zip file")
                path = cache_dir / f"tl_{year}_{county}_addrfeat.zip"
                path.write_bytes(data)
                return path
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code == 404:
                    break          # that year isn't out yet: try the one before
            except (OSError, CensusError) as e:
                last_error = f"{type(e).__name__}: {str(e)[:200]}"
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    raise CensusError(f"Couldn't download the Census address ranges for county {county} ({last_error}).")


# ---- reading the shapefile (only the parts the Census files use: polylines and a dBASE table)

def read_dbf(data: bytes) -> list[dict]:
    count, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields = []
    pos = 32
    while pos < header_len - 1 and data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii", "replace")
        fields.append((name, data[pos + 16]))
        pos += 32
    rows = []
    for n in range(count):
        start = header_len + n * record_len
        record = data[start:start + record_len]
        if len(record) < record_len:
            break
        if record[:1] == b"*":
            continue        # deleted
        row, offset = {}, 1
        for name, length in fields:
            row[name] = record[offset:offset + length].decode("utf-8", "replace").strip()
            offset += length
        rows.append(row)
    return rows


def read_shp(data: bytes) -> list[list[tuple[float, float]]]:
    """Each record's points as (lat, lon), parts joined in order."""
    shapes = []
    pos = 100
    while pos + 8 <= len(data):
        _number, words = struct.unpack(">ii", data[pos:pos + 8])
        content = data[pos + 8:pos + 8 + words * 2]
        pos += 8 + words * 2
        if len(content) < 4:
            break
        shape_type = struct.unpack("<i", content[:4])[0]
        if shape_type == 0:
            shapes.append([])
            continue
        parts, points = struct.unpack("<ii", content[36:44])
        start = 44 + 4 * parts
        xy = struct.unpack(f"<{2 * points}d", content[start:start + 16 * points])
        shapes.append([(xy[k + 1], xy[k]) for k in range(0, len(xy), 2)])
    return shapes


def read_zip(path: Path) -> list[tuple[dict, list[tuple[float, float]]]]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        dbf = next(n for n in names if n.lower().endswith(".dbf"))
        shp = next(n for n in names if n.lower().endswith(".shp"))
        rows, shapes = read_dbf(z.read(dbf)), read_shp(z.read(shp))
    return list(zip(rows, shapes))


# ---- matching Census pieces to OpenStreetMap roads

def clean_name(name: str) -> str:
    """Census spellings that differ from OpenStreetMap's: "State Rte 248" -> "Route 248", "I- 78" -> "I-78"."""
    name = (name or "").strip()
    m = STATE_ROUTE.match(name)
    if m:
        return f"Route {int(m.group(1))}"
    m = INTERSTATE.match(name)
    if m:
        return f"I-{int(m.group(1))}"
    return name


def house_number(value: str) -> int:
    return int(value) if value and value.isdigit() else 0


class RoadIndex:
    """Points every few metres along each OpenStreetMap road, in a grid, to find the road under a point."""

    def __init__(self, ways: list[dict], proj: Projection):
        self.proj = proj
        self.ways = ways
        self.cells: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
        for k, way in enumerate(ways):
            xy = [proj.xy(lat, lon) for lat, lon in way.get("geometry") or []]
            for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
                steps = max(1, int(math.hypot(x2 - x1, y2 - y1) // STEP_M))
                for s in range(steps + 1):
                    x, y = x1 + (x2 - x1) * s / steps, y1 + (y2 - y1) * s / steps
                    self.cells.setdefault((int(x // GRID_M), int(y // GRID_M)), []).append((x, y, k))

    def near(self, lat: float, lon: float, within: float = MATCH_M) -> dict[int, float]:
        """Road index -> distance in metres, for roads within `within` of the point."""
        x, y = self.proj.xy(lat, lon)
        cx, cy = int(x // GRID_M), int(y // GRID_M)
        found: dict[int, float] = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for px, py, k in self.cells.get((cx + dx, cy + dy), ()):
                    d = math.hypot(px - x, py - y)
                    if d <= within and d < found.get(k, within + 1):
                        found[k] = d
        return found


def along(points: list[tuple[float, float]], fraction: float, proj: Projection) -> tuple[float, float]:
    """The point this fraction of the way along a line."""
    if len(points) == 1:
        return points[0]
    xy = [proj.xy(*p) for p in points]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(xy, xy[1:])]
    target = max(0.0, min(1.0, fraction)) * sum(lengths)
    for (a, b), length in zip(zip(xy, xy[1:]), lengths):
        if target <= length and length > 0:
            t = target / length
            return proj.latlon(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        target -= length
    return points[-1]


def way_names(way: dict) -> list[str]:
    from .fetch_places import route_names
    return ([way["name"]] if way.get("name") else []) + route_names(way.get("ref") or "")


def match_road(name: str, points, roads: RoadIndex) -> dict | None:
    """The OpenStreetMap road a Census street piece lies on: under it, and named the same (or nearly)."""
    key = _name_key(clean_name(name))
    if not key:
        return None
    samples = [along(points, f, roads.proj) for f in (0.25, 0.5, 0.75)]
    hits: dict[int, int] = {}
    for lat, lon in samples:
        for k in roads.near(lat, lon):
            hits[k] = hits.get(k, 0) + 1
    best = None
    for k, count in hits.items():
        for road_name in way_names(roads.ways[k]):
            road_key = _name_key(road_name)
            ratio = 1.0 if road_key == key else SequenceMatcher(None, key, road_key).ratio()
            if ratio < NAME_RATIO:
                continue
            score = (ratio == 1.0, count, ratio)
            if best is None or score > best[0]:
                best = (score, k)
    return roads.ways[best[1]] if best else None


def build(features, geo: dict, bbox=None) -> tuple[dict, dict]:
    """Groups Census street pieces by OpenStreetMap name and town. Returns (ranges, counts)."""
    from .fetch_places import Towns

    ways = [w for w in geo.get("ways", []) if len(w.get("geometry") or []) >= 2]
    lats = [w["geometry"][0][0] for w in ways[:2000]]
    proj = Projection(sum(lats) / len(lats) if lats else 40.65)
    roads = RoadIndex(ways, proj)
    towns = Towns.from_outlines(geo.get("municipalities", []))
    streets: dict[tuple[str, str], dict] = {}
    counts = {"pieces": 0, "matched": 0, "no_numbers": 0, "unmatched": 0, "outside": 0}
    for row, points in features:
        lf, lt = house_number(row.get("LFROMHN")), house_number(row.get("LTOHN"))
        rf, rt = house_number(row.get("RFROMHN")), house_number(row.get("RTOHN"))
        if len(points) < 2:
            continue
        counts["pieces"] += 1
        if not (lf and lt) and not (rf and rt):
            counts["no_numbers"] += 1
            continue
        if bbox:
            south, west, north, east = bbox
            lat, lon = points[len(points) // 2]
            if not (south <= lat <= north and west <= lon <= east):
                counts["outside"] += 1
                continue
        road = match_road(row.get("FULLNAME") or "", points, roads)
        if road is None:
            counts["unmatched"] += 1
            continue
        counts["matched"] += 1
        middle = along(points, 0.5, proj)
        town = towns.find(*middle) or next(iter(road.get("municipalities") or []), "")
        segment = [lf if lt else 0, lt if lf else 0, rf if rt else 0, rt if rf else 0,
                   [[round(lat, 6), round(lon, 6)] for lat, lon in points]]
        for name in way_names(road):
            entry = streets.setdefault((name, town), {"name": name, "town": town, "segments": []})
            entry["segments"].append(segment)
    return {"version": 1, "streets": list(streets.values())}, counts


def fetch(geo: dict, out: Path, cache_dir: Path, counties=None, bbox=None) -> bool:
    """Downloads, matches and writes address-ranges.json. Returns False (leaving any old file alone)
    when a download fails."""
    features = []
    sources = []
    for county in counties or COUNTIES:
        log.info("Downloading house-number ranges for %s from the US Census...", COUNTIES.get(county, county))
        try:
            path = download(county, cache_dir)
        except CensusError as e:
            log.error("%s", e)
            log.error("The house-number check keeps using the ranges it had (if any). Try again later.")
            return False
        part = read_zip(path)
        log.info("  %d street pieces", len(part))
        features += part
        sources.append(path.name)
    ranges, counts = build(features, geo, bbox)
    ranges["source"] = "US Census TIGER/Line address features: " + ", ".join(sources)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(ranges, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)
    log.info("Wrote %s: house numbers for %d streets (%d of %d street pieces matched to the map, "
             "%d had no numbers, %d were outside the area)", out, len(ranges["streets"]), counts["matched"],
             counts["pieces"], counts["no_numbers"], counts["outside"])
    return True
