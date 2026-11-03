"""The Settings page: edits files safely, applies at once, finds new codes, and is closed to guests."""

import json
import time
from datetime import datetime

from fastapi.testclient import TestClient

from sdrtrunk_transcriber import live, settings_page, share
from sdrtrunk_transcriber.config import load_config
from sdrtrunk_transcriber.server import create_app
from test_service import FakeBackend, make_client  # noqa: F401 (fixture)
from test_share import PASSWORD, gate_client, no_wait, sign_in  # noqa: F401 (fixture)

CODES = "type,code,meaning,applies_to,prompt,source\r\nten-code,10-4,Acknowledged,all,yes,\r\nstation,45,Forks Fire,fire-ems,,\r\n"


def add_call(client, text, when=None, label="Countywide"):
    db = client.app.state.db
    call_id = db.insert_call({"call_time": when or time.time(), "received_at": time.time(), "talkgroup": "10000",
                              "talkgroup_label": label, "audio_path": "calls/x.mp3"})
    db.update_call(call_id, transcript=text, status="done")
    return call_id


def test_vocabulary_saves_with_backup_and_reaches_whisper(make_client, tmp_path):
    client, _ = make_client(vocabulary="Old Term\n")
    with client:
        r = client.put("/api/settings/vocabulary", json={"text": "Nancy Run\r\nTatamy\n\n"})
        assert r.status_code == 200 and r.json()["terms"] == 2
        assert (tmp_path / "vocabulary.txt").read_text() == "Nancy Run\nTatamy\n"
        assert client.app.state.worker.vocabulary == ["Nancy Run", "Tatamy"]
        backups = list((tmp_path / "data" / "settings-backups").glob("vocabulary.txt.*"))
        assert len(backups) == 1 and backups[0].read_text() == "Old Term\n"
        assert "Tatamy" in client.get("/api/settings").json()["vocabulary"]


def test_codes_edit_and_add_apply_at_once(make_client, tmp_path):
    (tmp_path / "codes.csv").write_text(CODES, encoding="utf-8")
    client, _ = make_client()
    with client:
        rows = client.get("/api/settings").json()["codes"]
        assert [r["code"] for r in rows] == ["10-4", "45"]
        rows.append({"type": "ten-code", "code": "10-50", "meaning": "Crash", "applies_to": "all"})
        assert client.put("/api/settings/codes", json={"rows": rows}).status_code == 200
        book = client.app.state.codebook
        assert "10-50" in book._codes
        bad = client.put("/api/settings/codes", json={"rows": [{"type": "nonsense", "code": "1", "meaning": "x"}]})
        assert bad.status_code == 400
        assert client.post("/api/settings/codes/add",
                           json={"type": "station", "code": "Station 97", "meaning": "Test Fire"}).status_code == 200
        assert book._stations["97"] == "Test Fire"
        assert "10-50" in book._codes  # nothing lost


def test_corrections_can_be_edited_and_deleted(make_client, tmp_path):
    (tmp_path / "corrections.csv").write_text(
        "heard,correct,talkgroup,hint,count,source\r\nnever,Nevin,,yes,3,learned\r\nPLS,BLS,,yes,1,hand-made\r\n",
        encoding="utf-8")
    client, _ = make_client()
    with client:
        rows = client.get("/api/settings").json()["corrections"]
        rows = [r for r in rows if r["heard"] != "PLS"]
        rows[0]["correct"] = "Nevin Avenue"
        assert client.put("/api/settings/corrections", json={"rows": rows}).json()["rows"] == 1
        items = client.app.state.corrections.items()
        assert [(c.heard, c.correct, c.count) for c in items] == [("never", "Nevin Avenue", 3)]


def test_departments_and_alert_distance_apply_now_and_after_restart(make_client, tmp_path):
    (tmp_path / "config.toml").write_text(
        '[alerts]\nzones = [{name = "Home", lat = 40.65, lon = -75.28, radius_miles = 0.5}]\n', encoding="utf-8")
    config = load_config(tmp_path / "config.toml")
    client = TestClient(create_app(config, backend_factory=lambda _config: FakeBackend()))
    with client:
        r = client.put("/api/settings/departments", json={"departments": [{"mhz": "453.3", "name": "Forks Fire"},
                                                                          {"mhz": "502.425", "name": "Nancy FD"}]})
        assert r.status_code == 200
        assert config.channels.name_for(453_300_000) == "Forks Fire"
        assert config.channels.name_for(460_375_000) is None
        assert client.put("/api/settings/departments", json={"departments": [{"mhz": "abc", "name": "x"}]}).status_code == 400
        r = client.put("/api/settings/alerts", json={"zone_radius_miles": "1.5", "close_miles": 3})
        assert r.status_code == 200 and r.json()["alerts"]["zone_radius_miles"] == 1.5
        assert config.recaps.close_miles == 3
        assert client.put("/api/settings/alerts", json={"close_miles": 500}).status_code == 400
    saved = json.loads((tmp_path / settings_page.OVERRIDES_FILE).read_text())
    assert saved["zone_radius_miles"] == 1.5
    again = load_config(tmp_path / "config.toml")
    assert again.alerts.zones[0]["radius_miles"] == 1.5 and again.alerts.zones[0]["name"] == "Home"
    assert again.recaps.close_miles == 3
    assert again.channels.name_for(502_425_000) == "Nancy FD"


def test_new_codes_lists_unknown_codes_heard_twice(make_client, tmp_path):
    (tmp_path / "codes.csv").write_text(CODES, encoding="utf-8")
    client, _ = make_client()
    noon = datetime.now().replace(hour=12, minute=0).timestamp()
    with client:
        add_call(client, "Engine 4511 and Engine 9711 responding, 10-4", noon)
        add_call(client, "Station 97 tone, 10-50 on Route 22", noon)
        add_call(client, "10-50 with injuries, Engine 9712", noon)
        add_call(client, "Signal 9 once", noon)
        # The dispatch time at 10:28, not a code.
        add_call(client, "Ops 1, 10-28", datetime.now().replace(hour=10, minute=28).timestamp())
        add_call(client, "Ops 1, 10-28", datetime.now().replace(hour=10, minute=29).timestamp())
        found = {c["code"]: c for c in client.get("/api/settings/new-codes").json()["codes"]}
        assert set(found) == {"10-50", "Station 97"}
        assert found["Station 97"]["count"] == 3 and found["Station 97"]["type"] == "station"
        assert len(found["10-50"]["examples"]) == 2
        client.post("/api/settings/new-codes/ignore", json={"code": "10-50"})
        assert [c["code"] for c in client.get("/api/settings/new-codes").json()["codes"]] == ["Station 97"]


def test_guests_on_the_share_link_cannot_reach_settings(make_client, tmp_path):
    client, _ = make_client()
    data = tmp_path / "data"
    share.set_password(data, PASSWORD)
    with client:
        guest = gate_client(client, data)
        prefix = f"/g/{share.read_path(data)}"
        sign_in(guest, prefix)
        assert guest.get(prefix + "/settings").status_code == 404
        assert guest.get(prefix + "/api/settings").status_code == 404
        assert guest.put(prefix + "/api/settings/vocabulary", json={"text": "x"}).status_code == 405
        public = live.create_public_app(client.app, data)
        assert TestClient(public).get("/api/settings").status_code == 404
