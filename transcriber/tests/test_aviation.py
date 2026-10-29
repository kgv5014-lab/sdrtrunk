"""The aviation page: airport and helicopter calls kept apart, matched to ADS-B planes, 121.5 alerts."""

import json
import time

from fastapi.testclient import TestClient

from sdrtrunk_transcriber import aviation
from sdrtrunk_transcriber.aircraft import AircraftWatch
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.server import create_app

from test_alerts import BASE_URL, lines, make_config, session  # noqa: F401 (pytest fixture)
from test_incidents import Radio
from test_service import CALL, make_mp3, post, sdrtrunk_body

NOW = 1_792_000_000

PLANES = [
    {"hex": "a00001", "flight": "AAY1234", "reg": "N254NV", "type": "A320"},
    {"hex": "a00002", "flight": "N812LF", "reg": "N812LF", "type": "EC45", "kind": "medevac", "helicopter": True},
    {"hex": "a00003", "flight": "N5432K", "reg": "N5432K", "type": "C172"},
    {"hex": "a00004", "flight": "FDX12", "reg": "N123FE", "type": "B763"},
    {"hex": "a00005", "flight": "LIFE4", "reg": "N404GH", "type": "EC35", "helicopter": True},
    {"hex": "a00006", "flight": "N2LF", "reg": "N2LF", "type": "PA28"},
]


def matched(text, planes=PLANES):
    return [p["hex"] for p in aviation.match_call(text, planes)]


def test_call_signs_are_read_in_the_ways_they_are_said():
    signs = aviation.spoken_callsigns("Allegiant twelve thirty four, descend and maintain 3000")
    assert signs[0]["flight"] == "AAY1234"
    assert aviation.spoken_callsigns("November eight one two Lima Foxtrot")[0]["tail"] == "N812LF"
    assert aviation.spoken_callsigns("N812LF, Allentown tower")[0]["tail"] == "N812LF"
    assert aviation.spoken_callsigns("Cessna 4-3-2 Kilo, runway 6")[0]["short"] == "432K"
    assert aviation.spoken_callsigns("FedEx one two heavy")[0]["flight"] == "FDX12"
    # A taxiway or ATIS letter on its own isn't a call sign
    assert all("short" not in s or s["short"] != "B" for s in aviation.spoken_callsigns("taxi via Bravo"))


def test_calls_are_matched_to_the_plane_that_said_it():
    assert matched("Allentown approach, Allegiant 1234 with you at 6000") == ["a00001"]
    assert matched("November 812 Lima Foxtrot, landing St. Luke's") == ["a00002"]
    assert matched("812LF, wind 240 at 10") == ["a00002"]
    assert matched("Cessna 32K, cleared to land runway 6") == ["a00003"]
    assert matched("FedEx 12 heavy, contact departure") == ["a00004"]
    assert matched("Life Flight 4 departing the pad") == ["a00005"]
    # Two call signs in one call: both, in the order heard
    assert matched("Cessna 32K, traffic is Allegiant 1234 on a 3 mile final") == ["a00003", "a00001"]
    # Numbers that are only altitudes, headings and runways match nothing
    assert matched("Descend and maintain 3000, turn left heading 240, runway 24") == []
    assert matched("Delta 77, contact New York") == []


def test_a_short_call_sign_two_planes_fit_matches_neither():
    twins = PLANES + [{"hex": "a00007", "flight": "N7432K", "reg": "N7432K", "type": "C182"}]
    assert matched("Cessna 32K, cleared to land", twins) == []
    # Said in full it is only one of them
    assert matched("November 5432 Kilo", twins) == ["a00003"]


def test_medevac_words():
    assert aviation.is_medevac_call("Life Flight 4, Allentown tower")
    assert aviation.is_medevac_call("JeffSTAT 2 inbound")
    assert not aviation.is_medevac_call("American 12, cleared for takeoff")


def jet(**fields):
    plane = {"hex": "a00001", "flight": "AAY1234 ", "r": "N254NV", "t": "A320", "category": "A3",
             "lat": 40.65, "lon": -75.44, "alt_baro": 3000, "gs": 160, "seen_pos": 0.5, "seen": 0.2}
    plane.update(fields)
    return plane


