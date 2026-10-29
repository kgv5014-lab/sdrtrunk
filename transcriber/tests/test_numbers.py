"""Telling unit numbers, times, house numbers, apartments, years and 911 apart."""

from datetime import datetime

import pytest

from sdrtrunk_transcriber.codes import CodeBook
from sdrtrunk_transcriber.numbers import read_numbers
from sdrtrunk_transcriber.places import PlaceBook


@pytest.fixture
def places(tmp_path):
    path = tmp_path / "places.csv"
    path.write_text("name,kind,municipality,spoken,prompt,rank,osm_id,source\n"
                    "Butler Street,street,Easton,,,10,,osm\n"
                    "Bally Drive,street,Allen Township,,,10,,osm\n"
                    "County Road,street,Easton,,,10,,osm\n"
                    "Janet Avenue,street,Palmer Township,,,10,,osm\n", encoding="utf-8")
    return PlaceBook(path)


@pytest.fixture
def county(tmp_path, places):
    path = tmp_path / "codes.csv"
    path.write_text(
        "type,code,meaning,applies_to,prompt\n"
        "unit-suffix,81-89,Ambulance,ems,\n"
        "unit-suffix,51-59,Fire officer,fire,\n"
        "station,13,Hellertown Fire/EMS,fire-ems,\n"
        "station,20,Easton Fire/EMS,fire-ems,\n"
        "station,28,Williams Twp Fire,fire-ems,\n"
        "police-esn,13,Hellertown PD,police,\n"
        "police-esn,20,Easton PD,police,\n"
        "police-esn,27,Palmer Twp PD,police,\n"
        "police-esn,91,Bangor Area SD Police,police,\n", encoding="utf-8")
    return CodeBook(path, streets=places.street_at)


def kinds(text, places=None):
    return [(n.text, n.kind) for n in read_numbers(text, places.street_at if places else None)]


@pytest.mark.parametrize("text,expected", [
    ("1360 Butler Street, 1360 Butler", [("1360", "address"), ("1360", "address")]),
    ("2056, Bally Drive, Allen", [("2056", "address")]),
    ("631, South 25th Street", [("631", "address")]),
    ("1200 block of Main", [("1200", "address")]),
    ("223 Brother Thomas Bright Avenue", [("223", "address")]),
    ("650 Northampton St. Apartment 204", [("650", "address"), ("204", "apartment")]),
    ("Room 516, Alpha", [("516", "apartment")]),
    ("he's in a 2026 White Ford", [("2026", "year")]),
    ("comes back on the 2016 Jeep", [("2016", "year")]),
    ("expires March of 2027", [("2027", "year")]),
    ("a 911 hang-up", [("911", "911")]),
    ("911 open line from the school", [("911", "911")]),
    ("County, 2851, on scene", [("2851", "unit")]),
    ("1735 County, this is unit only", [("1735", "unit")]),
    ("County from 2015.", [("2015", "unit")]),
    ("2851 responding", [("2851", "unit")]),
    # Not enough to tell
    ("1385, St. Luke's", [("1385", None)]),
    ("1346", [("1346", None)]),
    # Not numbers this reads: 10-codes, written times, numbers with thousands commas
    ("10-23 at 13:46, 21,876", []),
])
def test_what_each_number_is(text, expected):
    assert kinds(text) == expected


def test_a_known_street_without_its_street_word(places):
    assert kinds("1360 Butler, there's smoke", places) == [("1360", "address")]
    # Only right after the number: after a comma it's a unit and a name
    assert kinds("2714, Janet, are you clear", places) == [("2714", None)]
    # Radio words that are also street names don't count
    assert kinds("1224 County Traffic", places) == [("1224", "unit")]


def test_units_are_not_labelled_on_addresses_years_and_911(county):
    labels = county.annotate("2741 respond to 2045 Butler Street, 2045 Butler, a 2013 white Honda, "
                             "a 911 hang-up, apartment 2712", "Co PD E1 Patch")
    assert [(l["code"], l["meaning"]) for l in labels] == [("2741", "Palmer Twp PD, car 41")]
    labels = county.annotate("County, 2851, 2056, Bally Drive", "Countywide")
    assert [(l["code"], l["meaning"]) for l in labels] == [("2851", "Williams Twp Fire, Fire officer")]


def test_a_unit_calling_county_is_not_the_time(county):
    at = datetime(2026, 10, 28, 17, 35, 20)
    assert county.normalize("1735 County, I'm clear", at) == "1735 County, I'm clear"
    assert county.normalize("County, 1735, on scene", at) == "County, 1735, on scene"
    # Dispatch signing off still gets the time
    assert county.normalize("Easton Communications, Ops 2, 1735.", at) == "Easton Communications, Ops 2, 17:35."
    assert county.normalize("4751 at 1735, 4816 Timberline Road", at) == "4751 at 17:35, 4816 Timberline Road"
    assert county.normalize("on scene 1735", at) == "on scene 17:35"


def test_house_numbers_are_not_the_time(county):
    at = datetime(2026, 10, 28, 13, 57)
    assert county.normalize("Liza's house, 1357 Blue Mountain Drive", at) == "Liza's house, 1357 Blue Mountain Drive"
    assert county.normalize("1357, Blue Mountain Drive", at) == "1357, Blue Mountain Drive"


@pytest.mark.parametrize("text,meaning", [
    ("Clear at 11:16 PM.", "11:16 PM"),
    ("10-4, 12:27 AM.", "12:27 AM"),
    ("Ops 1, 6:49 PM", "6:49 PM"),
    ("Ops 1, 13:46", "1:46 PM"),
])
def test_times_already_in_twelve_hour_form_keep_am_and_pm(county, text, meaning):
    assert [l["meaning"] for l in county.annotate(text, "Countywide") if l["kind"] == "time"] == [meaning]
