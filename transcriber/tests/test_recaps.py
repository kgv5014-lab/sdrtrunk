"""12-hour summaries (recaps.py)."""

import json
import time
from datetime import datetime

from fastapi.testclient import TestClient

from sdrtrunk_transcriber import recaps
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.recaps import RecapMaker, count_line, last_slot, next_slot, parse_times, town_of
from sdrtrunk_transcriber.server import create_app

from test_service import FakeBackend
from test_summaries import FakeOllama

HOME = (40.6567870, -75.2836552)


class FakeAlerter:
    base_url = "https://pc.example.ts.net"

    def __init__(self):
        self.sent = []

    def send(self, kind, title, message, **kwargs):
        self.sent.append((kind, title, message, kwargs))


def add_incident(db, start, nature, category, label, lat, lon, calls=3, summary=None, tags="", status=None):
    incident_id = db.create_incident({"first_time": start, "last_time": start + 600, "lat": lat, "lon": lon,
                                      "label": label, "category": category, "nature": nature})
    for n in range(calls):
        call_id = db.insert_call({"call_time": start + n * 60, "received_at": start, "talkgroup": "10000",
                                  "talkgroup_label": "County Fire / EMS", "audio_path": f"{incident_id}-{n}.mp3"})
        db.update_call(call_id, status="done", transcript=f"Engine 41 responding to {label}",
                       map_status="placed")
        db.add_call_place({"call_id": call_id, "incident_id": incident_id, "kind": "address", "label": label,
                           "lat": lat, "lon": lon, "precision": "address"})
    if summary:
        db.store_summary({"kind": "incident", "ref_id": incident_id, "basis": "x", "summary": summary,
                          "tags": tags, "status": status, "model": "m", "made_at": start + 700})
    return incident_id


def setup(tmp_path, ollama=None):
    config = Config()
    config.alerts.zones = [{"name": "Concord Circle", "lat": HOME[0], "lon": HOME[1], "radius_miles": 0.5}]
    config.summaries.enabled = ollama is not None
    db = Database(tmp_path / "calls.db")
    alerter = FakeAlerter()
    maker = RecapMaker(config, db, alerter, session=ollama)
    return config, db, alerter, maker


def fill(db, end):
    start = end - 12 * 3600
    big = add_incident(db, start + 3600, "Structure fire", "fire", "1234 Northampton St, Easton", 40.69, -75.21,
                       calls=20, summary="Working fire, second alarm.", tags="working fire", status="under control")
    db.add_incident_update({"incident_id": big, "time": start + 4000, "note": "Fire knocked down.",
                            "status": "under control", "made_at": start + 4000})
    near = add_incident(db, start + 7200, "Fall", "ems", "12 Concord Cir, Forks", HOME[0] + 0.005, HOME[1], calls=2)
    add_incident(db, start + 9000, "MVA", None, "Route 22 East, Bethlehem", 40.63, -75.36, calls=4)
    add_incident(db, start + 9500, "Theft", "police", "Northampton Crossings, Easton", 40.70, -75.24, calls=1)
    # Before the window: not counted.
    add_incident(db, start - 7200, "Structure fire", "fire", "9 Main St, Nazareth", 40.74, -75.31, calls=5)
    return big, near


def test_times_and_slots():
    times = parse_times(["19:00", "07:00"])
    assert times == [(7, 0), (19, 0)]
    noon = datetime(2026, 11, 2, 12, 0).timestamp()
    assert last_slot(times, noon) == datetime(2026, 11, 2, 7, 0).timestamp()
    assert next_slot(times, noon) == datetime(2026, 11, 2, 19, 0).timestamp()
    early = datetime(2026, 11, 2, 3, 0).timestamp()
    assert last_slot(times, early) == datetime(2026, 11, 1, 19, 0).timestamp()
    assert town_of("1234 Northampton St, Easton") == "Easton"
    assert town_of("Route 22") is None
    assert count_line({"incidents": 0}) == "No incidents"


