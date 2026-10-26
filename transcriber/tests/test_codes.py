"""Local code list: Whisper hints, spoken-code cleanup and labels."""

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from sdrtrunk_transcriber.codes import CodeBook, spoken_number
from sdrtrunk_transcriber.worker import build_prompt

EXAMPLE = Path(__file__).resolve().parents[1] / "codes.example.csv"


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "codes.csv"
    shutil.copy(EXAMPLE, path)
    return CodeBook(path)


@pytest.mark.parametrize("words,number", [
    ("four", 4), ("twenty three", 23), ("twenty-three", 23), ("seventeen", 17), ("ninety nine", 99),
    ("23", 23), ("twenty", 20), ("banana", None), ("twenty seventeen", None),
])
def test_spoken_number(words, number):
    assert spoken_number(words) == number


@pytest.mark.parametrize("heard,written", [
    ("Ten four, Medic 49.", "10-4, Medic 49."),
    ("Engine 2011 is ten twenty three.", "Engine 2011 is 10-23."),
    ("ten-seventeen to the hospital", "10-17 to the hospital"),
    ("10 8 from the station", "10-8 from the station"),
    ("Show me 10, 23", "Show me 10-23"),
    ("We need a signal thirty three", "We need a Signal 33"),
    ("10-4 already written", "10-4 already written"),
    # Not codes, or not in the list: left alone.
    ("about ten minutes out", "about ten minutes out"),
    ("ten fifty eight", "ten fifty eight"),
    ("1023 Main Street", "1023 Main Street"),
    ("Engine 2011", "Engine 2011"),
])
def test_normalize(book, heard, written):
    assert book.normalize(heard) == written


def test_annotate_codes_and_call_signs(book):
    labels = book.annotate("2081 is 10-23 at 1201 Main Street, 10-4, Signal 33", "Fire Dispatch")
    assert [(l["code"], l["meaning"]) for l in labels] == [
        ("2081", "Easton Fire/EMS, Ambulance"),
        ("10-23", "Arrived on scene"),
        ("10-4", "Message received / acknowledged"),
        ("Signal 33", "Officer needs help quickly"),
    ]


def test_annotate_ignores_unknown_stations_and_suffixes(book):
    # 55 is not a listed station; 2000 has unit 00, which is no unit type.
    assert book.annotate("5581 and 2000 responding") == []


