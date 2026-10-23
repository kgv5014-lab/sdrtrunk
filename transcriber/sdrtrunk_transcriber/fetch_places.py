"""Builds places.csv (and places-geo.json for the map) from OpenStreetMap.

    python -m sdrtrunk_transcriber.fetch_places            # Easton to Allentown, PA
    python -m sdrtrunk_transcriber.fetch_places --bbox 40.50,-75.62,40.78,-75.17

Downloads named roads, numbered routes, landmarks and town boundaries for the area from the Overpass
API (three requests), works out which town each one is in, ranks roads by size and length, and writes:

    places.csv       one row per name and town; see places.py for the columns
    places-geo.json  road lines, landmark points and town outlines, for the incident map

Rows in an existing places.csv whose source isn't "osm" or "example" are hand-made and are kept.
"""

import argparse
import csv
import http.client
import json
import logging
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# Public Overpass servers, tried in order.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
CACHE_SECONDS = 24 * 3600
# Easton (east) to Allentown (west), Pennsylvania: south, west, north, east.
DEFAULT_BBOX = (40.50, -75.62, 40.78, -75.17)
USER_AGENT = "sdrtrunk-transcriber/1 (places list for a scanner)"

ROAD_CLASSES = {
    "motorway": 100, "trunk": 90, "primary": 80, "secondary": 65, "tertiary": 50,
    "motorway_link": 30, "trunk_link": 30, "primary_link": 30, "secondary_link": 25,
    "unclassified": 25, "residential": 15, "living_street": 10,
}
LANDMARKS = {
    ("amenity", "hospital"): 85, ("aeroway", "aerodrome"): 70, ("amenity", "university"): 65,
    ("amenity", "college"): 60, ("shop", "mall"): 60, ("amenity", "fire_station"): 55,
    ("amenity", "police"): 55, ("amenity", "townhall"): 45, ("amenity", "courthouse"): 50,
    ("amenity", "school"): 40, ("amenity", "nursing_home"): 40, ("amenity", "clinic"): 30,
    ("leisure", "stadium"): 45, ("leisure", "park"): 30, ("tourism", "hotel"): 25,
}
# Places ranked at least this high are Whisper hint candidates (prompt = yes).
PROMPT_RANK = 60
# ...plus the top few roads of every town, so small boroughs get their main streets.
PROMPT_PER_TOWN = 8
COLUMNS = ["name", "kind", "municipality", "spoken", "prompt", "rank", "osm_id", "source"]
ROUTE_REF = re.compile(r"^(US|PA|SR|I)[\s-]*(\d{1,3})$", re.IGNORECASE)


def queries(bbox) -> dict[str, str]:
    box = ",".join(f"{v:.5f}" for v in bbox)
    roads = "|".join(ROAD_CLASSES)
    landmark_lines = "\n".join(f'  nwr["{k}"="{v}"]["name"]({box});' for k, v in LANDMARKS)
    return {
        "roads": f'[out:json][timeout:300];\nway["highway"~"^({roads})$"]["name"]({box});\nout tags geom;',
        "routes": f'[out:json][timeout:300];\nway["highway"~"^(motorway|trunk|primary|secondary)$"]["ref"]({box});\n'
                  f'out tags geom;',
        "landmarks": f"[out:json][timeout:300];\n(\n{landmark_lines}\n);\nout tags center;",
        # "body", not "tags": a relation's members (with their outlines) only come with body.
        "towns": f'[out:json][timeout:300];\nrel["boundary"="administrative"]["admin_level"="8"]({box});\n'
                 f'out body geom;',
    }


def overpass(query: str, url: str | None = None, attempts: int = 3) -> dict:
    """Runs one Overpass query. Busy servers, dropped connections and cut-off downloads are retried,
    then the next public Overpass server is tried."""
    data = urllib.parse.urlencode({"data": query}).encode()
    servers = [url] if url else OVERPASS_MIRRORS
    last_error = None
    for server in servers:
        for attempt in range(attempts):
            request = urllib.request.Request(server, data=data, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=360) as response:
                    result = json.loads(response.read())
                remark = result.get("remark") or ""
                if "error" in remark.lower():
                    raise OverpassError(remark)  # e.g. "runtime error: Query timed out"
                return result
            except urllib.error.HTTPError as e:
                if e.code not in (429, 502, 503, 504):
                    raise
                last_error = f"HTTP {e.code}"
            except (http.client.IncompleteRead, http.client.HTTPException, urllib.error.URLError, OSError,
                    ValueError, OverpassError) as e:
                last_error = f"{type(e).__name__}: {str(e)[:200]}"
            if attempt < attempts - 1:
                wait = 20 * (attempt + 1)
                log.warning("  %s failed (%s), trying again in %d s", server, last_error, wait)
                time.sleep(wait)
        log.warning("  %s didn't work (%s)", server, last_error)
    raise OverpassError(f"Couldn't download from OpenStreetMap ({last_error}). Try again in a few minutes.")


