"""Incident map: following incidents onto Tac channels, grouping calls, and fixes made by hand."""

import pytest

from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.incidents import (MapWorker, channel_in, channels_in, is_dispatch, made_up, street_words,
                                            talkgroup_channel)

from test_incidents import gaz  # noqa: F401  (a fixture)

T = 1_800_000_000
TALKGROUPS = {"Countywide": "100", "Norco Ops 2": "102", "Norco Tac 1": "111", "Norco Tac 2": "112", "Norco Tac 2A": "122",
              "Co PD N1 Patch": "200"}


def make_worker(tmp_path, gaz):
    config = Config()
    return MapWorker(config, Database(tmp_path / "calls.db"), gaz, None)


def call(worker, transcript, offset, talkgroup="Countywide", alias=None):
    db = worker.db
    call_id = db.insert_call({"call_time": T + offset, "received_at": T + offset, "talkgroup": TALKGROUPS[talkgroup],
                              "talkgroup_label": talkgroup, "talkgroup_group": "County Fire / EMS",
                              "talker_alias": alias, "audio_path": "x.mp3"})
    db.update_call(call_id, status="done", transcript=transcript)
    worker.process(call_id)
    return call_id


def by_label(worker):
    return {i["label"].split(" & ")[0].split(",")[0]: i for i in worker.db.incidents()}


@pytest.mark.parametrize("text,channel", [
    ("Engine 41 switch to Tac 2", "Tac 2"),
    ("go to tack 2 for operations", "Tac 2"),
    ("units operating, TAC-1", "Tac 1"),
    ("Medic 5, BLS, leg pain, Nazareth Communications Ops 2, 20:16", "Ops 2"),
    ("command on fire ground 3", "Fireground 3"),
    ("operations on Ops 2, move to Tac 1", "Tac 1"),      # the last one named wins
    ("10-4, 20:16", None),
    # As Whisper wrote them on the Countywide tone-outs here.
    ("Bethlehem Township Communications on Tag 2, 16:01.", "Tac 2"),
    ("Upper Saucon Communications, OPS 1-1509.", "Ops 1"),
    ("Bangor, Washington, communications ops 2-12-14.", "Ops 2"),
    ("Palmer Township, communication on TAC 3-634", "Tac 3"),
    ("Fox Gap, Upper Mount Bethel, Communications, Op 2, 19-4.", "Ops 2"),
    ("every town of the station is on Pac-1 and Fireground 1, 14-23.", "Tac 1"),
    ("Lehigh County Communications, on TAC 5, and Fire North, Fire Ground 1, 21:09.", "Tac 5"),
    ("Ops 113-39.", None),                    # Ops 1 at 13:39 run together: too garbled to trust
])
def test_channel_in(text, channel):
    assert channel_in(text) == channel


def test_every_channel_a_call_names():
    assert channels_in("Lower Saucon Communications, Response on TAC-2A, Fireground, TAC-2, 16-38") == ["Tac 2A", "Tac 2"]


@pytest.mark.parametrize("text,dispatch", [
    ("Fire 16, 1311, for a Dwelling Fire Report, Garage Fire, Lower Saucon Communications, on TAC 2, 16:32.", True),
    ("10-27, MS-9, BLS, Shaking, Palmer Dialysis, Palmer Communications, Ops 2, 14:46.", True),
    ("Second Alarm Working, Dwelling Fire", True),
    ("Interior, we're almost out of water.", False),
    ("County 1382.", False),
])
def test_is_dispatch(text, dispatch):
    assert is_dispatch(text) == dispatch


VOCABULARY = ["Dispatch", "Engine 12", "Medic 4", "Battalion 1", "10-4", "Main Street"]


@pytest.mark.parametrize("text,invented", [
    ("Dispatch, Engine 12, 10-8, you can murder reportable.", True),       # the prompt read back
    ("Engine 12, Medic 4, Battalion 2, Battalion 2, Battalion,", True),
    ("4, Battalion 2, Battalion 2, Battalion 2, Battalion 2, Battalion 2", True),
    ("10-4, 10-4, 10-4, 10-4, 10-4", True),
    ("Engine 12 on location, working fire.", False),
    ("Dispatch, Engine 1234 Hay Street, 84-year-old female fall victim on the cement, head injury, coughing "
     "up blood in her nosebleed.", False),                                   # a real call starting with the echo
    ("1234 Main Street, 1234 Main Street, 1234 Main Street,", True),
    ("Highway Nation, 12 Main Street, Mission Trail, W-7, 34 Main Street, 18-5, the easton, 56 Main Street, "
     "78 Main Street, 90 Main Street, 1931,", True),
    ("1783 County, you can show me clear, this unit only", False),
])
def test_made_up(text, invented):
    assert made_up(text, VOCABULARY) == invented


@pytest.mark.parametrize("label,channel", [("Norco Tac 2", "Tac 2"), ("Norco Tac 2A", "Tac 2A"), ("Norco Ops 1", "Ops 1"),
                                           ("Fireground 2", "Fireground 2"), ("Countywide", None)])