def test_agency_picks_the_matching_meaning(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_text(
        "type,code,meaning,applies_to,prompt\n"
        "ten-code,10-50,Vehicle accident,police,\n"
        "ten-code,10-50,Fire under control,fire,\n", encoding="utf-8")
    book = CodeBook(path)
    assert book.annotate("10-50", "Police Main")[0]["meaning"] == "Vehicle accident"
    assert book.annotate("10-50", "Fire Dispatch")[0]["meaning"] == "Fire under control"
    assert book.annotate("10-50")[0]["meaning"] == "Vehicle accident"


def test_edits_apply_without_restart(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_text("type,code,meaning\nten-code,10-4,OK\n", encoding="utf-8")
    book = CodeBook(path)
    assert book.annotate("10-4")[0]["meaning"] == "OK"
    time.sleep(0.01)
    path.write_text("type,code,meaning\nten-code,10-4,Acknowledged\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert book.annotate("10-4")[0]["meaning"] == "Acknowledged"


def test_excel_cp1252_file_still_loads(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_bytes("type,code,meaning\nten-code,10-20,Location – where are you\n".encode("cp1252"))
    assert CodeBook(path).annotate("10-20")[0]["meaning"] == "Location – where are you"


def test_missing_or_disabled_file_means_no_codes(tmp_path):
    for book in (CodeBook(tmp_path / "nope.csv"), CodeBook(None)):
        assert book.prompt_terms() == []
        assert book.normalize("ten four") == "ten four"
        assert book.annotate("10-4 2081") == []


def test_prompt_lists_marked_codes_then_vocabulary(book):
    terms = book.prompt_terms()
    assert "10-4" in terms and "10-23" in terms and "Signal 33" in terms
    assert "10-1" not in terms
    prompt = build_prompt(terms, ["Medic 49", "Route 22"])
    assert prompt.endswith("Medic 49, Route 22")


def test_long_prompt_drops_the_earliest_terms():
    prompt = build_prompt([f"code {i}" for i in range(200)], ["Medic 49"])
    assert len(prompt) <= 800
    assert prompt.endswith("Medic 49")
    assert "code 0," not in prompt


AT_1346 = datetime(2026, 10, 22, 13, 46, 20)


@pytest.mark.parametrize("heard,written", [
    ("Received at 1346.", "Received at 13:46."),
    ("time out 13 46", "time out 13:46"),
    ("on scene 1344", "on scene 13:44"),          # within 3 minutes
    ("13:46 already written", "13:46 already written"),
    ("dispatched at 1330 today", "dispatched at 1330 today"),  # too far from the call time
    ("Engine 1346 responding", "Engine 1346 responding"),
    ("1346 Main Street", "1346 Main Street"),
    ("ten twenty three", "10-23"),
])
def test_dispatch_times_near_the_call_become_clock_times(book, heard, written):
    assert book.normalize(heard, AT_1346) == written


def test_times_wrap_around_midnight(book):
    assert book.normalize("time 2359", datetime(2026, 10, 23, 0, 1)) == "time 23:59"


def test_ten_code_digits_win_over_a_matching_time(book):
    assert book.normalize("10 23", datetime(2026, 10, 22, 10, 23)) == "10-23"


def test_time_window_zero_turns_it_off(tmp_path):
    shutil.copy(EXAMPLE, tmp_path / "codes.csv")
    assert CodeBook(tmp_path / "codes.csv", time_window_minutes=0).normalize("at 1346", AT_1346) == "at 1346"


def test_times_are_labelled_in_twelve_hour_form(book):
    labels = book.annotate("Received at 13:46, 2081 en route")
    assert labels[0] == {"code": "13:46", "meaning": "1:46 PM", "kind": "time"}
    assert labels[1]["code"] == "2081"


@pytest.mark.parametrize("heard,hour,minute,written", [
    ("Color Town Communications, Ops 2, 1330.", 13, 31, "Color Town Communications, Ops 2, 13:30."),
    ("Northampton City, 14-16.", 14, 16, "Northampton City, 14:16."),
    ("Bethlehem City Communications, EMS, Dispatch 1, 14, 11.", 14, 11,
     "Bethlehem City Communications, EMS, Dispatch 1, 14:11."),
    ("Communications, Ops 2, 13.04 hours.", 13, 5, "Communications, Ops 2, 13:04 hours."),
    ("I found Town 2 Communications, Ops 113-30.", 13, 33, "I found Town 2 Communications, Ops 1, 13:30."),
    ("Kirkland Village, 2000, 14-16.", 14, 16, "Kirkland Village, 2000, 14:16."),
    ("Easton Communications, Ops 2, 10-28.", 10, 28, "Easton Communications, Ops 2, 10:28."),
    # Left alone: a 10-code said by a unit, too long before the call, or an address
    ("Engine 11 is 10-23.", 10, 24, "Engine 11 is 10-23."),
    ("Communications, Ops 2, 1330.", 13, 38, "Communications, Ops 2, 1330."),
    ("respond to 2500.", 14, 0, "respond to 2500."),
])
def test_the_time_at_the_end_of_a_dispatch(book, heard, hour, minute, written):
    assert book.normalize(heard, datetime(2026, 10, 26, hour, minute, 30)) == written


def test_old_dispatch_times_are_labelled_from_the_call_time(book):
    call_time = datetime(2026, 10, 26, 14, 17).timestamp()
    labels = book.annotate("Hanover Township, Communications Office, 14-16.", "Countywide", call_time=call_time)
    assert labels == [{"code": "14:16", "kind": "time", "meaning": "2:16 PM"}]


@pytest.fixture
def county(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_text(
        "type,code,meaning,applies_to,prompt\n"
        "unit-suffix,81-89,Ambulance,ems,\n"
        "unit-suffix,21-29,Ladder,fire,\n"
        "station,15,Hanover Twp Fire/EMS,fire-ems,\n"
        "station,24,Wilson Fire,fire-ems,\n"
        "station,27,Palmer Twp Fire,fire-ems,\n"
        "police-esn,27,Palmer Twp PD,police,\n"
        "call-type,ALS,Advanced life support call,fire-ems,\n"
        "acronym,ALS,Advanced Life Support,all,\n"
        "acronym,EMS,Emergency Medical Services,all,\n"
        "acronym,MVA,Motor vehicle accident,all,\n", encoding="utf-8")
    return CodeBook(path)


def test_stations_dashed_call_signs_and_call_types(county):
    labels = county.annotate("EMS 15, 15-82, ALS, Fire 27, 24-21, 27 Command, 10-10", "Countywide")
    assert [(l["code"], l["meaning"]) for l in labels] == [
        ("EMS 15", "Hanover Twp Fire/EMS"),
        ("15-82", "Hanover Twp Fire/EMS, Ambulance"),
        ("ALS", "Advanced life support call"),
        ("Fire 27", "Palmer Twp Fire"),
        ("24-21", "Wilson Fire, Ladder"),
        ("27 Command", "Palmer Twp Fire"),
    ]


def test_police_channels_read_numbers_as_police_cars(county):
    labels = county.annotate("2741, MVA at 2700 Nazareth Road, on 278.", "Co PD E1 Patch")
    assert [(l["code"], l["meaning"]) for l in labels] == [
        ("2741", "Palmer Twp PD, car 41"), ("MVA", "Motor vehicle accident")]
    # The same number on a fire channel is a fire call sign
    assert county.annotate("2781", "Countywide")[0]["meaning"] == "Palmer Twp Fire, Ambulance"


def test_times_are_written_the_twelve_hour_way():
    from sdrtrunk_transcriber.codes import to_twelve_hour
    assert to_twelve_hour("EMS 13, 1382 BLS, Ops 1, 13:41") == "EMS 13, 1382 BLS, Ops 1, 1:41 PM"
    assert to_twelve_hour("00:05 and 09:30 and 12:00") == "12:05 AM and 9:30 AM and 12:00 PM"
    # Already 12-hour, not a time, or not a clock time: left alone
    assert to_twelve_hour("1:41 PM, 13:41 pm, 10-4, 24:10, 3:15") == "1:41 PM, 13:41 pm, 10-4, 24:10, 3:15"
