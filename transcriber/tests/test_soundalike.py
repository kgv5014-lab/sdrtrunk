"""Smart address lookup (soundalike.py): streets Whisper spelled by sound, checked against house numbers,
cross streets and towns."""

import json

import pytest

from sdrtrunk_transcriber import tiger
from sdrtrunk_transcriber.address_fix import AddressBook, AddressFixer
from sdrtrunk_transcriber.addresses import Gazetteer
from sdrtrunk_transcriber.incidents import MapWorker
from sdrtrunk_transcriber.soundalike import similarity, sound_key

from test_address_fix import box, census_zip, line


def column(lon, lat1, lat2, steps=4):
    return [[lat1 + (lat2 - lat1) * k / steps, lon] for k in range(steps + 1)]


SCHOENERSVILLE = line(40.640, -75.400, -75.380)
SHIMERVILLE = line(40.620, -75.400, -75.380)
WESTGATE = column(-75.390, 40.630, 40.650)
FREEMANSBURG = line(40.660, -75.330, -75.310)
OHIO = column(-75.320, 40.655, 40.665)
MACADA = line(40.670, -75.400, -75.380)
COAL = line(40.700, -75.230, -75.225)
COLE = line(40.560, -75.500, -75.495)
GEO = {
    "ways": [
        {"id": "way/1", "name": "Schoenersville Road", "highway": "secondary", "municipalities": ["Bethlehem"],
         "geometry": SCHOENERSVILLE},
        {"id": "way/2", "name": "Shimerville Road", "highway": "residential", "municipalities": ["Bethlehem"],
         "geometry": SHIMERVILLE},
        {"id": "way/3", "name": "Westgate Drive", "highway": "residential", "municipalities": ["Bethlehem"],
         "geometry": WESTGATE},
        {"id": "way/4", "name": "Freemansburg Avenue", "highway": "secondary",
         "municipalities": ["Bethlehem Township"], "geometry": FREEMANSBURG},
        {"id": "way/5", "name": "Ohio Street", "highway": "residential", "municipalities": ["Bethlehem Township"],
         "geometry": OHIO},
        {"id": "way/6", "name": "Macada Road", "highway": "residential", "municipalities": ["Bethlehem"],
         "geometry": MACADA},
        {"id": "way/7", "name": "Coal Street", "highway": "residential", "municipalities": ["Easton"],
         "geometry": COAL},
        {"id": "way/8", "name": "Cole Street", "highway": "residential", "municipalities": ["Allentown"],
         "geometry": COLE},
    ],
    "landmarks": [],
    "municipalities": [box("Bethlehem", 40.60, -75.42, 40.68, -75.36),
                       box("Bethlehem Township", 40.645, -75.35, 40.68, -75.30),
                       box("Easton", 40.67, -75.24, 40.71, -75.20),
                       box("Allentown", 40.55, -75.52, 40.58, -75.48)],
}
CENSUS = [
    ("Schoenersville Rd", "2501", "2599", "2500", "2598", SCHOENERSVILLE),
    ("Shimerville Rd", "2501", "2599", "2500", "2598", SHIMERVILLE),
    ("Westgate Dr", "1", "99", "2", "98", WESTGATE),
    ("Freemansburg Ave", "4001", "4199", "4000", "4198", FREEMANSBURG),
    ("Ohio St", "1", "99", "2", "98", OHIO),
    ("Macada Rd", "1701", "1799", "1700", "1798", MACADA),
    ("Coal St", "601", "699", "600", "698", COAL),
    ("Cole St", "1", "99", "2", "98", COLE),
]


@pytest.fixture
def fixer(tmp_path):
    geo = tmp_path / "places-geo.json"
    geo.write_text(json.dumps(GEO), encoding="utf-8")
    data, _ = tiger.build(tiger.read_zip(census_zip(tmp_path / "c.zip", CENSUS)), GEO)
    ranges = tmp_path / "address-ranges.json"
    ranges.write_text(json.dumps(data), encoding="utf-8")
    return AddressFixer(Gazetteer(geo, None), AddressBook(ranges))


