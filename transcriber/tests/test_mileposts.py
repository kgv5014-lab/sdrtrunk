"""Highway mile markers: reading them from transcripts and placing them on the highway."""

import json
import math

import pytest

from sdrtrunk_transcriber.addresses import Gazetteer, Projection
from sdrtrunk_transcriber.mileposts import ANCHORS, Mileposts, find


# ---- reading them: all heard on Northampton County dispatch

@pytest.mark.parametrize("text, route, mile, direction", [
    ("337.5 Route 22 East, 337.5 Route 22 East for two vehicle MVA", "Route 22", 337.5, "e"),
    ("we have a road hazard call in for Route 22 West around mile marker 337.9", "Route 22", 337.9, "w"),
    ("a disabled vehicle, 333.4, going eastbound on 22, caller reporting", "Route 22", 333.4, "e"),
    ("3-3-4-1, Route 22, East, 3-3-4-1, Route 22, East", "Route 22", 334.1, "e"),
    ("unknown injuries, Route 27.4, 22, Westbound", "Route 22", 327.4, "w"),
    ("22 West. It looks like around number 34.7.", "Route 22", 334.7, "w"),
    ("22, Westbound 33, 31.3, for a bicycle accident", "Route 22", 331.3, "w"),
    ("Mile marker 336 Route 22 East. White Honda", "Route 22", 336.0, "e"),
    ("9.4, Route 33 North, 9.4, Route 33 North", "Route 33", 9.4, "n"),
    ("it'll be 33 Southbound 12.5 is the mile marker", "Route 33", 12.5, "s"),
    ("Yeah, Route 33, NB, 6.2, Lady just came over", "Route 33", 6.2, "n"),
    ("in the area of mile marker 72.4, 78 Westbound", "I-78", 72.4, "w"),
    ("MVA, I-78, Westbound, mile marker 68.8", "I-78", 68.8, "w"),
    ("you're going to 78 westbound, number 64.4, tractor trailer", "I-78", 64.4, "w"),
    ("325.8, 22 Easton, the motorcycle", "Route 22", 325.8, None),
])
def test_finds_mile_markers(text, route, mile, direction):
    found = find(text)
    assert found is not None
    assert (found.route, found.mile, found.direction) == (route, pytest.approx(mile), direction)


@pytest.mark.parametrize("text", [
    "Transporting one female, starting mileage 498.8",
    "I'll be out of NCP, ending mileage 334.5 on 22",
    "10-4 at 22:15 hours",
    "Engine 22, 3.5 minutes out",
    "Unit 33 on scene 10.5",
    "Route 22 and 25th Street, two vehicles",
    "doing 60 miles per hour on Route 22",
    "Office Milenberg, 3.30",
])
def test_ignores_other_numbers(text):
    assert find(text) is None


def test_only_highways_the_map_knows():
    assert find("I-78 westbound mile marker 68.8", routes={"Route 22"}) is None


# ---- placing them

def _carriageways(anchors, offset_m, proj):
    """Two one-way lines through the anchors, `offset_m` either side, drawn in driving order."""
    pts = [proj.xy(lat, lon) for _, lat, lon in anchors]
    # The road goes on a kilometre past the first and last anchor.
    for end, nxt, at in ((pts[0], pts[1], 0), (pts[-1], pts[-2], len(pts))):
        dx, dy = end[0] - nxt[0], end[1] - nxt[1]
        n = math.hypot(dx, dy)
        pts.insert(at, (end[0] + dx / n * 1000, end[1] + dy / n * 1000))
    up, down = [], []
    for k, (x, y) in enumerate(pts):
        a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy)
        # Right-hand traffic: the lanes going up the miles are on the right of that direction.
        rx, ry = dy / n, -dx / n
        up.append(proj.latlon(x + rx * offset_m, y + ry * offset_m))
        down.append(proj.latlon(x - rx * offset_m, y - ry * offset_m))
    return [("motorway", up), ("motorway", list(reversed(down)))]


@pytest.fixture
def route33():
    proj = Projection()
    return Mileposts({"Route 33": _carriageways(ANCHORS["Route 33"], 15, proj)}, proj), proj


def test_anchors_land_where_they_are(route33):
    marks, proj = route33
    for mile, lat, lon in ANCHORS["Route 33"]:
        point = marks.locate("Route 33", mile)
        assert math.dist(proj.xy(*point), proj.xy(lat, lon)) < 10


def test_each_direction_on_its_own_side(route33):
    marks, proj = route33
    north = marks.locate("Route 33", 9.4, "n")
    south = marks.locate("Route 33", 9.4, "s")
    assert 25 < math.dist(proj.xy(*north), proj.xy(*south)) < 35
    # Between the PA 191 (8.814) and Belfast (10.598) interchanges, a third of the way along.
    (_, la, oa), (_, lb, ob) = ANCHORS["Route 33"][-2:]
    middle = marks.locate("Route 33", 9.4)
    f = (9.4 - 8.814) / (10.598 - 8.814)
    expected = (la + (lb - la) * f, oa + (ob - oa) * f)
    assert math.dist(proj.xy(*middle), proj.xy(*expected)) < 30


def test_markers_past_the_first_anchor_and_off_the_road(route33):
    marks, _ = route33
    assert marks.locate("Route 33", 0.6, "n") is not None      # measured on before Freemansburg Ave
    assert marks.locate("Route 33", 11.1, "s") is not None     # and after Belfast
    assert marks.locate("Route 33", 19.0) is None               # off the downloaded map
    assert marks.locate("Route 22", 330.0) is None
    whole = [m["mile"] for m in marks.markers()]
    assert whole == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0]


def test_gazetteer_places_a_mile_marker_call(tmp_path):
    proj = Projection()
    ways = [{"id": f"way/{k}", "name": None, "ref": "PA 33", "highway": highway, "municipalities": [],
             "geometry": [[round(lat, 6), round(lon, 6)] for lat, lon in line]}
            for k, (highway, line) in enumerate(_carriageways(ANCHORS["Route 33"], 15, proj))]
    (tmp_path / "places-geo.json").write_text(json.dumps({"ways": ways}), encoding="utf-8")
    gazetteer = Gazetteer(tmp_path / "places-geo.json", None)
    found = gazetteer.find("County Fire 53, MVA with injuries, 33 southbound, mile marker 5.8.")
    assert found.kind == "milepost"
    assert found.label == "Route 33 southbound at mile marker 5.8"
    assert len(found.points) == 1
    # Past the downloaded highway: not placed as a mile marker.
    assert gazetteer.find("33 southbound, mile marker 18.5") is None
