"""Highway mile markers: "337.5 Route 22 East", "78 westbound, mile marker 72.4", "33 South 12.5".

Dispatch gives highway calls by the posted mile marker. PennDOT publishes marker points only for
interstates (I-78 here), so every route is worked out the same way: each interchange's mile number
(ANCHORS) is pinned where the highway crosses it, and markers in between are measured along the
highway's own line from places-geo.json. Each direction is measured on its own carriageway, so an
eastbound call lands on the eastbound lanes. Nothing is downloaded.
"""

import heapq
import math
import re
from dataclasses import dataclass

METERS_PER_MILE = 1609.344
# An anchor further than this from its highway's line is ignored (wrong data, or the road moved).
SNAP_M = 250
# Past the first and last anchor, markers are measured on for at most this many miles.
EXTEND_MILES = 6

# Mile numbers of points along each highway, as posted. US 22 and PA 33 from the PennDOT
# interchange mileage (the mile markers dispatch reads out match it: "337.9, Bridge Commission
# property" is at the 4th Street/PA 611 bridge), I-78 from PennDOT's interstate mile marker points
# (gis.penndot.pa.gov opendata/interstatemilemarkers), I-476 from its exit numbers.
ANCHORS = {
    "Route 22": [
        (316.1, 40.59633, -75.56891),    # I-476
        (316.7, 40.59996, -75.55956),    # PA 309
        (318.7, 40.60974, -75.53174),    # Cedar Crest Blvd
        (321.0, 40.62645, -75.48329),    # MacArthur Rd / PA 145
        (322.1, 40.63083, -75.47014),    # Fullerton Ave
        (324.0, 40.64310, -75.43199),    # Airport Rd / PA 987
        (325.6, 40.65006, -75.41085),    # Schoenersville Rd
        (327.4, 40.66285, -75.38406),    # PA 512
        (329.9, 40.67847, -75.34240),    # PA 191
        (332.3, 40.68317, -75.28891),    # PA 33
        (334.5, 40.68632, -75.25180),    # 25th St
        (336.0, 40.69523, -75.22834),    # 13th St
        (337.9, 40.69444, -75.20476),    # PA 611
    ],
    "Route 33": [
        (1.147, 40.65277, -75.28739),    # Freemansburg Ave
        (2.148, 40.66671, -75.29316),    # William Penn Hwy
        (3.324, 40.68317, -75.28891),    # US 22
        (4.315, 40.69723, -75.28964),    # Hecktown Rd
        (5.678, 40.71728, -75.28864),    # PA 248
        (7.626, 40.73932, -75.26712),    # Tatamy Rd
        (8.814, 40.75529, -75.26844),    # PA 191
        (10.598, 40.77845, -75.28293),   # Belfast (Henry Rd)
    ],
    "I-78": [(float(mile), lat, lon) for mile, lat, lon in [
        (44, 40.579701, -75.731243), (45, 40.580465, -75.712773), (46, 40.578946, -75.693911),
        (47, 40.577160, -75.675154), (48, 40.575232, -75.656311), (49, 40.577428, -75.637617),
        (50, 40.580797, -75.619210), (51, 40.584697, -75.601010), (52, 40.587899, -75.583798),
        (53, 40.581384, -75.566914), (54, 40.576542, -75.549302), (55, 40.569434, -75.532832),
        (56, 40.567220, -75.513460), (57, 40.565508, -75.495252), (58, 40.563518, -75.476415),
        (59, 40.561522, -75.457781), (60, 40.557082, -75.439655), (61, 40.558227, -75.423273),
        (62, 40.567516, -75.408831), (63, 40.573238, -75.391677), (64, 40.585863, -75.383359),
        (65, 40.591752, -75.366025), (66, 40.596079, -75.349327), (67, 40.595328, -75.329366),
        (68, 40.601700, -75.312633), (69, 40.609249, -75.296978), (70, 40.620472, -75.285570),
        (71, 40.633292, -75.277354), (72, 40.642775, -75.263444), (73, 40.650266, -75.247113),
        (74, 40.660750, -75.234772), (75, 40.669165, -75.221776)]],
    "I-476": [
        (56.0, 40.59633, -75.56891),     # Lehigh Valley interchange (US 22)
    ],
}
# The direction miles count up in.
UP = {"Route 22": "e", "I-78": "e", "Route 33": "n", "I-476": "n"}
# Mile numbers each highway has in Pennsylvania near here; a number outside is not a mile marker of it.
# US 22 and I-78 end at the Delaware River (their lines go on into New Jersey, where miles start over).
RANGES = {"Route 22": (300.0, 338.2), "Route 33": (0.0, 20.0), "I-78": (40.0, 77.6), "I-476": (40.0, 80.0)}
# Interchange miles are rounded and pinned where the roads cross, a few hundred metres off where
# PennDOT measures them, so for these one straight line through all of them fits the evenly spaced
# posted markers better than going from one anchor to the next.
SMOOTH = {"Route 22"}
DIRECTION_NAMES = {"n": "northbound", "s": "southbound", "e": "eastbound", "w": "westbound"}


