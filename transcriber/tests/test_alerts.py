"""Phone alerts: what sends one, and the addresses the ntfy phone app reads them from."""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from sdrtrunk_transcriber import alerts
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.server import create_app

from test_incidents import GEO, Radio
from test_service import CALL, make_mp3, post, sdrtrunk_body

BASE_URL = "https://desktop.tail1234.ts.net"


class FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, headers))
        return self

    def raise_for_status(self):
        pass


@pytest.fixture
def wakeups(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(alerts.requests, "Session", lambda: session)
    return session.posts


def make_config(tmp_path, **alert_settings):
    (tmp_path / "places-geo.json").write_text(json.dumps(GEO), encoding="utf-8")
    config = Config()
    config.base_dir = tmp_path
    config.map.online_lookup = False
    config.alerts.enabled = True
    config.alerts.base_url = BASE_URL
    for name, value in alert_settings.items():
        setattr(config.alerts, name, value)
    return config


def lines(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines()]


def test_the_ntfy_app_can_read_alerts_and_the_iphone_is_woken(tmp_path, wakeups):
    client = TestClient(create_app(make_config(tmp_path), backend_factory=lambda _c: Radio([])))
    with client:
        auth = client.get("/sdrtrunk-alerts/auth")
        first = client.app.state.alerter.send("test", "Test alert", "one", tags=["white_check_mark"])
        second = client.app.state.alerter.send("test", "Test alert", "two")
        client.app.state.alerter.flush()
        everything = lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1, "since": "all"}))
        newer = lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1, "since": first["id"]}))
        one = client.get("/sdrtrunk-alerts/json", params={"poll": 1, "id": first["id"]}).json()
        # Sending a test alert is only allowed from the PC itself; the test client isn't.
        assert client.post("/api/alerts/test").status_code == 403

    assert auth.json() == {"success": True}
    assert [m["message"] for m in everything] == ["one", "two"]
    assert [m["id"] for m in newer] == [second["id"]]
    assert one["id"] == first["id"] and one["event"] == "message" and one["topic"] == "sdrtrunk-alerts"
    assert one["tags"] == ["white_check_mark"] and isinstance(one["time"], int)
    # What a self-hosted ntfy server sends upstream: the hash of the topic's address, and only the id.
    topic_hash = hashlib.sha256(f"{BASE_URL}/sdrtrunk-alerts".encode()).hexdigest()
    assert wakeups == [(f"https://ntfy.sh/{topic_hash}", {"X-Poll-ID": first["id"]}),
                       (f"https://ntfy.sh/{topic_hash}", {"X-Poll-ID": second["id"]})]


def test_big_incidents_and_chosen_stations_alert(tmp_path, wakeups):
    config = make_config(tmp_path, stations=["Station 4"])
    radio = Radio([
        "Station 4 Engine, respond North Third and Northampton for a structure fire, smoke showing.",
        "Engine 41 on location, North 3rd and Northampton, working fire.",
        "Medic 49 responding Main and Center in Nazareth for a fall.",
        "Crash on Route 22 at Freemansburg Ave, two cars, airbags deployed.",
    ])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        client.post("/api/page", json={"dateTime": now - 30, "toneA": 349.0, "toneB": 832.5, "label": "Station 4"})
        client.post("/api/page", json={"dateTime": now - 25, "toneA": 349.0, "toneB": 832.5, "label": "Station 4"})
        client.post("/api/page", json={"dateTime": now - 20, "toneA": 500.0, "toneB": 900.0, "label": "Station 9"})
        for offset in (0, 60, 120, 180):
            post(client, sdrtrunk_body({**CALL, "dateTime": str(now - 200 + offset)}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        client.app.state.alerter.flush()
        sent = lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1}))

    assert [m["title"] for m in sent][:2] == ["Tone-out: Station 4", "Structure fire"]
    page, fire, crash = sent
    assert crash["title"] in ("MVA", "Vehicle crash")
    assert "349 / 832.5 Hz" in page["message"]
    assert fire["message"].startswith("North 3rd Street & Northampton Street, Easton\nStation 4 Engine")
    assert fire["tags"] == ["fire"] and fire["click"] == f"{BASE_URL}/map" and fire["priority"] == 4
    assert crash["tags"] == ["car"] and "Route 22" in crash["message"]
    assert len(wakeups) == 3


def test_old_calls_do_not_alert(tmp_path, wakeups):
    config = make_config(tmp_path)
    radio = Radio(["Crash on Route 22 at Freemansburg Ave."])
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        post(client, sdrtrunk_body({**CALL, "dateTime": str(int(time.time()) - 3600)}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        incidents = client.get("/api/incidents", params={"since": 0}).json()
        sent = lines(client.get("/sdrtrunk-alerts/json", params={"poll": 1}))
    assert len(incidents) == 1 and sent == [] and wakeups == []


def test_no_alert_addresses_when_turned_off(tmp_path):
    config = make_config(tmp_path, enabled=False)
    client = TestClient(create_app(config, backend_factory=lambda _c: Radio([])))
    with client:
        assert client.get("/sdrtrunk-alerts/json").status_code == 404


def test_topic_must_be_a_plain_name(tmp_path):
    config = make_config(tmp_path, topic="my alerts/../x")
    with pytest.raises(ValueError):
        with TestClient(create_app(config, backend_factory=lambda _c: Radio([]))):
            pass
