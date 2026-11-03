"""Smart Fire Scan: which departments SDRTrunk's Fire Scan stays on after a tone-out."""

import time

from fastapi.testclient import TestClient

from sdrtrunk_transcriber.config import ChannelsConfig, Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.scan_focus import fire_stations_said, toned_out
from sdrtrunk_transcriber.server import create_app

FORKS, PALMER, TATAMY, NANCY = 453_300_000, 460_375_000, 458_725_000, 502_425_000


def page(db, t, label, channel="Countywide"):
    db.insert_page({"page_time": t, "received_at": int(t), "tone_a": 1000.0, "tone_b": 1200.0,
                    "tone_a_seconds": 1.0, "label": label, "channel": channel})


def call(db, t, transcript, label="Countywide"):
    call_id = db.insert_call({"call_time": int(t), "received_at": int(t), "talkgroup": "10000",
                              "talkgroup_label": label, "audio_path": "x.mp3"})
    db.update_call(call_id, duration_s=20.0, status="done", transcript=transcript)


def held(db, now):
    return [(f["frequency"], round(f["until"] - now)) for f in toned_out(db, ChannelsConfig(), now)["frequencies"]]


def test_a_fire_tone_out_holds_its_department_for_fifteen_minutes(tmp_path):
    db = Database(tmp_path / "calls.db")
    now = time.time()
    page(db, now - 60, "Fire 25")
    assert held(db, now) == [(FORKS, 14 * 60)]
    # Over after fifteen minutes.
    assert held(db, now + 15 * 60) == []


def test_several_toned_out_are_all_held_and_a_second_tone_out_restarts_the_time(tmp_path):
    db = Database(tmp_path / "calls.db")
    now = time.time()
    page(db, now - 600, "Fire 25")
    page(db, now - 300, "Station 14")
    page(db, now - 120, "Fire 25")
    assert held(db, now) == [(NANCY, 10 * 60), (FORKS, 13 * 60)]


def test_ems_tones_and_other_stations_do_not_count(tmp_path):
    db = Database(tmp_path / "calls.db")
    now = time.time()
    page(db, now - 60, "EMS 27")
    page(db, now - 50, "Fire 54")
    page(db, now - 40, None)
    assert held(db, now) == []


def test_dispatch_naming_the_station_after_unnamed_tones_counts(tmp_path):
    db = Database(tmp_path / "calls.db")
    now = time.time()
    page(db, now - 100, None)
    call(db, now - 95, "Station 22, Tatamy, respond for the brush fire")
    # The same words on another talkgroup, or long after the tones, don't count.
    page(db, now - 400, None)
    call(db, now - 390, "Station 27 respond", label="County Police")
    call(db, now - 200, "Fire 14 respond")
    assert [f for f, _ in held(db, now)] == [TATAMY]


def test_station_words():
    words = {"forks": 25, "tatamy": 22, "palmer": 27, "nancy": 14}
    assert fire_stations_said("Forks Fire, structure fire", words) == {25}
    assert fire_stations_said("Tatamy fire company respond", words) == {22}
    assert fire_stations_said("Fire 27 and EMS 14", words) == {27}
    # An address in a township isn't a fire company.
    assert fire_stations_said("EMS 27, 100 Main Street, Palmer Township, fall", words) == set()
    assert fire_stations_said("Medic 2781 respond", words) == set()


def test_scan_focus_api_and_turning_it_off(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    config.server.data_dir = str(tmp_path)
    with TestClient(create_app(config)) as client:
        page(client.app.state.db, time.time() - 30, "Fire 27")
        body = client.get("/api/scan-focus").json()
        assert body["hold_minutes"] == 15
        assert [(f["frequency"], f["name"]) for f in body["frequencies"]] == [(PALMER, "Palmer FD")]
    config.channels.toned_stations = {}
    with TestClient(create_app(config)) as client:
        assert client.get("/api/scan-focus").json()["frequencies"] == []