def _key(lat, lon):
    return round(lat, 6), round(lon, 6)


class _Line:
    """One direction of one highway as a line with a mile number at every point."""

    def __init__(self, points: list[tuple[float, float]], miles: list[float]):
        self.points = points
        self.miles = miles

    def at(self, mile: float) -> tuple[float, float] | None:
        miles = self.miles
        if not miles:
            return None
        rising = miles[-1] >= miles[0]
        lo, hi = (miles[0], miles[-1]) if rising else (miles[-1], miles[0])
        if not lo - 0.05 <= mile <= hi + 0.05:
            return None
        for k in range(len(miles) - 1):
            a, b = miles[k], miles[k + 1]
            if min(a, b) <= mile <= max(a, b):
                f = 0.0 if a == b else (mile - a) / (b - a)
                (lat1, lon1), (lat2, lon2) = self.points[k], self.points[k + 1]
                return lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f
        return self.points[0] if abs(mile - miles[0]) < abs(mile - miles[-1]) else self.points[-1]


class Mileposts:
    """Where each mile marker is, from the highways' lines (see Gazetteer.route_ways)."""

    def __init__(self, route_ways: dict[str, list[tuple[str, list]]], proj):
        self.proj = proj
        self.lines: dict[tuple[str, str], _Line] = {}
        for route, anchors in ANCHORS.items():
            ways = route_ways.get(route)
            if not ways:
                continue
            up = UP[route]
            down = {"e": "w", "w": "e", "n": "s", "s": "n"}[up]
            for direction, forward in ((up, True), (down, False)):
                line = self._measure(route, ways, anchors, forward)
                if line:
                    self.lines[(route, direction)] = line

    @property
    def routes(self) -> set[str]:
        return {route for route, _ in self.lines}

    def locate(self, route: str, mile: float, direction: str | None = None) -> tuple[float, float] | None:
        """The point of a mile marker; on the side of the road `direction` ("e", "w", "n", "s")
        names, else between both sides."""
        if direction:
            line = self.lines.get((route, direction))
            point = line.at(mile) if line else None
            if point:
                return point
        points = [line.at(mile) for (r, _), line in self.lines.items() if r == route]
        points = [p for p in points if p]
        if not points:
            return None
        return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)

    def markers(self, step: float = 1.0) -> list[dict]:
        """Every whole mile (or `step`) of every highway, between both sides, for the map."""
        out = []
        for route in sorted(self.routes):
            miles = [m for (r, _), line in self.lines.items() if r == route for m in (line.miles[0], line.miles[-1])]
            mile = math.ceil(min(miles) / step) * step
            while mile <= max(miles) + 1e-9:
                point = self.locate(route, round(mile, 3))
                if point:
                    out.append({"route": route, "mile": round(mile, 1), "lat": round(point[0], 6),
                                "lon": round(point[1], 6)})
                mile += step
        return out

    # ---- building

    def _measure(self, route, ways, anchors, forward: bool) -> _Line | None:
        """The highway's line in one direction, with the mile number at each point."""
        graph = self._graph(ways)
        if not graph:
            return None
        xy = {node: self.proj.xy(*node) for node in graph}
        ax, ay = self.proj.xy(*anchors[0][1:])
        bx, by = self.proj.xy(*anchors[-1][1:]) if len(anchors) > 1 else (ax, ay)
        if (ax, ay) == (bx, by):
            # One anchor: miles count up roughly east or north.
            bx, by = (ax + 1, ay) if UP[route] == "e" else (ax, ay + 1)
        ux, uy = bx - ax, by - ay
        norm = math.hypot(ux, uy) or 1.0
        ux, uy = ux / norm, uy / norm

        def along(node):
            x, y = xy[node]
            return (x - ax) * ux + (y - ay) * uy

        # Driving in the direction miles count up starts at the low end; the other way at the high end.
        nodes = sorted(graph, key=along, reverse=not forward)
        best = None
        for start in nodes[:12]:
            dist, prev = _dijkstra(graph, xy, start)
            end = max(dist, key=along) if forward else min(dist, key=along)
            span = abs(along(end) - along(start))
            if best is None or span > best[0] + 1:
                best = (span, start, end, prev, dist)
        if best is None or best[0] < METERS_PER_MILE:
            return None
        _, start, end, prev, dist = best
        path = [end]
        while path[-1] != start:
            path.append(prev[path[-1]])
        path.reverse()

        # Distance along the path at each point, then each anchor's distance where it is closest.
        run = [0.0]
        for a, b in zip(path, path[1:]):
            run.append(run[-1] + math.dist(xy[a], xy[b]))
        fixes = []
        for mile, lat, lon in anchors:
            at = self._project(path, run, xy, self.proj.xy(lat, lon))
            if at is not None:
                fixes.append((at, mile))
        fixes.sort()
        # Miles must go up (or down) steadily along the path; drop an anchor that disagrees.
        sign = 1 if forward else -1
        steady = []
        for at, mile in fixes:
            if not steady or (mile - steady[-1][1]) * sign > 0:
                steady.append((at, mile))
        if not steady:
            return None

        if route in SMOOTH and len(steady) >= 3:
            n = len(steady)
            md = sum(d for d, _ in steady) / n
            mm = sum(m for _, m in steady) / n
            slope = sum((d - md) * (m - mm) for d, m in steady) / sum((d - md) ** 2 for d, _ in steady)
            steady = [(d, mm + slope * (d - md)) for d in (steady[0][0], steady[-1][0])]

        def mile_at(d):
            if len(steady) == 1 or d <= steady[0][0]:
                return steady[0][1] + sign * (d - steady[0][0]) / METERS_PER_MILE
            if d >= steady[-1][0]:
                return steady[-1][1] + sign * (d - steady[-1][0]) / METERS_PER_MILE
            for (d1, m1), (d2, m2) in zip(steady, steady[1:]):
                if d1 <= d <= d2:
                    return m1 + (m2 - m1) * (d - d1) / (d2 - d1)
            return steady[-1][1]

        points, miles = [], []
        first, last = steady[0][0] - EXTEND_MILES * METERS_PER_MILE, steady[-1][0] + EXTEND_MILES * METERS_PER_MILE
        low, high = RANGES[route]
        for node, d in zip(path, run):
            if first <= d <= last and low <= mile_at(d) <= high:
                points.append(node)
                miles.append(mile_at(d))
        return _Line(points, miles) if len(points) > 1 else None

    @staticmethod
    def _graph(ways) -> dict:
        """Points of the highway joined in driving order. A motorway is drawn in the direction it is
        driven; a trunk road may be two-way, so it is joined both ways."""
        graph: dict = {}
        for highway, line in ways:
            nodes = [_key(*p) for p in line]
            for a, b in zip(nodes, nodes[1:]):
                if a == b:
                    continue
                graph.setdefault(a, set()).add(b)
                graph.setdefault(b, set())
                if highway != "motorway":
                    graph[b].add(a)
        return graph

    @staticmethod
    def _project(path, run, xy, point) -> float | None:
        """Distance along the path where it passes closest to a point, None when it's too far."""
        px, py = point
        best = None
        for k, (a, b) in enumerate(zip(path, path[1:])):
            (x1, y1), (x2, y2) = xy[a], xy[b]
            dx, dy = x2 - x1, y2 - y1
            seg = dx * dx + dy * dy
            f = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg))
            d = math.hypot(x1 + f * dx - px, y1 + f * dy - py)
            if best is None or d < best[0]:
                best = (d, run[k] + f * math.sqrt(seg))
        return best[1] if best and best[0] <= SNAP_M else None