def test_every_aircraft_is_remembered_with_where_it_was(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    watch = AircraftWatch(config, Database(tmp_path / "calls.db"))
    watch.update({"aircraft": [jet()]}, NOW)
    watch.update({"aircraft": [jet(lat=40.66, alt_baro=2500)]}, NOW + 30)
    # Not a helicopter, so not on the incident map, but kept for matching radio calls
    assert watch.aircraft() == []
    plane, = watch.recent_aircraft(NOW + 35)
    assert plane["flight"] == "AAY1234" and plane["reg"] == "N254NV" and not plane["helicopter"]
    assert aviation.position_at(plane, NOW + 28)["alt_ft"] == 2500
    assert aviation.position_at(plane, NOW + 2)["lat"] == 40.65
    assert watch.recent_aircraft(NOW + 3600) == []
    # Long gone from the receiver: forgotten
    watch.update({"aircraft": []}, NOW + 2000)
    assert watch.recent_aircraft(NOW + 35) == []


AIR_CALL = {**CALL, "talkgroup": "9102", "talkgroupLabel": "ABE Tower", "talkgroupGroup": "Aviation",
            "talkerAlias": "", "frequency": "120500000", "systemLabel": "Airband"}
GUARD_CALL = {**AIR_CALL, "talkgroup": "9103", "talkgroupLabel": "Air Emergency", "frequency": "121500000"}


def upload(client, call, when):
    post(client, sdrtrunk_body({**call, "dateTime": str(when)}, make_mp3(2.0)))
    assert client.app.state.worker.wait_idle()


def test_airport_calls_get_their_own_page_and_medevac_stays_in_the_call_log(tmp_path, session):
    config = make_config(tmp_path)
    config.aircraft.enabled = True
    config.aircraft.url = "http://192.168.50.102:8080/data/aircraft.json"
    radio = Radio([
        "Engine 41 on location, North 3rd and Northampton, working fire.",
        "Allegiant 1234, runway 6, cleared to land.",
        "Allentown tower, Life Flight 12 departing St. Luke's to the north.",
    ])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        watch = client.app.state.aircraft
        watch.stop()
        watch.update({"aircraft": [jet(), {"hex": "c0ffee", "flight": "LIFE12", "r": "N812LF", "t": "EC45",
                                           "category": "A7", "lat": 40.60, "lon": -75.39, "alt_baro": 900,
                                           "gs": 40, "seen_pos": 1}]}, now - 5)
        upload(client, CALL, now - 30)
        upload(client, AIR_CALL, now - 20)
        upload(client, AIR_CALL, now - 10)
        assert client.app.state.map_worker.wait_idle()
        main = client.get("/api/calls").json()
        air = client.get("/api/aviation/calls").json()
        tower_only = client.get("/api/aviation/calls", params={"talkgroup": "9102", "q": "Allegiant"}).json()
        incidents = client.get("/api/incidents", params={"since": 0}).json()
        page = client.get("/aviation")
        client.app.state.alerter.flush()
        sent = lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1}))

    assert page.status_code == 200 and "Aviation" in page.text
    # The county call and the medevac call are in the main log; the airliner isn't
    assert [c["transcript"][:10] for c in main] == ["Allentown ", "Engine 41 "]
    assert main[0]["air"] == "medevac" and main[0]["aircraft"][0]["reg"] == "N812LF"
    assert [c["air"] for c in air["calls"]] == ["medevac", "aviation"]
    plane = air["calls"][1]["aircraft"][0]
    assert plane["flight"] == "AAY1234" and plane["alt_ft"] == 3000 and plane["how"] == "said Allegiant 1234"
    assert air["adsb"] and air["adsb_map_url"] == "http://192.168.50.102:8080/"
    assert [c["id"] for c in tower_only["calls"]] == [air["calls"][1]["id"]]
    # Airport radio never makes incidents, and only 121.5 alerts (the county fire does)
    assert all(c["talkgroup"] == "1203" for i in incidents for c in i["calls"])
    assert [m["title"] for m in sent] == ["Structure fire"]


def test_anyone_talking_on_121_5_sends_one_alert(tmp_path, session):
    config = make_config(tmp_path)
    radio = Radio(["Mayday mayday, Cessna 5432K, engine failure", "Cessna 5432K, roger, say souls on board", ""])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        upload(client, GUARD_CALL, now - 20)
        upload(client, GUARD_CALL, now - 10)
        upload(client, GUARD_CALL, now - 5)
        client.app.state.alerter.flush()
        sent = lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1}))
        air = client.get("/api/aviation/calls").json()

    assert len(sent) == 1
    alert = sent[0]
    assert alert["title"] == "121.5 emergency: someone is talking" and alert["message"].startswith("Mayday")
    assert alert["priority"] == 5 and alert["click"] == f"{BASE_URL}/aviation"
    assert all(c["emergency"] for c in air["calls"])


def test_121_5_is_transcribed_before_county_calls(tmp_path):
    config = make_config(tmp_path)
    client = TestClient(create_app(config, backend_factory=lambda _c: Radio([])))
    with client:
        worker = client.app.state.worker
        assert worker.rank({"talkgroup": "9103", "frequency": 121500000}) == -1
        assert worker.rank({"talkgroup": "9102", "talkgroup_label": "ABE Tower"}) == len(
            config.transcription.first_talkgroups)


def test_calls_logged_before_are_moved_to_the_aviation_page(tmp_path):
    db = Database(tmp_path / "calls.db")
    for talkgroup, label in (("9101", "ABE Approach"), ("1203", "Fire Dispatch"), ("77", "Helicopters")):
        db.insert_call({"call_time": NOW, "received_at": NOW, "talkgroup": talkgroup, "talkgroup_label": label,
                        "audio_path": "x.mp3"})
    config = Config()
    config.air_transcripts.talkgroups = ["9101", "helicopters"]
    assert aviation.AviationLog(config, db).backfill() == 2
    assert [c["talkgroup"] for c in db.search(limit=10)] == ["1203"]
    assert [c["talkgroup"] for c in db.search(limit=10, aviation=True)] == ["77", "9101"]


def test_aviation_settings_load(tmp_path):
    from sdrtrunk_transcriber.config import load_config
    path = tmp_path / "config.toml"
    path.write_text('[aviation]\nemergency_alert = false\n', encoding="utf-8")
    config = load_config(path)
    assert not config.aviation.emergency_alert
    assert json.loads(json.dumps(config.aviation.emergency_talkgroups)) == ["9103"]