class OverpassError(Exception):
    pass


def cached(cache_dir: Path, name: str, query: str, fetch) -> dict:
    """Reuses a download from the last day so a re-run after a failure only fetches what's missing."""
    path = cache_dir / f"{name}.json"
    try:
        if time.time() - path.stat().st_mtime < CACHE_SECONDS:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("query") == query:
                log.info("  using the copy downloaded at %s", time.strftime("%H:%M", time.localtime(path.stat().st_mtime)))
                return saved["result"]
    except (OSError, ValueError, KeyError):
        pass
    result = fetch()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"query": query, "result": result}), encoding="utf-8")
    except OSError as e:
        log.warning("  couldn't save %s: %s", path, e)
    return result


class Towns:
    """Point-in-town lookup from admin boundary relations (even-odd rule over all member lines,
    so outer and inner rings work without stitching them together)."""
    BAND = 0.005

    def __init__(self, relations: list[dict]):
        self.towns = []
        for rel in relations:
            name = rel.get("tags", {}).get("name")
            segments = []
            for member in rel.get("members", []):
                if member.get("type") != "way" or member.get("role") not in ("outer", "inner", ""):
                    continue
                points = [(p["lat"], p["lon"]) for p in member.get("geometry", []) if p]
                segments += list(zip(points, points[1:]))
            if not name or not segments:
                continue
            bands: dict[int, list] = {}
            for a, b in segments:
                for band in range(self._band(min(a[0], b[0])), self._band(max(a[0], b[0])) + 1):
                    bands.setdefault(band, []).append((a, b))
            lats = [p[0] for s in segments for p in s]
            lons = [p[1] for s in segments for p in s]
            self.towns.append({"name": name, "bbox": (min(lats), min(lons), max(lats), max(lons)),
                               "bands": bands, "segments": segments})

    def _band(self, lat: float) -> int:
        return math.floor(lat / self.BAND)

    def find(self, lat: float, lon: float) -> str | None:
        for town in self.towns:
            s, w, n, e = town["bbox"]
            if not (s <= lat <= n and w <= lon <= e):
                continue
            inside = False
            for (lat1, lon1), (lat2, lon2) in town["bands"].get(self._band(lat), []):
                if (lat1 > lat) != (lat2 > lat):
                    crossing = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
                    if crossing > lon:
                        inside = not inside
            if inside:
                return town["name"]
        return None

    def outlines(self) -> list[dict]:
        return [{"name": t["name"], "bbox": t["bbox"],
                 "lines": [[[round(a[0], 6), round(a[1], 6)], [round(b[0], 6), round(b[1], 6)]]
                           for a, b in t["segments"]]} for t in self.towns]


