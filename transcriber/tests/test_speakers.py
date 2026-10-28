import pytest

from sdrtrunk_transcriber.codes import CodeBook
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.incidents import dispatch_call, talker, units_in
from sdrtrunk_transcriber.speakers import SpeakerBook, sign_key
from sdrtrunk_transcriber.summaries import _call_line
from sdrtrunk_transcriber.units import UnitBoard

NOW = 1_792_000_000
POLICE = "Co PD S1 Patch"
FIRE = "Norco Ops 1"


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "calls.db")


@pytest.fixture
def book(db):
    return SpeakerBook(db)


def add(db, text, talkgroup=POLICE, source="0", tg_id="11007", at=None):
    call_id = db.insert_call({"call_time": at or NOW, "received_at": NOW, "talkgroup": tg_id,
                              "talkgroup_label": talkgroup, "source": source, "audio_path": "x.mp3"})
    db.update_call(call_id, status="done", transcript=text)
    return call_id


@pytest.mark.parametrize("text, talkgroup, kind, name", [
    # Police: the unit says its number, then County; the dispatcher says County first.
    ("1770 County, I'll break free from William Penn Highway.", POLICE, "unit", "1770"),
    ("17-81 to the County, you can clear me from the MVA.", POLICE, "unit", "17-81"),
    ("County 1784 for your status.", POLICE, "dispatch", "Dispatch"),
    # Fire and EMS: the other way around.
    ("County 2851, I'll be responding.", FIRE, "unit", "2851"),
    ("County, if you can notify Verizon, we're going to have to come out.", FIRE, "unit", "Unit"),
    ("1691, go ahead, County.", FIRE, "dispatch", "Dispatch"),
    # A tone-out the dispatcher starts with County is still a dispatch.
    ("County, EMS 810, BLS, sick person, 971 Wyandotte Street, 971 Wyandotte Street.", "Countywide",
     "dispatch", "Dispatch"),
    # "X from Y" is Y, anywhere.
    ("2851 from 2857", FIRE, "unit", "2857"),
    ("County from Command, notify my FM, please.", FIRE, "unit", "Command"),
    # What a dispatcher says, and what a unit says.
    ("2073 Cali Ridge Road, 2073 Cali Ridge Road, for a 71-year-old male with heart pain.", POLICE,
     "dispatch", "Dispatch"),
    ("16-12, 10-90, 10-4, I'll show you on scene at 19:37 hours.", POLICE, "dispatch", "Dispatch"),
    ("Negative, I checked the area of Oak Street, we have nothing found.", POLICE, "unit", "Unit"),
    ("Okay, you can clear me from here.", POLICE, "unit", "Unit"),
    # Nothing to go on: left unlabelled, not guessed.
    ("Thank you.", POLICE, None, None),
    ("", POLICE, None, None),
])
def test_words(book, text, talkgroup, kind, name):
    assert book.label({"transcript": text, "talkgroup_label": talkgroup, "source": "0"})[:2] == (kind, name)


def test_countywide_leans_dispatch(book):
    call = {"transcript": "Fire 20, Fire 24 for a commercial fire alarm.", "talkgroup_label": "Countywide",
            "source": "0"}
    assert book.label(call) == ("dispatch", "Dispatch", "words")


def test_a_ten_code_is_not_a_call_sign(book):
    assert book.label({"transcript": "10-4 County, thanks.", "talkgroup_label": POLICE, "source": "0"})[:2] \
        == (None, None)


def test_a_radio_id_is_a_unit_and_learns_its_call_sign(db, book):
    assert book.label({"transcript": "Yeah, we're on it.", "talkgroup_label": POLICE, "source": "1117081"}) \
        == ("unit", "Unit", "radio id")
    book.label({"transcript": "1781 County, I'm clear.", "talkgroup_label": POLICE, "source": "1117081"})
    # Its later calls carry the call sign, even with none in the words.
    assert book.label({"transcript": "Show me on scene.", "talkgroup_label": POLICE, "source": "1117081"}) \
        == ("unit", "1781", "radio id")
    # Dispatch wording doesn't make a radio with an ID the dispatcher.
    assert book.label({"transcript": "County 1784 for your status.", "talkgroup_label": POLICE,
                       "source": "1117081"})[0] == "unit"


def test_one_mishearing_does_not_rename_a_radio(db, book):
    for _ in range(3):
        book.label({"transcript": "1781 County.", "talkgroup_label": POLICE, "source": "1117081"})
    book.label({"transcript": "1871 County.", "talkgroup_label": POLICE, "source": "1117081"})
    assert book.label({"transcript": "Copy.", "talkgroup_label": POLICE, "source": "1117081"})[1] == "1781"