def test_sound_keys_fold_what_radio_blurs():
    assert sound_key("Grimminsburg")[1:] == sound_key("Freemansburg")[1:]
    assert sound_key("Makeda") == sound_key("Macada")
    assert sound_key("Cole") == sound_key("Coal")
    assert similarity("shanersville", "schoenersville") > 0.8
    assert similarity("committed", "schoenersville") < 0.6


def test_cross_street_confirms_a_sound_alike(fixer):
    text = "Muhlenberg, 2545, Shanersville, Railroad, Cross of Westgate Drive, Bethlehem City Communications"
    fixed, heard = fixer.fix(text)
    assert fixed.startswith("Muhlenberg, 2545, Schoenersville Road, Cross of Westgate Drive")
    assert heard == ["2545, Shanersville, Railroad"]


def test_first_letter_wrong(fixer):
    text = "ALS, Unresponsive, 4100, Grimminsburg Avenue, Cross of Ohio Street, Bethlehem Township Communications"
    assert "4100, Freemansburg Avenue, Cross of Ohio Street" in fixer.fix(text)[0]


def test_direction_kept_and_house_number_checked(fixer):
    assert fixer.fix("Fall victim, 1745, West Makeda Road, 93-year-old female")[0] == \
        "Fall victim, 1745, West Macada Road, 93-year-old female"
    # Macada Road has no 9000: left alone.
    text = "Fall victim, 9000, West Makeda Road, 93-year-old female"
    assert fixer.fix(text) == (text, [])


def test_a_real_street_said_right_is_not_changed(fixer):
    text = "EMS to 20 Cole Street for a fall"
    assert fixer.fix(text) == (text, [])


def test_the_closest_of_two_that_fit(fixer):
    # 2545 is on both Schoenersville and Shimerville: "Shimersville" is plainly the second.
    assert fixer.fix("Respond to 2545 Shimersville Road for an alarm")[0] == \
        "Respond to 2545 Shimerville Road for an alarm"


def test_two_streets_that_fit_equally_change_nothing(fixer):
    text = "Respond to 2545 Shoemersville Road for an alarm"
    assert fixer.fix(text) == (text, [])


def test_radio_words_after_a_number_are_not_streets(fixer):
    for text in ("County, it's 52, 12, and 21 units, 10-4.", "496 Received.", "County 1755 for an ALS call.",
                 "I'll be 10 more minutes, I'll take 4100 more drive on this"):
        assert fixer.fix(text) == (text, [])


def test_without_a_street_word_it_needs_the_cross_street(fixer):
    text = "Walgreens 2545, Shanersville, 63-year-old female"
    assert fixer.fix(text) == (text, [])


# ---- the online search, town by town

class FakeGeocoder:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def lookup(self, query):
        self.asked.append(query)
        return self.answers.get(query)


def test_address_on_a_street_in_several_towns_is_searched_in_each(tmp_path):
    geo = {"ways": [
        {"id": "way/1", "name": "William Penn Highway", "highway": "primary", "municipalities": ["Palmer Township"],
         "geometry": line(40.676, -75.270, -75.250)},
        {"id": "way/2", "name": "William Penn Highway", "highway": "primary",
         "municipalities": ["Upper Macungie Township"], "geometry": line(40.600, -75.600, -75.580)},
    ], "landmarks": [], "municipalities": []}
    path = tmp_path / "geo.json"
    path.write_text(json.dumps(geo), encoding="utf-8")
    gazetteer = Gazetteer(path, None)
    found = gazetteer.find("Easton Area High School, 2601 William Penn Highway")
    worker = MapWorker.__new__(MapWorker)
    worker.gazetteer = gazetteer
    worker.geocoder = FakeGeocoder({
        "2601 William Penn Highway, Palmer Township, Pennsylvania":
            {"lat": 40.6762, "lon": -75.26, "kind": "address", "display": ""},
    })
    assert worker.in_one_town(found) == (40.6762, -75.26)
    # Both towns answering with a house: no way to tell, so no pin.
    worker.geocoder.answers["2601 William Penn Highway, Upper Macungie Township, Pennsylvania"] = \
        {"lat": 40.6002, "lon": -75.59, "kind": "address", "display": ""}
    assert worker.in_one_town(found) is None
