"""Local street list: Whisper hints, street-name cleanup, and building the list from OpenStreetMap."""

import csv
import json
import shutil
from pathlib import Path

import pytest

from sdrtrunk_transcriber import fetch_places
from sdrtrunk_transcriber.places import PlaceBook, core_name, route_number, spoken_ordinal
from sdrtrunk_transcriber.worker import build_prompt

EXAMPLE = Path(__file__).resolve().parents[1] / "places.example.csv"


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "places.csv"
    shutil.copy(EXAMPLE, path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write("South 25th Street,street,Palmer Township,,yes,70,,manual\n"
                "Main Street,street,Easton,,,20,,manual\n"
                "Maine Street,street,Allentown,,,10,,manual\n"
                "Oak Lane,street,Easton,,,10,,manual\n")
    return PlaceBook(path, prompt_chars=120)


@pytest.mark.parametrize("words,number", [
    ("second", 2), ("twenty second", 22), ("twenty-fifth", 25), ("thirteenth", 13), ("thirtieth", 30),
    ("twenty", None),
])
def test_spoken_ordinal(words, number):
    assert spoken_ordinal(words) == number


@pytest.mark.parametrize("words,number", [
    ("22", 22), ("twenty two", 22), ("two forty eight", 248), ("five twelve", 512), ("six eleven", 611),
    ("three seventy eight", 378), ("seventy eight", 78), ("two two", None),
])
def test_route_number(words, number):
    assert route_number(words) == number


@pytest.mark.parametrize("heard,written", [
    # Spoken numbers on streets and routes, written the way dispatch writes them.
    ("Engine 2011 responding to twenty fifth street", "Engine 2011 responding to 25th Street"),
    ("1201 Twenty-Second St.", "1201 22nd St."),
    ("crash on route twenty two westbound", "crash on Route 22 westbound"),
    ("route two forty eight at the bridge", "Route 248 at the bridge"),
    ("interstate seventy eight mile marker 71", "I-78 mile marker 71"),
    ("Route 22 already written", "Route 22 already written"),
    # Near misses fixed against the list.
    ("1201 Northamton Street, smoke showing", "1201 Northampton Street, smoke showing"),
    ("at Larry Holms Drive and Northampton St", "at Larry Holmes Drive and Northampton St"),
    ("Freemansberg Ave. by the school", "Freemansburg Avenue by the school"),
    ("on Stefco Boulevard", "on Stefko Boulevard"),
    # Spoken alias from the list.
    ("units to the circle for a fall", "units to Centre Square for a fall"),
    # Left alone: exact names, short names, unknown streets, everyday speech.
    ("123 Main Street", "123 Main Street"),
    ("Oak Lane and Elm Lane", "Oak Lane and Elm Lane"),
    ("Cherrywood Drive", "Cherrywood Drive"),
    ("en route twenty minutes out", "en route twenty minutes out"),
    ("first floor, second street on the left", "first floor, 2nd Street on the left"),
    ("on the way", "on the way"),
])
def test_normalize(book, heard, written):
    assert book.normalize(heard) == written


def test_unknown_streets_close_to_a_listed_one_are_fixed_only_when_long_enough(book):
    assert book.normalize("Cider Street") == "Cider Street"  # no Cedar Street in the list
    assert book.normalize("Hamiltun Street") == "Hamilton Street"


def test_near_misses_prefer_the_talkgroups_town(tmp_path):
    path = tmp_path / "places.csv"
    path.write_text("name,kind,municipality,rank\n"
                    "Walnut Street,street,Easton,10\n"
                    "Walnuts Street,street,Allentown,50\n", encoding="utf-8")
    book = PlaceBook(path)
    assert book.normalize("Walnutt Street", "Easton PD") == "Walnut Street"


def test_prompt_picks_the_talkgroups_town_first(book):
    terms = book.prompt_terms("Easton PD", "Police")
    prompt = build_prompt(terms, ["10-4"], ["Medic 2081"])
    # Most important last; Easton's streets outrank the bigger roads elsewhere.
    assert terms[-1] == "Northampton Street"
    assert "Larry Holmes Drive" in terms and "Hamilton Street" not in terms
    assert sum(len(t) + 2 for t in terms) <= 120
    assert prompt.endswith("10-4, Medic 2081")


def test_prompt_without_a_town_uses_the_biggest_places(book):
    terms = book.prompt_terms("County Fire Dispatch")
    assert "Route 22" in terms and "I-78" in terms
    assert "Main Street" not in terms  # not marked prompt


def test_config_areas_map_a_talkgroup_to_towns(tmp_path):
    shutil.copy(EXAMPLE, tmp_path / "places.csv")
    book = PlaceBook(tmp_path / "places.csv", prompt_chars=60, areas={"Station 20": ["Bethlehem"]})
    assert book.areas_for("Station 20 Fire") == ["bethlehem"]
    # A name that isn't in the list matches on its core name, so this covers the township too.
    both = PlaceBook(tmp_path / "places.csv", areas={"Station 20": ["City of Bethlehem"]})
    assert sorted(both.areas_for("Station 20 Fire")) == ["bethlehem", "bethlehem township"]
    assert sorted(book.areas_for("Bethlehem PD")) == ["bethlehem", "bethlehem township"]
    assert book.prompt_terms("Station 20 Fire")[-1] == "Broad Street"


def test_missing_file_is_harmless(tmp_path):
    book = PlaceBook(tmp_path / "nope.csv")
    assert book.prompt_terms("Easton PD") == []
    assert book.normalize("route twenty two") == "Route 22"


@pytest.mark.parametrize("name,core", [
    ("City of Bethlehem", "bethlehem"), ("Palmer Township", "palmer"), ("Wilson", "wilson"),
    ("Borough of Freemansburg", "freemansburg"),
])
def test_core_name(name, core):
    assert core_name(name) == core


# --- Building the list from OpenStreetMap -----------------------------------------------------------

def square(name, south, west, north, east, osm_id):
    ring = [(south, west), (south, east), (north, east), (north, west), (south, west)]
    return {"type": "relation", "id": osm_id, "tags": {"name": name},
            "members": [{"type": "way", "role": "outer",
                         "geometry": [{"lat": a, "lon": b} for a, b in ring[:3]]},
                        {"type": "way", "role": "outer",
                         "geometry": [{"lat": a, "lon": b} for a, b in ring[2:]]}]}


def line(osm_id, tags, *points):
    return {"type": "way", "id": osm_id, "tags": tags, "geometry": [{"lat": a, "lon": b} for a, b in points]}


TOWNS = {"elements": [square("Easton", 40.66, -75.24, 40.70, -75.19, 1),
                      square("Palmer Township", 40.70, -75.30, 40.76, -75.19, 2)]}
ROADS = {"elements": [
    line(10, {"highway": "primary", "name": "Northampton Street", "ref": "PA 611"}, (40.69, -75.23), (40.69, -75.20)),
    line(11, {"highway": "residential", "name": "Oak Lane"}, (40.67, -75.22), (40.671, -75.22)),
    line(12, {"highway": "secondary", "name": "Freemansburg Avenue"}, (40.68, -75.22), (40.72, -75.25)),
]}
ROUTES = {"elements": [
    ROADS["elements"][0],
    line(20, {"highway": "trunk", "ref": "US 22;PA 33"}, (40.71, -75.29), (40.71, -75.20)),
    line(21, {"highway": "motorway", "ref": "I 78"}, (40.60, -75.40), (40.60, -75.20)),
]}
LANDMARKS = {"elements": [
    {"type": "way", "id": 30, "tags": {"amenity": "hospital", "name": "St. Luke's Easton Campus"},
     "center": {"lat": 40.68, "lon": -75.21}},
    {"type": "node", "id": 31, "tags": {"leisure": "park", "name": "Hackett Park"}, "lat": 40.675, "lon": -75.23},
]}


def test_towns_lookup():
    towns = fetch_places.Towns(TOWNS["elements"])
    assert towns.find(40.69, -75.21) == "Easton"
    assert towns.find(40.73, -75.25) == "Palmer Township"
    assert towns.find(40.60, -75.30) is None


def test_route_names():
    assert fetch_places.route_names("US 22;PA 33") == ["Route 22", "Route 33"]
    assert fetch_places.route_names("I 78") == ["I-78"]
    assert fetch_places.route_names("SR 2012") == []


def test_build_ranks_and_places_everything():
    rows, geo = fetch_places.build(ROADS, ROUTES, LANDMARKS, TOWNS)
    found = {(r["name"], r["municipality"]): r for r in rows}

    assert ("Freemansburg Avenue", "Easton") in found and ("Freemansburg Avenue", "Palmer Township") in found
    assert found[("Northampton Street", "Easton")]["rank"] > found[("Oak Lane", "Easton")]["rank"]
    assert {"Route 22", "Route 33", "Route 611", "I-78"} <= {r["name"] for r in rows if r["kind"] == "route"}
    assert found[("I-78", "")]["prompt"] == "yes"
    assert found[("St. Luke's Easton Campus", "Easton")]["kind"] == "landmark"
    assert found[("St. Luke's Easton Campus", "Easton")]["osm_id"] == "way/30"
    assert all(r["source"] == "osm" for r in rows)

    assert len(geo["ways"]) == 5  # the named route isn't listed twice
    northampton = next(w for w in geo["ways"] if w["id"] == "way/10")
    assert northampton["municipalities"] == ["Easton"] and len(northampton["geometry"]) == 2
    assert {l["name"] for l in geo["landmarks"]} == {"St. Luke's Easton Campus", "Hackett Park"}
    assert {m["name"] for m in geo["municipalities"]} == {"Easton", "Palmer Township"}


def test_main_writes_both_files_and_keeps_hand_made_rows(tmp_path, monkeypatch):
    out = tmp_path / "places.csv"
    out.write_text("name,kind,municipality,spoken,prompt,rank,osm_id,source\n"
                   "Centre Square,landmark,Easton,the Circle,yes,70,,Ken\n"
                   "Route 22,route,,,yes,100,,example\n"
                   "Old Road,street,Easton,,,5,way/1,osm\n", encoding="utf-8")
    answers = {"roads": ROADS, "routes": ROUTES, "landmarks": LANDMARKS, "towns": TOWNS}
    asked = []

    def fake_overpass(query, url):
        name = next(n for n, q in fetch_places.queries(fetch_places.DEFAULT_BBOX).items() if q == query)
        asked.append(name)
        return answers[name]

    monkeypatch.setattr(fetch_places, "overpass", fake_overpass)
    assert fetch_places.main(["--out", str(out), "--geo", str(tmp_path / "geo.json")]) == 0

    with open(out, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["name"] == "Centre Square" and rows[0]["source"] == "Ken"
    assert "Old Road" not in {r["name"] for r in rows}
    assert sum(1 for r in rows if r["name"] == "Route 22") == 1
    assert sorted(asked) == ["landmarks", "roads", "routes", "towns"]
    assert json.loads((tmp_path / "geo.json").read_text())["ways"]

    book = PlaceBook(out)
    assert book.normalize("smoke at Northamton Street") == "smoke at Northampton Street"