def _dijkstra(graph, xy, start):
    dist = {start: 0.0}
    prev = {}
    heap = [(0.0, start)]
    while heap:
        d, node = heapq.heappop(heap)
        if d > dist.get(node, math.inf):
            continue
        for nxt in graph[node]:
            nd = d + math.dist(xy[node], xy[nxt])
            if nd < dist.get(nxt, math.inf):
                dist[nxt] = nd
                prev[nxt] = node
                heapq.heappush(heap, (nd, nxt))
    return dist, prev


# ---- finding mile markers in transcripts

# A highway: "Route 22", "US-22", "I-78", "Interstate 78", "Rt. 33", or the bare number ("22 westbound").
ROAD = re.compile(r"(?<![\d.\-])(?:(?:route|rt|rte|us|pa|sr|interstate|i)\.?[\s-]*)?(22|33|78|476)(?![\d.]\d|-\d)"
                  r"|\b(?:turnpike|northeast extension|north east extension)\b", re.IGNORECASE)
ROUTE_WORD = re.compile(r"(route|rt|rte|us|pa|sr|interstate|i)\b", re.IGNORECASE)
ROAD_NAMES = {"22": "Route 22", "33": "Route 33", "78": "I-78", "476": "I-476"}
# "337.5", "9.4", Whisper's "3-3-4-1" (334.1) and "3-3-1.8" (331.8).
NUMBER = re.compile(r"(?<![\d.\-])(\d{1,3}\.\d|\d(?:-\d){3}|\d-\d-\d\.\d)(?![\d\-]|\.\d)")
# "mile marker 72", "mile 72", "marker of 334.2"
MARKER = re.compile(r"\b(?:mile\s*marker|mile\s*mark|mile\s*post|marker|mile)\b(?:\s+(?:of|is|was|at))?[\s,]*"
                    r"(\d{1,3}(?:\.\d)?)(?![\d\-]|\.\d)", re.IGNORECASE)