def test_counts_highlights_and_phone_alert_only_important_or_close(tmp_path):
    config, db, alerter, maker = setup(tmp_path)
    end = datetime(2026, 11, 2, 19, 0).timestamp()
    big, near = fill(db, end)
    recap = maker.make(end - 12 * 3600, end, kind="scheduled", alert=True)
    assert recap["counts"]["incidents"] == 4
    assert recap["counts"]["types"] == {"fire": 1, "mva": 1, "ems": 1, "police": 1}
    assert recap["counts"]["towns"]["Easton"] == 2
    assert recap["text"] == ""  # no Ollama: counts and list only
    ids = [h["incident_id"] for h in recap["highlights"]]
    assert ids[0] == big
    assert near in ids
    first = recap["highlights"][0]
    assert first["important"] and first["updates"] == ["Fire knocked down."]
    close = next(h for h in recap["highlights"] if h["incident_id"] == near)
    assert close["close"] and not close["important"] and close["miles"] < 1
    # An MVA with no injuries isn't important, and isn't close.
    mva = next(h for h in recap["highlights"] if h["nature"] == "MVA")
    assert not mva["important"] and not mva["close"]

    assert len(alerter.sent) == 1
    kind, title, message, extra = alerter.sent[0]
    assert kind == "recap" and title == "12-hour summary · 7 PM"
    assert "Structure fire, 1234 Northampton St, Easton - Fire knocked down." in message
    assert "Fall, 12 Concord Cir, Forks" in message and "mi from home" in message
    assert "Route 22" not in message and "Theft" not in message
    assert extra["click"].endswith(f"/map?recap={recap['id']}")
    assert db.recap_for_slot(end)["alerted"] == 1
    assert "! " in recap["plain"] and "4 incidents: 1 fire, 1 MVA, 1 EMS, 1 police" in recap["plain"]


def test_quiet_hours_send_no_alert(tmp_path):
    config, db, alerter, maker = setup(tmp_path)
    end = datetime(2026, 11, 2, 7, 0).timestamp()
    add_incident(db, end - 3600, "Theft", "police", "Northampton Crossings, Easton", 40.70, -75.24, calls=3)
    recap = maker.make(end - 12 * 3600, end, kind="scheduled", alert=True)
    assert recap["counts"]["incidents"] == 1 and len(recap["highlights"]) == 1
    assert alerter.sent == []


def test_ollama_writes_the_recap(tmp_path):
    ollama = FakeOllama(answers=[{"recap": "A busy evening: a working fire on Northampton Street was knocked down."}])
    config, db, alerter, maker = setup(tmp_path, ollama)
    end = datetime(2026, 11, 2, 19, 0).timestamp()
    fill(db, end)
    recap = maker.make(end - 12 * 3600, end)
    assert recap["text"].startswith("A busy evening")
    prompt = ollama.prompts[0]
    assert "4 incidents: 1 fire, 1 MVA, 1 EMS, 1 police" in prompt
    assert "Structure fire, 1234 Northampton St, Easton, 20 calls, under control: Working fire" in prompt
    assert "Fire knocked down." in prompt
    assert "Nazareth" not in prompt
    # Made with the button: no phone alert.
    assert alerter.sent == []


def test_ollama_down_still_saves_counts(tmp_path):
    config, db, alerter, maker = setup(tmp_path, FakeOllama(down=True))
    end = datetime(2026, 11, 2, 19, 0).timestamp()
    fill(db, end)
    recap = maker.make(end - 12 * 3600, end)
    assert recap["text"] == "" and "Ollama" in recap["problem"]
    assert recap["counts"]["incidents"] == 4


def test_scheduled_once_and_caught_up_after_restart(tmp_path, monkeypatch):
    config, db, alerter, maker = setup(tmp_path)
    slot = datetime(2026, 11, 2, 19, 0).timestamp()
    fill(db, slot)
    # Not yet: it waits a couple of minutes after 7 PM.
    assert maker.make_due(slot + 30) is None
    made = maker.make_due(slot + recaps.SETTLE_S + 5)
    assert made and made["end"] == slot
    assert maker.make_due(slot + recaps.SETTLE_S + 60) is None
    assert len(db.recaps()) == 1
    # Too long ago (the PC was off all night): skipped, not made late.
    assert maker.make_due(slot + 11 * 3600) is None


def test_api_lists_and_makes_recaps(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    app = create_app(config, backend_factory=lambda _config: FakeBackend())
    with TestClient(app) as client:
        db = app.state.db
        add_incident(db, int(time.time()) - 3600, "Structure fire", "fire", "1 Main St, Easton", 40.69, -75.21,
                     calls=6)
        made = client.post("/api/recaps", json={}).json()
        assert made["counts"]["incidents"] == 1 and made["kind"] == "manual"
        listed = client.get("/api/recaps").json()
        assert listed[0]["id"] == made["id"] and listed[0]["highlights"][0]["label"] == "1 Main St, Easton"
        assert client.get(f"/api/recaps/{made['id']}").json()["plain"] == made["plain"]
        assert client.post("/api/recaps", json={"hours": 0}).status_code == 400
        assert client.post("/api/recaps", content=b"").status_code == 200