def test_no_id_on_a_talkgroup_where_units_send_theirs_leans_dispatch(db):
    for i in range(40):
        add(db, "x", source=str(1117000 + i) if i % 2 else "0")
    book = SpeakerBook(db)
    # Neither kind of wording, but on this talkgroup units send their radio ID: the console sent this.
    call = {"transcript": "Copy, the sergeant will call you back.", "talkgroup": "11007",
            "talkgroup_label": POLICE, "source": "0"}
    assert book.label(dict(call, talkgroup="other"), learn=False)[0] is None
    assert book.label(call)[0] == "dispatch"


def test_apply_and_backfill_store_labels(db, book):
    first = add(db, "County 1784 for your status.")
    second = add(db, "Thank you.")
    assert book.backfill() == 2
    assert db.get_call(first)["speaker_kind"] == "dispatch"
    assert db.get_call(second)["speaker_how"] == "none"
    assert book.backfill() == 0
    third = add(db, "1770 County, on scene.")
    call = book.apply(third)
    assert (call["speaker_kind"], call["speaker"]) == ("unit", "1770")
    assert db.get_call(third)["speaker"] == "1770"
    # The calls page's radio ID box finds calls by who said them.
    assert [c["id"] for c in db.search(source="dispatch")] == [first]


def test_sign_key():
    assert sign_key("17-81") == sign_key("1781") == "1781"
    assert sign_key("Medic 27") == "medic27"


def test_the_map_doesnt_take_a_units_own_words_as_a_dispatch():
    text = "County 4985, we'll respond to 683 East 21st Street, ALS."
    assert dispatch_call({"transcript": text})
    assert dispatch_call({"transcript": text, "speaker_kind": "dispatch", "speaker": "Dispatch"})
    # A unit guessed only from its wording might still be the dispatcher: only a sure unit counts.
    assert dispatch_call({"transcript": text, "speaker_kind": "unit", "speaker": "Unit", "speaker_how": "words"})
    assert not dispatch_call({"transcript": text, "speaker_kind": "unit", "speaker": "4985", "speaker_how": "words"})
    assert not dispatch_call({"transcript": text, "speaker_kind": "unit", "speaker": "Unit",
                              "speaker_how": "radio id"})


def test_the_map_joins_a_call_by_the_unit_talking():
    call = {"transcript": "On scene.", "talker_alias": "", "speaker_kind": "unit", "speaker": "17-81"}
    assert units_in(talker(call)) == units_in("1781")
    assert talker({"transcript": "x", "speaker_kind": "dispatch", "speaker": "Dispatch"}) == ""


def test_summaries_read_calls_as_a_conversation():
    base = {"call_time": NOW, "talkgroup_label": POLICE, "talkgroup": "11007", "talker_alias": None,
            "transcript": "On scene."}
    assert "(unit 1781): On scene." in _call_line(dict(base, speaker_kind="unit", speaker="1781"))
    assert "(Dispatch): On scene." in _call_line(dict(base, speaker_kind="dispatch", speaker="Dispatch"))
    assert "(a unit): " in _call_line(dict(base, speaker_kind="unit", speaker="Unit"))
    assert f"{POLICE}: On scene." in _call_line(base)


CODES = """type,code,meaning,applies_to,prompt,source
unit-suffix,81-89,Ambulance,ems,,test
station,13,Hellertown Fire/EMS,fire-ems,,test
station,20,Easton Fire/EMS,fire-ems,,test
"""


def test_the_unit_board_credits_the_unit_talking(tmp_path, db, book):
    (tmp_path / "codes.csv").write_text(CODES, encoding="utf-8")
    board = UnitBoard(db, CodeBook(tmp_path / "codes.csv"))
    # 2081 says 1382 is with it; only 2081 said it is on scene.
    call_id = add(db, "County 2081, on scene, 1382 is coming in behind us.", talkgroup=FIRE, tg_id="10001")
    board.process(book.apply(call_id))
    found = {u["unit"]: u for u in board.board(now=NOW + 60)}
    assert found["2081"]["status"] == "on scene"
    assert found["1382"]["status"] is None and found["1382"]["last_heard"] == NOW
    # A radio that gave its call sign before is credited with no number in the words.
    add(db, "County 2081, responding.", talkgroup=FIRE, tg_id="10001", source="1220081", at=NOW + 100)
    book.backfill()
    call_id = add(db, "Transporting one to St. Luke's.", talkgroup=FIRE, tg_id="10001", source="1220081",
                  at=NOW + 200)
    board.process(book.apply(call_id))
    assert {u["unit"]: u for u in board.board(now=NOW + 300)}["2081"]["status"] == "transporting"
