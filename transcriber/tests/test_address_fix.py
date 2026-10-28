"""Address autocorrect: Census house-number ranges (tiger.py) and snapping misheard addresses (address_fix.py)."""

import io
import json
import struct
import zipfile

import pytest

from sdrtrunk_transcriber import fetch_places, tiger
from sdrtrunk_transcriber.address_fix import AddressBook, AddressFixer, number_slips
from sdrtrunk_transcriber.addresses import Gazetteer
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.incidents import MapWorker


def line(lat, lon1, lon2, steps=4):
    return [[lat, lon1 + (lon2 - lon1) * k / steps] for k in range(steps + 1)]


def box(name, south, west, north, east):
    corners = [[south, west], [south, east], [north, east], [north, west], [south, west]]
    return {"name": name, "bbox": [south, west, north, east], "lines": [[a, b] for a, b in zip(corners, corners[1:])]}


NORTHAMPTON = line(40.690, -75.230, -75.210)
FERRY = line(40.686, -75.230, -75.210)
FERRE = line(40.682, -75.230, -75.225)
MAIN_EASTON = line(40.694, -75.230, -75.225)
MAIN_ALLENTOWN = line(40.600, -75.480, -75.460)
GEO = {
    "ways": [
        {"id": "way/1", "name": "Northampton Street", "highway": "secondary", "municipalities": ["Easton"],
         "geometry": NORTHAMPTON},
        {"id": "way/2", "name": "Ferry Street", "highway": "residential", "municipalities": ["Easton"], "geometry": FERRY},
        {"id": "way/3", "name": "Ferre Street", "highway": "residential", "municipalities": ["Easton"], "geometry": FERRE},
        {"id": "way/4", "name": "Main Street", "highway": "residential", "municipalities": ["Easton"],
         "geometry": MAIN_EASTON},
        {"id": "way/5", "name": "Main Street", "highway": "residential", "municipalities": ["Allentown"],
         "geometry": MAIN_ALLENTOWN},
    ],
    "landmarks": [],
    "municipalities": [box("Easton", 40.67, -75.24, 40.71, -75.20), box("Allentown", 40.55, -75.52, 40.65, -75.42)],
}
# Census pieces: name, left from/to, right from/to, points.
CENSUS = [
    ("Northampton St", "101", "999", "100", "998", NORTHAMPTON),
    ("Ferry St", "601", "999", "600", "998", FERRY),
    ("Ferre St", "1", "7", "2", "8", FERRE),
    ("Main St", "1", "299", "2", "298", MAIN_EASTON),
    ("Main St", "1", "1999", "2", "1998", MAIN_ALLENTOWN),
    ("Nowhere Rd", "1", "99", "2", "98", line(40.700, -75.500, -75.490)),   # no map road under it
    ("Ferry St", "", "", "", "", FERRY),                                     # no house numbers
]


# ---- a tiny shapefile writer, for the reader

FIELDS = ["FULLNAME", "LFROMHN", "LTOHN", "RFROMHN", "RTOHN"]


def dbf(rows):
    width = 40
    header = struct.pack("<BBBBIHH20x", 3, 26, 1, 1, len(rows), 32 + 32 * len(FIELDS) + 1, 1 + width * len(FIELDS))
    fields = b"".join(name.encode().ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([width]) + b"\0" * 15
                      for name in FIELDS)
    records = b"".join(b" " + b"".join(str(v).encode().ljust(width) for v in row) for row in rows)
    return header + fields + b"\r" + records + b"\x1a"