MARKER_WORD = re.compile(r"\b(mile\s*marker|mile\s*mark|mile\s*post|marker|mile)\b", re.IGNORECASE)
DIRECTION = re.compile(r"\b(north|south|east|west)\s*-?\s*(bound)?\b|\b([nsew])\.?b\b", re.IGNORECASE)
# "starting mileage 498.8", "60 miles per hour": not a place.
NOT_A_PLACE = re.compile(r"\bmileage\b|\bmiles?\s+(?:per|an)\s+hour\b|\bmph\b", re.IGNORECASE)
# How far apart (characters) the road, the number and the direction may be.
NEAR = 45


@dataclass
class Milepost:
    route: str
    mile: float
    direction: str | None   # "e", "w", "n", "s" or None
    heard: str

    @property
    def label(self) -> str:
        way = f" {DIRECTION_NAMES[self.direction]}" if self.direction else ""
        return f"{self.route}{way} at mile marker {self.mile:.1f}"


def _number(raw: str) -> float:
    if "-" in raw:
        digits = raw.replace("-", "").replace(".", "")
        return int(digits[:-1]) + int(digits[-1]) / 10
    return float(raw)


def _fit(route: str, mile: float) -> float | None:
    """The mile as posted on this highway, or None. Whisper often drops the leading 3 of Route 22's
    miles ("Route 27.4, 22" for 327.4)."""
    low, high = RANGES[route]
    if low <= mile <= high:
        return mile
    if route == "Route 22" and 10 <= mile < 40 and low <= mile + 300 <= high:
        return mile + 300
    return None


def find(text: str | None, routes=None) -> Milepost | None:
    """The first highway mile marker in a transcript, or None. `routes` limits it to highways the
    map knows the markers of."""
    if not text or NOT_A_PLACE.search(text):
        return None
    roads = []
    for m in ROAD.finditer(text):
        route = ROAD_NAMES[m.group(1)] if m.group(1) else "I-476"
        if routes is not None and route not in routes:
            continue
        roads.append((m.start(), m.end(), route, bool(ROUTE_WORD.match(m.group(0))) or not m.group(1)))
    if not roads:
        return None
    numbers = [(m.start(1), m.end(1), m.group(1), True) for m in MARKER.finditer(text)]
    numbers += [(m.start(), m.end(), m.group(1), False) for m in NUMBER.finditer(text)
                if not any(s <= m.start() < e for s, e, _, _ in numbers)]
    numbers.sort()
    directions = [(m.start(), m.end(), (m.group(1) or m.group(3))[0].lower()) for m in DIRECTION.finditer(text)]

    for start, end, raw, said_marker in numbers:
        best = None
        for r_start, r_end, route, named in roads:
            if r_start < end and start < r_end:
                continue  # the number is the road's own ("33" in "Route 33")
            gap = r_start - end if r_start >= end else start - r_end
            if gap > NEAR:
                continue
            mile = _fit(route, _number(raw))
            if mile is None:
                continue
            if not said_marker and "." not in raw and "-" not in raw:
                continue
            way = None
            for d_start, d_end, d in directions:
                if min(abs(d_start - r_end), abs(r_start - d_end), abs(d_start - end), abs(start - d_end)) <= 25:
                    if (d in "ew") == (UP[route] in "ew"):
                        way = d
                        break
            # A bare "22" or "33" next to a small number is only a mile marker when something else
            # says highway: "mile marker", "Route", or a direction.
            near_marker = said_marker or bool(MARKER_WORD.search(text[max(0, min(start, r_start) - NEAR):
                                                                       max(end, r_end) + NEAR]))
            # Route 22's miles (300 and up) are unlike any other number said on the radio.
            distinctive = route == "Route 22" and re.match(r"3\d\d\.", raw) is not None
            if not (named or way or near_marker or distinctive):
                continue
            if best is None or gap < best[0]:
                lo, hi = min(start, r_start), max(end, r_end)
                best = (gap, Milepost(route, mile, way, text[lo:hi].strip(" ,.")))
        if best:
            return best[1]
    return None