def length_m(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
        y = math.radians(lat2 - lat1)
        total += 6371000 * math.hypot(x, y)
    return total


def route_names(ref: str) -> list[str]:
    """"US 22;PA 33" -> ["Route 22", "Route 33"], "I 78" -> ["I-78"]."""
    names = []
    for part in re.split(r"[;,]", ref):
        match = ROUTE_REF.match(part.strip())
        if match:
            names.append(f"I-{int(match.group(2))}" if match.group(1).upper() == "I"
                         else f"Route {int(match.group(2))}")
    return names


def build(roads: dict, routes: dict, landmarks: dict, towns_data: dict):
    towns = Towns([e for e in towns_data.get("elements", []) if e.get("type") == "relation"])
    places: dict[tuple[str, str], dict] = {}
    geo_ways = []

    def add(name, kind, town, rank, osm_id):
        key = (name, town or "")
        current = places.get(key)
        if current is None:
            places[key] = {"name": name, "kind": kind, "municipality": town or "", "rank": rank,
                           "osm_id": osm_id, "length": 0.0}
        elif rank > current["rank"]:
            current["rank"], current["osm_id"] = rank, osm_id
        return places[key]

    seen = set()
    for way in roads.get("elements", []) + routes.get("elements", []):
        if way.get("id") in seen:
            continue  # named routes come back in both downloads
        seen.add(way.get("id"))
        tags = way.get("tags", {})
        points = [(p["lat"], p["lon"]) for p in way.get("geometry", []) if p]
        if len(points) < 2:
            continue
        sample = [points[0], points[len(points) // 2], points[-1]]
        way_towns = sorted({t for t in (towns.find(*p) for p in sample) if t}) or [""]
        meters = length_m(points)
        weight = ROAD_CLASSES.get(tags.get("highway"), 10)
        osm_id = f"way/{way['id']}"
        names = []
        if tags.get("name"):
            names.append((tags["name"], "street"))
        names += [(n, "route") for n in route_names(tags.get("ref", ""))]
        for name, kind in names:
            for town in (way_towns if kind == "street" else [""]):
                place = add(name, kind, town, weight, osm_id)
                place["length"] += meters
        geo_ways.append({"id": osm_id, "name": tags.get("name"), "ref": tags.get("ref"),
                         "highway": tags.get("highway"), "municipalities": [t for t in way_towns if t],
                         "geometry": [[round(lat, 6), round(lon, 6)] for lat, lon in points]})

    geo_landmarks = []
    for element in landmarks.get("elements", []):
        tags = element.get("tags", {})
        center = element.get("center") or ({"lat": element["lat"], "lon": element["lon"]}
                                           if "lat" in element else None)
        if not tags.get("name") or not center:
            continue
        rank = max((r for (k, v), r in LANDMARKS.items() if tags.get(k) == v), default=30)
        town = towns.find(center["lat"], center["lon"])
        osm_id = f"{element['type']}/{element['id']}"
        add(tags["name"], "landmark", town, rank, osm_id)
        geo_landmarks.append({"id": osm_id, "name": tags["name"], "municipality": town,
                              "type": next((f"{k}={v}" for k, v in LANDMARKS if tags.get(k) == v), None),
                              "lat": round(center["lat"], 6), "lon": round(center["lon"], 6)})

    # Size plus length: a long secondary road outranks a short one, a 3 km residential street a cul-de-sac.
    for place in places.values():
        if place["kind"] != "landmark":
            place["rank"] += min(20, round(8 * math.log10(1 + place["length"] / 100)))

    rows = sorted(places.values(), key=lambda p: (p["municipality"], -p["rank"], p["name"]))
    per_town: dict[str, int] = {}
    for row in rows:
        count = per_town.get(row["municipality"], 0)
        row["prompt"] = "yes" if row["rank"] >= PROMPT_RANK or (row["kind"] == "street" and count < PROMPT_PER_TOWN) \
            else ""
        if row["kind"] == "street":
            per_town[row["municipality"]] = count + 1
        row["spoken"] = ""
        row["source"] = "osm"

    geo = {"ways": geo_ways, "landmarks": geo_landmarks, "municipalities": towns.outlines()}
    return rows, geo


def kept_rows(path: Path) -> list[dict]:
    """Hand-made rows from an existing places.csv."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [row for row in csv.DictReader(f)
                if (row.get("source") or "").strip().lower() not in ("osm", "example") and row.get("name")]


def write(path: Path, rows: list[dict]):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build places.csv from OpenStreetMap.")
    parser.add_argument("--bbox", help="south,west,north,east (default: Easton to Allentown, PA)")
    parser.add_argument("--out", default="places.csv", help="CSV to write (default places.csv)")
    parser.add_argument("--geo", default="places-geo.json", help="map geometry to write")
    parser.add_argument("--url", help="Overpass API address (default: try the public servers in turn)")
    parser.add_argument("--cache", default="data/osm-cache", help="where downloads are kept for a day")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else DEFAULT_BBOX
    if len(bbox) != 4:
        parser.error("--bbox needs four numbers: south,west,north,east")

    results = {}
    for name, query in queries(bbox).items():
        log.info("Downloading %s from OpenStreetMap...", name)
        try:
            results[name] = cached(Path(args.cache), name, query, lambda: overpass(query, args.url))
        except OverpassError as e:
            log.error("%s", e)
            log.error("Nothing was changed. Downloads that worked are kept, so the next run picks up from here.")
            return 1
        log.info("  %d items", len(results[name].get("elements", [])))

    rows, geo = build(results["roads"], results["routes"], results["landmarks"], results["towns"])
    out = Path(args.out)
    kept = kept_rows(out)
    write(out, kept + rows)
    Path(args.geo).write_text(json.dumps(geo, separators=(",", ":")), encoding="utf-8")

    towns = len({r["municipality"] for r in rows if r["municipality"]})
    hints = sum(1 for r in rows if r["prompt"])
    log.info("Wrote %s: %d places in %d towns (%d hint candidates), kept %d hand-made rows",
             out, len(rows), towns, hints, len(kept))
    log.info("Wrote %s for the map", args.geo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
