import pytest

from sdrtrunk_transcriber.codes import CodeBook
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.units import UnitBoard, status_of

CODES = """type,code,meaning,applies_to,prompt,source
unit-suffix,11-19,Engine,fire,,test
unit-suffix,81-89,Ambulance,ems,,test
station,13,Hellertown Fire/EMS,fire-ems,,test
station,20,Easton Fire/EMS,fire-ems,,test
police-esn,27,Palmer Twp PD,police,,test
"""
NOW = 1_792_000_000


@pytest.fixture
def board(tmp_path):
    (tmp_path / "codes.csv").write_text(CODES, encoding="utf-8")
    db = Database(tmp_path / "calls.db")
    board = UnitBoard(db, CodeBook(tmp_path / "codes.csv"))
    board.calls = 0

    def heard(text, talkgroup="Norco Ops 1", at=None):
        board.calls += 1
        call_id = db.insert_call({"call_time": at or NOW + board.calls * 60, "received_at": NOW,
                                  "talkgroup": "1", "talkgroup_label": talkgroup, "audio_path": "x.mp3"})
        db.update_call(call_id, status="done", transcript=text)
        board.call_done(call_id)
        return call_id

    board.heard = heard
    return board


def units(board):
    return {u["unit"]: u for u in board.board(now=NOW + 3600)}


@pytest.mark.parametrize("text, status", [
    ("EMS 13, 1382 BLS, 1234 Main Street, Bethlehem City Communications, Ops 1, 13:41", "dispatched"),
    ("1382 responding", "responding"),
    ("2011 en route", "responding"),
    ("Engine 2011 on scene, nothing showing", "on scene"),
    ("1382 on location", "on scene"),
    ("1382 transporting one to St. Luke's Bethlehem", "transporting"),
    ("1382 en route to LVH Muhlenberg", "transporting"),
    ("1382 at St. Luke's", "at hospital"),
    ("1382 clear of St. Luke's, available", "available"),
    ("2011 back in quarters", "available"),
    ("2011 copy", None),
])
def test_status_words(text, status):
    assert status_of(text) == status


def test_a_unit_follows_its_calls_through_an_incident(board):
    board.heard("EMS 13, 1382 BLS, 1234 Main Street, Bethlehem City Communications, Ops 1, 13:41")
    unit = units(board)["1382"]
    assert unit["status"] == "dispatched" and unit["agency"] == "ems"
    assert unit["meaning"] == "Hellertown Fire/EMS, Ambulance"
    # The house number is not a unit
    assert "1234" not in units(board)

    board.heard("1382 on location")
    board.heard("1382, copy that")
    unit = units(board)["1382"]
    assert unit["status"] == "on scene" and unit["said"] == "1382 on location"
    assert unit["last_heard"] > unit["status_time"]

    board.heard("1382 transporting one to St. Luke's Bethlehem")
    board.heard("1382 clear of St. Luke's, available")
    assert units(board)["1382"]["status"] == "available"


def test_dashed_call_signs_and_police_cars(board):
    board.heard("20-11 on scene", talkgroup="Norco Tac 2")
    board.heard("2741 on location", talkgroup="Co PD S1 Patch")
    found = units(board)
    assert found["2011"]["shown"] == "20-11" and found["2011"]["agency"] == "fire"
    assert found["2741"]["meaning"] == "Palmer Twp PD, car 41" and found["2741"]["agency"] == "police"


def test_an_older_call_transcribed_late_does_not_undo_a_newer_status(board):
    board.heard("1382 on location", at=NOW + 600)
    board.heard("1382 responding", at=NOW + 300)
    assert units(board)["1382"]["status"] == "on scene"


def test_rebuild_replays_recent_calls(board):
    board.heard("2011 on scene", at=NOW - 3600)
    board.heard("1382 responding", at=NOW - 20 * 3600)
    assert board.rebuild(hours=12, now=NOW) == 1
    found = {u["unit"] for u in board.board(hours=24, now=NOW)}
    assert found == {"2011"}