def shp(lines):
    records = b""
    for n, points in enumerate(lines, 1):
        lons, lats = [p[1] for p in points], [p[0] for p in points]
        content = struct.pack("<i4d2i", 3, min(lons), min(lats), max(lons), max(lats), 1, len(points))
        content += struct.pack("<i", 0) + b"".join(struct.pack("<2d", p[1], p[0]) for p in points)
        records += struct.pack(">2i", n, len(content) // 2) + content
    header = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, (100 + len(records)) // 2) + struct.pack("<2i4d4d", 1000, 3, *[0] * 8)
    return header + records


def census_zip(path, pieces=CENSUS):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("tl_2024_42095_addrfeat.dbf", dbf([p[:5] for p in pieces]))
        z.writestr("tl_2024_42095_addrfeat.shp", shp([p[5] for p in pieces]))
    return path


@pytest.fixture
def files(tmp_path):
    geo = tmp_path / "places-geo.json"
    geo.write_text(json.dumps(GEO), encoding="utf-8")
    ranges = tmp_path / "address-ranges.json"
    zip_path = census_zip(tmp_path / "tl_2024_42095_addrfeat.zip")
    data, _counts = tiger.build(tiger.read_zip(zip_path), GEO)
    ranges.write_text(json.dumps(data), encoding="utf-8")
    return geo, ranges


@pytest.fixture
def fixer(files):
    geo, ranges = files
    return AddressFixer(Gazetteer(geo, None), AddressBook(ranges))


# ---- reading and matching the Census files

def test_shapefile_is_read_back(tmp_path):
    features = tiger.read_zip(census_zip(tmp_path / "c.zip"))
    assert len(features) == len(CENSUS)
    row, points = features[0]
    assert row["FULLNAME"] == "Northampton St" and row["LTOHN"] == "999"
    assert points[0] == pytest.approx((40.690, -75.230))


def test_census_pieces_take_the_map_names_and_towns(tmp_path):
    data, counts = tiger.build(tiger.read_zip(census_zip(tmp_path / "c.zip")), GEO)
    by = {(s["name"], s["town"]): s for s in data["streets"]}
    assert ("Northampton Street", "Easton") in by
    assert ("Main Street", "Easton") in by and ("Main Street", "Allentown") in by
    assert by[("Northampton Street", "Easton")]["segments"][0][:4] == [101, 999, 100, 998]
    assert counts["unmatched"] == 1 and counts["no_numbers"] == 1


@pytest.mark.parametrize("census,osm", [("State Rte 248", "Route 248"), ("US Hwy 22", "Route 22"),
                                        ("I- 78", "I-78"), ("N 13th St", "N 13th St")])
def test_census_route_names(census, osm):
    assert tiger.clean_name(census) == osm


def test_fetch_writes_the_ranges(tmp_path, monkeypatch):
    zip_path = census_zip(tmp_path / "tl_2024_42095_addrfeat.zip")
    monkeypatch.setattr(tiger, "download", lambda county, cache: zip_path)
    out = tmp_path / "address-ranges.json"
    assert tiger.fetch(GEO, out, tmp_path, counties=["42095"])
    assert AddressBook(out).has("Northampton Street", 550)


def test_failed_census_download_keeps_the_old_ranges(tmp_path, monkeypatch):
    def fail(county, cache):
        raise tiger.CensusError("no")
    monkeypatch.setattr(tiger, "download", fail)
    out = tmp_path / "address-ranges.json"
    out.write_text("old", encoding="utf-8")
    assert not tiger.fetch(GEO, out, tmp_path, counties=["42095"])
    assert out.read_text() == "old"


def test_addresses_only_uses_the_saved_map_file(tmp_path, monkeypatch):
    zip_path = census_zip(tmp_path / "tl_2024_42095_addrfeat.zip")
    monkeypatch.setattr(tiger, "download", lambda county, cache: zip_path)
    monkeypatch.setattr(fetch_places, "overpass", lambda *a: pytest.fail("OpenStreetMap was asked"))
    geo = tmp_path / "geo.json"
    geo.write_text(json.dumps(GEO), encoding="utf-8")
    out = tmp_path / "ranges.json"
    assert fetch_places.main(["--addresses-only", "--geo", str(geo), "--ranges", str(out)]) == 0
    assert json.loads(out.read_text())["streets"]


# ---- the ranges

def test_house_numbers_and_sides(files):
    book = AddressBook(files[1])
    assert book.has("Northampton Street", 550) and book.has("Northampton St", 551)
    assert not book.has("Northampton Street", 5500)
    assert book.has("Main Street", 1500) and not book.has("Main Street", 1500, {"easton"})
    assert book.has("Northampton Street", 5000 // 10, block=True)


def test_house_number_is_placed_along_its_street(files):
    lat, lon = AddressBook(files[1]).locate("Northampton Street", 550)
    assert lat == pytest.approx(40.690) and lon == pytest.approx(-75.220, abs=0.0005)


def test_same_number_in_two_towns_needs_the_town(files):
    book = AddressBook(files[1])
    assert book.locate("Main Street", 100) is None
    assert book.locate("Main Street", 100, {"allentown"})[0] == pytest.approx(40.600)


def test_no_ranges_file(tmp_path):
    book = AddressBook(tmp_path / "missing.json")
    assert not book.loaded and not book.has("Main Street", 1)


# ---- fixing transcripts

def test_number_slips():
    slips = number_slips("516")
    assert slips["560"] == 1             # "five sixteen" for "five sixty"
    assert slips["16"] == 2 and slips["56"] == 2 and slips["156"] == 2 and slips["5160"] == 2
    assert "016" not in slips


def test_real_address_is_left_alone(fixer):
    text = "Engine 4 respond to 550 Northampton Street for the alarm"
    assert fixer.fix(text) == (text, [])


def test_extra_digit_is_dropped(fixer):
    assert fixer.fix("Respond to 1700 Ferry Street, Easton.") == ("Respond to 700 Ferry Street, Easton.",
                                                                  ["1700 Ferry Street"])


def test_teen_heard_for_ty(fixer):
    # Ferry Street starts at 600: "one sixty" was "six hundred"... no, "sixteen" for "sixty": 160 -> 600.
    assert fixer.fix("160 Ferry Street")[0] == "600 Ferry Street"


def test_two_possible_numbers_are_left_alone(fixer):
    text = "Medical call at 5500 Northampton Street"
    assert fixer.fix(text) == (text, [])     # 500 or 550


def test_incident_on_the_map_settles_a_tie(files):
    geo, ranges = files
    book = AddressBook(ranges)
    west_end = book.locate("Northampton Street", 100)
    fixer = AddressFixer(Gazetteer(geo, None), book, lambda t: [west_end])
    # 100, 900 or 910: only 100 is near the incident already on the map.
    assert fixer.fix("Medical call at 9100 Northampton Street", call_time=1000)[0] == \
        "Medical call at 100 Northampton Street"
    assert AddressFixer(Gazetteer(geo, None), book).fix("9100 Northampton Street")[1] == []


def test_number_somewhere_on_the_street_is_never_fixed(fixer):
    # Main Street in Easton stops at 299, but Allentown's has 1500.
    text = "1500 Main Street"
    assert fixer.fix(text, "County Fire") == (text, [])


def test_misspelt_street_with_the_number(fixer):
    assert fixer.fix("Respond to 820 Fery Street") == ("Respond to 820 Ferry Street", ["820 Fery Street"])


def test_similar_street_when_no_number_fits(fixer):
    # Ferre Street has 1 to 8; Ferry Street has 820.
    assert fixer.fix("820 Ferre Street")[0] == "820 Ferry Street"


def test_unit_numbers_and_ten_codes_are_not_house_numbers(fixer):
    for text in ("Medic 2081 Northampton Street", "10-4 Northampton Street", "Engine 5500 Northampton Street"):
        assert fixer.fix(text) == (text, [])


def test_block_of(fixer):
    assert fixer.fix("5000 block of Northampton Street")[0] == "500 block of Northampton Street"


def test_street_as_written_is_kept(fixer):
    assert fixer.fix("1700 Ferry St.")[0] == "700 Ferry St."


def test_nothing_changes_without_ranges(files, tmp_path):
    fixer = AddressFixer(Gazetteer(files[0], None), AddressBook(tmp_path / "none.json"))
    assert fixer.fix("1700 Ferry Street") == ("1700 Ferry Street", [])


# ---- the map

def test_map_pin_from_the_ranges(files, tmp_path):
    geo, ranges = files
    gazetteer = Gazetteer(geo, None)
    worker = MapWorker(Config(), Database(tmp_path / "calls.db"), gazetteer, None, AddressBook(ranges))
    found = gazetteer.find("Respond to 550 Northampton Street")
    point, precision = worker.place(found, 1000)
    assert precision == "address" and point[1] == pytest.approx(-75.220, abs=0.0005)


def test_worker_saves_what_the_address_was_heard_as(files, tmp_path):
    from sdrtrunk_transcriber import worker as worker_module
    db = Database(tmp_path / "calls.db")
    assert "address_fix" in {c[1] for c in db._conn.execute("PRAGMA table_info(calls)")}
    assert worker_module.heard_note(["1700 Ferry Street"]) == '"1700 Ferry Street"'