def test_talkgroup_channel(label, channel):
    assert talkgroup_channel(label) == channel


def test_street_words():
    assert street_words("North 3rd Street & Northampton Street, Easton") == ["n 3rd st", "northampton st"]
    assert street_words("12 Main Street, Nazareth") == ["main st"]


def test_calls_on_the_tac_channel_follow_the_incident_sent_there(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Station 4 respond North 3rd and Northampton for a structure fire, Tac 2", 0)
    call(worker, "Medic 49 respond Main and Center in Nazareth, ALS, chest pain, Ops 2", 30)
    call(worker, "Command to all units, we have heavy smoke from the second floor", 300, "Norco Tac 2")
    call(worker, "Interior crew, primary search negative", 600, "Norco Tac 2")
    call(worker, "Unrelated chatter on another Tac", 650, "Norco Tac 1")
    incidents = by_label(worker)
    fire, medical = incidents["North 3rd Street"], incidents["Main Street"]
    assert fire["channel"] == "Tac 2"
    assert [c["precision"] for c in fire["calls"]] == ["intersection", "channel", "channel"]
    # Ops channels carry many incidents, so they are shown but not followed.
    assert medical["channel"] == "Ops 2" and len(medical["calls"]) == 1


def test_the_latest_incident_sent_to_a_channel_gets_its_calls(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Respond North 3rd and Northampton for a fire alarm, Tac 2", 0)
    call(worker, "Respond Route 22 at Freemansburg Ave for an MVA, Tac 2", 900)
    call(worker, "On location, two vehicles", 1000, "Norco Tac 2")
    incidents = by_label(worker)
    assert len(incidents["Route 22"]["calls"]) == 2
    assert len(incidents["North 3rd Street"]["calls"]) == 1


def test_the_response_channel_and_fireground_both_follow_the_fire(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Second alarm working, structure fire, North 3rd and Northampton, Easton Communications, "
                 "Response on TAC-2A, Fireground, TAC-2, 16-38", 0)
    call(worker, "This is response channel, fire ground is on TAC 2 when you arrive, switch over.", 60, "Norco Tac 2A")
    call(worker, "Interior, we're almost out of water.", 120, "Norco Tac 2")
    [incident] = worker.db.incidents()
    assert incident["channel"] == "Tac 2A, Tac 2"
    assert len(incident["calls"]) == 3


def test_a_channel_given_to_a_call_the_map_could_not_place_is_not_followed(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Fire alarm, North 3rd and Northampton, Easton Communications, TAC 2, 15:00", 0)
    # Twenty minutes later Tac 2 is given to a crash somewhere the street list doesn't know.
    call(worker, "Motor Vehicle Accident, Nowhere Boulevard, Communications on Tag 2, 15:20.", 1200)
    call(worker, "Two cars, one in the ditch.", 1300, "Norco Tac 2")
    [incident] = worker.db.incidents()
    assert len(incident["calls"]) == 1


def test_invented_transcripts_stay_off_the_map(tmp_path, gaz):
    # No vocabulary.txt: calls transcribed with the old sample hint list still echo it.
    worker = make_worker(tmp_path, gaz)
    call(worker, "Engine 41 respond North 3rd and Northampton for a structure fire", 0)
    noise = call(worker, "Dispatch, Engine 41, 1234 Northampton Street, Dispatch, Engine 12", 60)
    [incident] = worker.db.incidents()
    assert len(incident["calls"]) == 1
    assert worker.db.get_call(noise)["map_status"] == "noise"


def test_a_call_naming_the_incidents_street_joins_it(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Engine 41 respond North 3rd and Northampton for a structure fire", 0)
    call(worker, "Ladder 4 staging on Northampton Street", 240)
    [incident] = worker.db.incidents()
    assert [c["precision"] for c in incident["calls"]] == ["intersection", "same-street"]


def test_a_unit_sent_elsewhere_no_longer_follows_its_first_incident(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "1786 respond to North 3rd and Northampton for a fall", 0)
    call(worker, "1786 on scene", 120)
    # Sent to a crash somewhere the map can't place: its later calls are about the crash.
    call(worker, "1786, you are responding at 15:39, 4.733 North, PSP is out there with an MVA", 600)
    call(worker, "1786, we're on location here", 900)
    [incident] = worker.db.incidents()
    assert len(incident["calls"]) == 2


def test_a_unit_that_went_available_is_done_with_the_incident(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Medic 49 respond to North 3rd and Northampton for a fall", 0)
    call(worker, "Medic 49 you can show me clear, available", 600)
    call(worker, "Medic 49 for your status", 900)
    [incident] = worker.db.incidents()
    assert len(incident["calls"]) == 2


def test_when_every_unit_is_available_the_tac_channel_is_free(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Special assignment, North 3rd and Northampton, Easton Communications are on TAC 1, 15:30", 0)
    call(worker, "On location, front entrance", 60, "Norco Tac 1")
    call(worker, "My bad, all the units for freedom are available 9-16-0-1", 1800, "Norco Tac 1")
    call(worker, "It's going to be the on-ramp for westbound, two vehicles", 2600, "Norco Tac 1")
    [incident] = worker.db.incidents()
    assert len(incident["calls"]) == 3


def test_the_dispatched_call_type_beats_words_in_later_calls(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "County 516 for a fire alarm, North 3rd and Northampton, Easton Communications, TAC 2, 19:01", 0)
    call(worker, "Units on Tac 2, the crash on the highway is cleared", 600, "Norco Tac 2")
    [incident] = worker.db.incidents()
    assert incident["nature"] == "Fire alarm" and len(incident["calls"]) == 2


def test_a_new_dispatch_nearby_is_a_new_incident(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Respond North 3rd and Northampton for a fall, BLS", 0)
    # 300 m along Northampton Street, ten minutes later: close, but a different call.
    call(worker, "Respond 3rd and Northampton Street, Easton for a fire alarm", 600)
    assert len(worker.db.incidents()) == 1     # same intersection: still the same incident
    call(worker, "Respond Northampton Street at Short Lane for an odor of gas", 700)
    call(worker, "Respond North 3rd and Northampton for chest pain, ALS", 3000)   # same place, much later
    assert len(worker.db.incidents()) == 2


def test_fixes_by_hand_are_kept_when_the_map_is_rebuilt(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    a = call(worker, "Engine 41 respond North 3rd and Northampton for a structure fire", 0)
    b = call(worker, "Engine 41 on location, working fire", 60)
    c = call(worker, "Medic 49 respond Main and Center in Nazareth for a fall", 120)
    d = call(worker, "Respond Route 22 at Freemansburg Ave for an MVA", 180)
    incidents = by_label(worker)
    fire, fall, crash = incidents["North 3rd Street"], incidents["Main Street"], incidents["Route 22"]
    changed = []
    worker.on_changed = changed.append

    worker.split_call(b)                         # "on location" was really its own thing
    worker.merge(crash["id"], fall["id"])        # these two were one incident
    worker.remove_call(c)                        # and this one was nothing
    assert len(changed) == 3

    def groups():
        return sorted(sorted(x["id"] for x in i["calls"]) for i in worker.db.incidents())

    before = groups()
    assert before == [[a], [b], [d]]
    worker.rebuild()
    while not worker._queue.empty():
        worker.process(worker._queue.get())
    assert groups() == before
    assert worker.db.get_call(c)["map_status"] == "removed"


def test_move_a_call_to_another_incident(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    call(worker, "Engine 41 respond North 3rd and Northampton for a structure fire", 0)
    b = call(worker, "Unrelated, nothing to do with it", 30)          # placed nowhere
    stray = call(worker, "Respond Route 22 at Freemansburg Ave for an MVA", 60)
    fire = by_label(worker)["North 3rd Street"]
    worker.move_call(b, fire["id"])
    worker.move_call(stray, fire["id"])
    [incident] = worker.db.incidents()            # the MVA had only that call, so it is gone
    assert [c["precision"] for c in incident["calls"]] == ["intersection", "manual", "intersection"]
    assert incident["label"].startswith("North 3rd Street")
    with pytest.raises(KeyError):
        worker.move_call(b, 999)


def test_old_map_databases_gain_the_channel_columns(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "calls.db")
    conn.executescript("CREATE TABLE incidents (id INTEGER PRIMARY KEY, first_time INTEGER NOT NULL, "
                       "last_time INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, label TEXT, category TEXT, "
                       "nature TEXT, hidden INTEGER NOT NULL DEFAULT 0); PRAGMA user_version = 2;")
    conn.close()
    db = Database(tmp_path / "calls.db")
    db.create_incident({"first_time": 1, "last_time": 1, "lat": 0, "lon": 0, "channel": "Tac 2", "channel_time": 1})
    assert db.incidents(hidden=True)[0]["channel"] == "Tac 2"


def test_fixes_from_the_map_page(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from sdrtrunk_transcriber.server import create_app
    from test_incidents import GEO
    (tmp_path / "places-geo.json").write_text(json.dumps(GEO), encoding="utf-8")
    config = Config()
    config.base_dir = tmp_path
    config.map.online_lookup = False
    with TestClient(create_app(config)) as client:
        worker = client.app.state.map_worker
        a = call(worker, "Engine 41 respond North 3rd and Northampton for a structure fire", 0)
        b = call(worker, "Medic 49 respond Main and Center in Nazareth for a fall", 60)
        fire, fall = (worker.db.incident_of_call(x) for x in (a, b))
        assert client.post(f"/api/calls/{b}/move", json={"incident_id": fire}).json() == {"incidents": [fire]}
        assert client.post(f"/api/calls/{b}/split").status_code == 200
        new = worker.db.incident_of_call(b)
        assert client.post(f"/api/incidents/{new}/merge", json={"into": fire}).json() == {"incidents": [fire]}
        assert client.delete(f"/api/calls/{b}/place").json() == {"incidents": [fire]}
        assert client.post(f"/api/calls/{b}/move", json={"incident_id": 999}).status_code == 404
        assert client.post(f"/api/calls/{b}/move", json={}).status_code == 400
        assert worker.db.get_incident(fall) is None
