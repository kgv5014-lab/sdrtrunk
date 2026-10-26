import time

from sdrtrunk_transcriber.aircraft import AircraftWatch, WatchList, is_helicopter, is_medical
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database

NOW = 1_792_000_000


class FakeAlerter:
    base_url = ""

    def __init__(self):
        self.sent = []

    def send(self, kind, title, message, **kwargs):
        self.sent.append((kind, title, message, kwargs))


def heli(**fields):
    plane = {"hex": "a1b2c3", "flight": "LIFE12  ", "r": "N812LF", "t": "EC45", "category": "A7",
             "desc": "AIRBUS HELICOPTERS H-145", "ownOp": "GEISINGER SYSTEM SERVICES",
             "lat": 40.70, "lon": -75.30, "alt_baro": 3000, "gs": 120, "seen_pos": 0.5}
    plane.update(fields)
    return plane


def make_watch(tmp_path, alerter=None, **settings):
    config = Config()
    config.base_dir = tmp_path
    for key, value in settings.items():
        setattr(config.aircraft, key, value)
    db = Database(tmp_path / "calls.db")
    return AircraftWatch(config, db, alerter), db


def add_incident(db, lat, lon, when=NOW - 600):
    with db._lock:
        cur = db._conn.execute(
            "INSERT INTO incidents (first_time, last_time, lat, lon, label, nature) VALUES (?, ?, ?, ?, ?, ?)",
            (when, when, lat, lon, "Route 33 at Freemansburg Ave", "MVA"))
        db._conn.commit()
    return cur.lastrowid


def test_helicopters_are_found_by_category_type_or_description():
    assert is_helicopter({"category": "A7"})
    assert is_helicopter({"t": "B407"})
    assert is_helicopter({"desc": "BELL 407 helicopter"})
    # A HondaJet the receiver showed on Ken's Pi
    assert not is_helicopter({"t": "HDJT", "category": "A2", "desc": "HONDA HA-420 HondaJet"})
    assert is_medical({"ownOp": "Geisinger System Services"})
    assert not is_medical({"ownOp": "NetJets"})


def test_watch_list_matches_tail_callsign_hex_and_prefix():
    watch = WatchList([{"match": "n812lf", "name": "Life Flight 12"}, "LIFE*", {"match": "ABC123"}])
    assert watch.name({"r": "N812LF"}) == "Life Flight 12"
    assert watch.name({"flight": "LIFE9   "}) == ""
    assert watch.name({"hex": "abc123"}) == ""
    assert watch.name({"r": "N1234", "flight": "UAL1"}) is None


def test_only_helicopters_are_kept_with_a_trail(tmp_path):
    watch, _ = make_watch(tmp_path)
    jet = {"hex": "ffffff", "flight": "UAL1", "t": "B738", "category": "A3", "lat": 40.6, "lon": -75.4,
           "alt_baro": 30000, "seen_pos": 1}
    watch.update({"aircraft": [heli(), jet]}, NOW)
    watch.update({"aircraft": [heli(lat=40.71)]}, NOW + 5)
    shown = watch.aircraft()
    assert [a["hex"] for a in shown] == ["a1b2c3"]
    plane = shown[0]
    assert plane["flight"] == "LIFE12" and plane["reg"] == "N812LF" and plane["medical"]
    assert plane["trail"] == [[40.70, -75.30], [40.71, -75.30]]
    assert plane["alt_ft"] == 3000 and plane["near"] is None

    # Gone from the receiver for over a minute: dropped
    watch.update({"aircraft": []}, NOW + 120)
    assert watch.aircraft() == []


def test_watch_list_only_when_all_helicopters_is_off(tmp_path):
    watch, _ = make_watch(tmp_path, all_helicopters=False, watch=[{"match": "N999", "name": "Mine"}])
    watch.update({"aircraft": [heli(), heli(hex="bbbbbb", r="N999")]}, NOW)
    assert [a["name"] for a in watch.aircraft()] == ["Mine"]


def test_landing_near_an_incident_is_marked_and_alerts_once(tmp_path):
    alerter = FakeAlerter()
    watch, db = make_watch(tmp_path, alerter, alert_landing=True)
    incident = add_incident(db, 40.6300, -75.3200)

    # High overhead: nearby but not landing
    watch.update({"aircraft": [heli(lat=40.632, lon=-75.321, alt_baro=2500, gs=110)]}, NOW)
    near = watch.aircraft()[0]["near"]
    assert near["incident_id"] == incident and not near["landing"]
    assert alerter.sent == []

    for step in range(3):
        watch.update({"aircraft": [heli(lat=40.6305, lon=-75.3205, alt_baro=600 - 200 * step, gs=30)]}, NOW + 5 + step)
    near = watch.aircraft()[0]["near"]
    assert near["landing"] and near["miles"] < 0.1
    assert len(alerter.sent) == 1
    kind, title, message, refs = alerter.sent[0]
    assert kind == "aircraft" and "LIFE12" in title and "GEISINGER" in title
    assert "MVA Route 33 at Freemansburg Ave" in message and refs["incident_id"] == incident


def test_old_or_far_incidents_are_ignored(tmp_path):
    watch, db = make_watch(tmp_path)
    add_incident(db, 40.6300, -75.3200, when=NOW - 4 * 3600)
    add_incident(db, 40.7500, -75.2000)
    watch.update({"aircraft": [heli(lat=40.6305, lon=-75.3205, alt_baro="ground", gs=0)]}, NOW)
    plane = watch.aircraft()[0]
    assert plane["alt_ft"] == 0 and plane["near"] is None


def test_no_alert_unless_turned_on(tmp_path):
    alerter = FakeAlerter()
    watch, db = make_watch(tmp_path, alerter)
    add_incident(db, 40.6300, -75.3200)
    watch.update({"aircraft": [heli(lat=40.6305, lon=-75.3205, alt_baro=300, gs=20)]}, NOW)
    assert watch.aircraft()[0]["near"]["landing"]
    assert alerter.sent == []


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return FakeResponse({"now": time.time(), "aircraft": [heli()]})


def test_poll_reads_the_receiver(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    session = FakeSession()
    watch = AircraftWatch(config, Database(tmp_path / "calls.db"), session=session)
    watch.poll(NOW)
    assert session.urls == ["http://192.168.50.102:8080/data/aircraft.json"]
    assert watch.aircraft()[0]["hex"] == "a1b2c3" and watch.error is None


def test_settings_load_from_config_toml(tmp_path):
    from sdrtrunk_transcriber.config import load_config
    path = tmp_path / "config.toml"
    path.write_text('[aircraft]\nenabled = true\nwatch = [{match = "N812LF", name = "Life Flight 12"}]\n'
                    'landing_radius_miles = 2\n', encoding="utf-8")
    config = load_config(path)
    assert config.aircraft.enabled and config.aircraft.landing_radius_miles == 2.0
    assert WatchList(config.aircraft.watch).name({"r": "N812LF"}) == "Life Flight 12"


def test_aircraft_api_says_when_off(tmp_path):
    from fastapi.testclient import TestClient
    from sdrtrunk_transcriber.backends import NoBackend
    from sdrtrunk_transcriber.server import create_app
    config = Config()
    config.base_dir = tmp_path
    with TestClient(create_app(config, backend_factory=lambda _c: NoBackend())) as client:
        assert client.get("/api/aircraft").json() == {"enabled": False, "aircraft": []}
