"""Incident map: finding places in transcripts, placing them, and grouping calls into incidents."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from sdrtrunk_transcriber.addresses import Gazetteer, town_key, words
from sdrtrunk_transcriber.backends import Transcript
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.geocode import Geocoder
from sdrtrunk_transcriber.incidents import MapWorker, highlights, nature_of
from sdrtrunk_transcriber.server import create_app

from test_service import CALL, FakeBackend, make_mp3, post, sdrtrunk_body


def way(name, town, points, ref=None, highway="residential", way_id=[0]):
    way_id[0] += 1
    return {"id": f"way/{way_id[0]}", "name": name, "ref": ref, "highway": highway,
            "municipalities": [town] if town else [], "geometry": points}


def line(lat1, lon1, lat2, lon2, n=6):
    return [[round(lat1 + (lat2 - lat1) * k / n, 6), round(lon1 + (lon2 - lon1) * k / n, 6)] for k in range(n + 1)]


# A made-up but realistically laid out piece of the Lehigh Valley.
GEO = {
    "ways": [
        way("Northampton Street", "Easton", line(40.6915, -75.230, 40.6915, -75.200), highway="primary"),
        way("North 3rd Street", "Easton", line(40.685, -75.2100, 40.700, -75.2100)),
        way("Main Street", "Bethlehem", line(40.620, -75.390, 40.620, -75.370)),
        way("Center Street", "Bethlehem", line(40.610, -75.380, 40.630, -75.380)),
        way("Main Street", "Nazareth", line(40.740, -75.320, 40.740, -75.300)),
        way("Center Street", "Nazareth", line(40.730, -75.310, 40.750, -75.310)),
        way("William Penn Highway", "Palmer Township", line(40.680, -75.300, 40.680, -75.220), ref="US 22",
            highway="trunk"),
        way("Freemansburg Avenue", "Palmer Township", line(40.670, -75.250, 40.690, -75.250)),
        way("Short Lane", "Wilson", line(40.6800, -75.2400, 40.6810, -75.2400, n=2)),
    ],
    "landmarks": [{"id": "way/99", "name": "Easton Hospital", "municipality": "Easton", "type": "amenity=hospital",
                   "lat": 40.6930, "lon": -75.2290}],
    "municipalities": [{"name": n, "bbox": [0, 0, 0, 0], "lines": []}
                       for n in ("Easton", "Bethlehem", "Nazareth", "Palmer Township", "Wilson", "Bethlehem Township")],
}


@pytest.fixture
def gaz(tmp_path):
    (tmp_path / "places-geo.json").write_text(json.dumps(GEO), encoding="utf-8")
    (tmp_path / "places.csv").write_text(
        "name,kind,municipality,spoken,prompt,rank,osm_id,source\n"
        "Northampton Street,street,Easton,the main drag,yes,80,,example\n"
        "Larry Holmes Drive,street,Easton,,yes,75,,example\n", encoding="utf-8")
    return Gazetteer(tmp_path / "places-geo.json", tmp_path / "places.csv")


def near(point, lat, lon, metres=30):
    dx = (point[1] - lon) * 84_400
    dy = (point[0] - lat) * 110_574
    return (dx * dx + dy * dy) ** 0.5 <= metres


@pytest.mark.parametrize("text,expected", [
    ("North Third Street", ["n", "3rd", "st"]),
    ("N. 3rd St.", ["n", "3rd", "st"]),
    ("twenty-second street", ["22nd", "st"]),
    ("Route 22", ["route", "22"]),
    ("Rt. 248", ["route", "248"]),
    ("US-22", ["route", "22"]),
    ("I-78", ["route", "78"]),
    ("route twenty two", ["route", "22"]),
    ("St. John's Avenue", ["st", "johns", "ave"]),
    ("give us 2 minutes", ["give", "route", "2", "minutes"]),
])
def test_words(text, expected):
    assert words(text) == expected


@pytest.mark.parametrize("name,key", [
    ("Palmer Township", "palmer twp"), ("Palmer Twp", "palmer twp"), ("City of Easton", "easton"),
    ("Nazareth Borough", "nazareth"), ("Easton", "easton"),
])
def test_town_key(name, key):
    assert town_key(name) == key


def test_intersection_is_placed_offline(gaz):
    found = gaz.find("Engine 4 respond to North Third and Northampton for a structure fire")
    assert found.kind == "intersection"
    assert found.label == "North 3rd Street & Northampton Street, Easton"
    assert len(found.points) == 1 and near(found.points[0], 40.6915, -75.2100)


def test_route_intersection(gaz):
    found = gaz.find("Crash on Route 22 at Freemansburg Ave, two cars")
    assert found.kind == "intersection"
    assert len(found.points) == 1 and near(found.points[0], 40.680, -75.250)


def test_same_names_in_two_towns_need_the_town(gaz):
    assert len(gaz.find("Main and Center for a fall").points) == 2
    found = gaz.find("Main and Center in Nazareth for a fall")
    assert len(found.points) == 1 and near(found.points[0], 40.740, -75.310)
    # The talkgroup name can say the town too.
    found = gaz.find("Main and Center for a fall", "Bethlehem PD")
    assert len(found.points) == 1 and near(found.points[0], 40.620, -75.380)


def test_between_two_cross_streets(gaz):
    found = gaz.find("Northampton Street between North 3rd and 4th")
    assert found.kind == "intersection" and near(found.points[0], 40.6915, -75.2100)


def test_township_without_the_word_township(gaz):
    assert gaz.towns_in(words("Route 22 in Palmer")) == {"palmer twp"}
    assert gaz.towns_in(words("in Bethlehem")) == {"bethlehem"}
    assert gaz.towns_in(words("Bethlehem Township")) == {"bethlehem twp"}


def test_streets_that_do_not_meet_are_not_an_intersection(gaz):
    assert gaz.find("Northampton Street and Freemansburg Avenue") is None


def test_house_number_address(gaz):
    found = gaz.find("Medic 2081 respond 1234 Northampton Street for a fall")
    assert found.kind == "address"
    assert found.label == "1234 Northampton Street, Easton"
    assert found.query == "1234 Northampton Street, Easton, Pennsylvania"
    found = gaz.find("the 1200 block of Northampton St")
    assert found.label == "1200 block of Northampton Street, Easton"


@pytest.mark.parametrize("text", [
    "Engine 4 Route 22 westbound",          # unit number before a route
    "Medic 2081 Northampton Street",        # call sign before a street
    "10-4, on the way",
    "Medic 49 is 10-8",
    "",
])
def test_not_places(gaz, text):
    assert gaz.find(text) is None


def test_landmark(gaz):
    found = gaz.find("Respond to Easton Hospital, the ER entrance, for a fall")
    assert found.kind == "landmark" and near(found.points[0], 40.6930, -75.2290)
    # Taking a patient there doesn't make it an incident.
    assert gaz.find("Medic 2081 transporting one to Easton Hospital") is None


def test_spoken_alias_from_places_csv(gaz):
    # "the main drag" has no suffix, so it is a weak mention: only usable where it meets a street.
    found = gaz.find("the main drag and North 3rd Street")
    assert found and found.kind == "intersection"


def test_names_only_without_shapes_go_to_the_online_lookup(tmp_path):
    (tmp_path / "places.csv").write_text(
        "name,kind,municipality\nNorthampton Street,street,Easton\nLarry Holmes Drive,street,Easton\n",
        encoding="utf-8")
    gaz = Gazetteer(tmp_path / "missing.json", tmp_path / "places.csv")
    found = gaz.find("Northampton Street and Larry Holmes Drive")
    assert found.kind == "intersection" and found.points == []
    assert found.query == "Northampton Street & Larry Holmes Drive, Easton, Pennsylvania"


def test_nature():
    assert nature_of("Report of a structure fire, smoke showing") == ("Structure fire", "fire")
    assert nature_of("Medic 49, 10-8") == (None, None)


@pytest.mark.parametrize("text", [
    "two vehicle MVA with injuries", "M.V.A. on Route 22", "respond for an M V A", "MVC, one car",
    "motor vehicle accident, unknown injuries", "car into a pole, rollover",
])
def test_mva(text):
    assert nature_of(text) == ("MVA", None)


def test_highlights():
    text = "Respond for an MVA with a car fire, smoke showing"
    assert [text[a:b] for a, b in highlights(text)] == ["MVA", "car fire", "smoke showing"]
    assert highlights(None) == []


class FakeResponse:
    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self.rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs["params"]["q"])
        return FakeResponse(self.rows)


def test_geocoder_caches_answers_including_misses(tmp_path):
    db = Database(tmp_path / "calls.db")
    session = FakeSession([{"lat": "40.6915", "lon": "-75.2150", "category": "place", "type": "house",
                            "addresstype": "place", "display_name": "1234, Northampton Street"}])
    geocoder = Geocoder(db, [40.5, -75.6, 40.8, -75.1], session=session, min_interval_s=0)
    assert geocoder.lookup("1234 Northampton Street")["kind"] == "address"
    assert geocoder.lookup("1234 northampton street ")["lat"] == 40.6915
    session.rows = []
    assert geocoder.lookup("9 Nowhere Lane") is None
    assert geocoder.lookup("9 Nowhere Lane") is None
    assert session.calls == ["1234 Northampton Street", "9 Nowhere Lane"]


def test_offline_address_on_a_short_street_uses_the_street(tmp_path, gaz):
    db = Database(tmp_path / "calls.db")
    worker = MapWorker(Config(), db, gaz, None)
    point, precision = worker.place(gaz.find("respond 12 Short Lane in Wilson"), 0)
    assert precision == "street" and near(point, 40.6805, -75.2400, 80)
    # A long street is too vague without the online lookup.
    assert worker.place(gaz.find("respond 1234 Northampton Street"), 0) == (None, "address")


def test_online_answer_far_from_the_street_is_ignored(tmp_path, gaz):
    db = Database(tmp_path / "calls.db")
    session = FakeSession([{"lat": "40.75", "lon": "-75.40", "category": "place", "type": "house"}])
    worker = MapWorker(Config(), db, gaz, Geocoder(db, [40.5, -75.6, 40.8, -75.1], session=session,
                                                   min_interval_s=0))
    assert worker.place(gaz.find("respond 1234 Northampton Street"), 0)[0] is None


class Radio(FakeBackend):
    """Says each line in turn."""

    def __init__(self, lines):
        super().__init__()
        self.lines = list(lines)

    def transcribe(self, samples, prompt):
        return Transcript(self.lines.pop(0), -0.2)


def test_calls_become_incidents_on_the_map(tmp_path):
    (tmp_path / "places-geo.json").write_text(json.dumps(GEO), encoding="utf-8")
    config = Config()
    config.base_dir = tmp_path
    config.map.online_lookup = False
    radio = Radio([
        "Station 4 Engine, respond North Third and Northampton for a structure fire, smoke showing.",
        "Engine 41 on location, North 3rd and Northampton, working fire.",
        "Medic 49 responding Main and Center in Nazareth for a fall.",
        "Medic 49 10-8.",
    ])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        client.post("/api/page", json={"dateTime": now - 30, "toneA": 349.0, "toneB": 832.5, "label": "Station 4"})
        for offset, label in ((0, "County Dispatch"), (120, "Fireground 2"), (300, "EMS Dispatch"), (400, "EMS Dispatch")):
            post(client, sdrtrunk_body({**CALL, "dateTime": str(now + offset), "talkgroupLabel": label,
                                        "talkgroupGroup": ""}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        incidents = client.get("/api/incidents", params={"since": now - 3600}).json()
        status = client.get("/api/map/status").json()

        fire = next(i for i in incidents if "Northampton" in i["label"])
        hidden = client.delete(f"/api/incidents/{fire['id']}")
        after = client.get("/api/incidents", params={"since": now - 3600}).json()
        with_hidden = client.get("/api/incidents", params={"since": now - 3600, "hidden": True}).json()
        page = client.get("/map")

    assert status["has_shapes"] and status["streets"] >= 8
    assert len(incidents) == 2
    assert fire["label"] == "North 3rd Street & Northampton Street, Easton"
    assert len(fire["calls"]) == 2
    assert fire["category"] == "fire" and fire["nature"] == "Structure fire"
    assert [p["label"] for p in fire["pages"]] == ["Station 4"]
    ems = next(i for i in incidents if "Main" in i["label"])
    assert ems["category"] == "ems" and ems["nature"] == "Fall" and len(ems["calls"]) == 1
    assert fire["type"] == "fire" and ems["type"] == "ems"
    assert [fire["calls"][0]["transcript"][a:b] for a, b in fire["calls"][0]["highlights"]] == \
        ["structure fire", "smoke showing"]
    assert hidden.status_code == 200
    assert [i["id"] for i in after] == [ems["id"]]
    assert len(with_hidden) == 2
    assert page.status_code == 200 and "leaflet" in page.text


def test_calls_logged_before_the_street_list_are_mapped_later(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    config.map.online_lookup = False
    radio = Radio(["Crash on Route 22 at Freemansburg Ave."])
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        before = client.get("/api/incidents").json()
    assert before == []

    (tmp_path / "places-geo.json").write_text(json.dumps(GEO), encoding="utf-8")
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        assert client.app.state.map_worker.wait_idle()
        after = client.get("/api/incidents").json()
        # Rebuilding is only allowed from the PC itself; the test client isn't.
        assert client.post("/api/map/rebuild").status_code == 403
        client.app.state.map_worker.rebuild()
        assert client.app.state.map_worker.wait_idle()
        again = client.get("/api/incidents").json()
    assert after[0]["nature"] == "MVA" and after[0]["type"] == "mva"
    assert len(after) == 1 and after[0]["label"] == "Route 22 & Freemansburg Avenue, Palmer Township"
    assert len(again) == 1
