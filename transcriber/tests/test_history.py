"""Incident history: kept when the map is rebuilt, for the heat map page and the CSV download."""

import csv
import io
import json
import time

import pytest

from fastapi.testclient import TestClient

from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.history import HistoryKeeper, to_csv
from sdrtrunk_transcriber.incidents import dismissal
from sdrtrunk_transcriber.server import create_app

from test_grouping import call, make_worker
from test_incidents import GEO, Radio, gaz  # noqa: F401  (gaz is a fixture)
from test_service import CALL, make_mp3, post, sdrtrunk_body


def two_incidents(worker):
    fire = call(worker, "Station 4 Engine, respond North Third and Northampton for a structure fire.", 0)
    call(worker, "Engine 41 on location, North 3rd and Northampton, working fire.", 120)
    ems = call(worker, "Medic 49 responding Main and Center in Nazareth for a fall.", 300)
    return fire, ems


def test_history_is_kept_when_the_map_is_rebuilt(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    fire, ems = two_incidents(worker)
    keeper = HistoryKeeper(worker.db)
    assert keeper.sync()["saved"] == 2
    assert keeper.sync()["saved"] == 0          # nothing changed
    rows = {r["first_call_id"]: r for r in worker.db.history()}
    assert set(rows) == {fire, ems}
    assert rows[fire]["type"] == "fire" and rows[fire]["nature"] == "Structure fire" and rows[fire]["calls"] == 2
    assert rows[fire]["services"] == "fire"
    assert rows[ems]["type"] == "ems"

    # A rebuild empties the incidents; the history keeps them, then picks up the rebuilt ones.
    worker.db.clear_map()
    assert keeper.sync() == {"saved": 0, "gone": 0, "detached": 2}
    assert {r["first_call_id"]: r["incident_id"] for r in worker.db.history()} == {fire: None, ems: None}
    for call_id in range(fire, ems + 1):
        worker.process(call_id)
    keeper.sync()
    after = worker.db.history()
    assert [r["first_call_id"] for r in after] == [fire, ems]
    assert all(r["incident_id"] for r in after)


def test_merged_and_removed_incidents_leave_the_history(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    fire, ems = two_incidents(worker)
    keeper = HistoryKeeper(worker.db)
    keeper.sync()
    fire_incident = worker.db.incident_of_call(fire)
    ems_incident = worker.db.incident_of_call(ems)
    worker.merge(ems_incident, fire_incident)
    assert keeper.sync()["gone"] == 1
    assert [r["first_call_id"] for r in worker.db.history()] == [fire]
    assert worker.db.history()[0]["calls"] == 3

    worker.remove_call(fire)
    keeper.sync()
    # The rest of the incident now starts with its second call.
    assert fire not in {r["first_call_id"] for r in worker.db.history()}


def test_hidden_incidents_and_csv(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    fire, ems = two_incidents(worker)
    worker.db.update_incident(worker.db.incident_of_call(ems), hidden=1)
    HistoryKeeper(worker.db).sync()
    assert [r["first_call_id"] for r in worker.db.history()] == [fire]
    everything = worker.db.history(hidden=True)
    assert len(everything) == 2
    text = to_csv(everything)
    lines = list(csv.DictReader(io.StringIO(text)))
    assert lines[0]["nature"] == "Structure fire" and lines[0]["address"].endswith("Easton")
    assert lines[0]["removed_from_map"] == "" and lines[1]["removed_from_map"] == "yes"


def test_history_page_and_api(tmp_path):
    (tmp_path / "places-geo.json").write_text(json.dumps(GEO), encoding="utf-8")
    config = Config()
    config.base_dir = tmp_path
    config.map.online_lookup = False
    radio = Radio(["Station 4 Engine, respond North Third and Northampton for a structure fire, smoke showing.",
                   "Crash on Route 22 at Freemansburg Ave."])
    now = int(time.time())
    with TestClient(create_app(config, backend_factory=lambda _c: radio)) as client:
        for offset in (0, 600):
            post(client, sdrtrunk_body({**CALL, "dateTime": str(now + offset), "talkgroupLabel": "County Dispatch",
                                        "talkgroupGroup": ""}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        client.app.state.history.last_sync = 0
        data = client.get("/api/history").json()
        rows = [dict(zip(data["fields"], r)) for r in data["rows"]]
        entry = client.get(f"/api/history/{rows[0]['id']}").json()
        csv_all = client.get("/api/history.csv")
        csv_mva = client.get("/api/history.csv", params={"types": "mva"})
        page = client.get("/history")
        missing = client.get("/api/history/999999")
    assert [r["type"] for r in rows] == ["fire", "mva"]
    assert entry["call_list"][0]["transcript"].startswith("Station 4 Engine")
    assert csv_all.headers["content-disposition"].startswith("attachment")
    assert len(csv_all.text.strip().split("\r\n")) == 3
    assert len(csv_mva.text.strip().split("\r\n")) == 2 and "MVA" in csv_mva.text
    assert page.status_code == 200 and "leaflet-heat" in page.text
    assert missing.status_code == 404


def test_accidental_dial_comes_off_the_map_and_stays_in_history_marked(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    fire, ems = two_incidents(worker)
    incident = worker.db.incident_of_call(ems)
    # The unit names no place; it joins by unit and says it was nothing.
    call(worker, "Medic 49 to county, that was an accidental dial, we'll be clearing.", 420)
    row = worker.db.get_incident(incident)
    assert row["hidden"] == 1 and row["dismissed"] == "Accidental dial"
    assert [i["id"] for i in worker.db.incidents()] == [worker.db.incident_of_call(fire)]

    keeper = HistoryKeeper(worker.db)
    keeper.sync()
    assert [r["first_call_id"] for r in worker.db.history()] == [fire]
    kept = {r["first_call_id"]: r for r in worker.db.history(hidden=True)}
    assert kept[ems]["dismissed"] == "Accidental dial" and kept[ems]["hidden"] == 1
    lines = list(csv.DictReader(io.StringIO(to_csv(worker.db.history(hidden=True)))))
    assert [l["removed_from_map"] for l in lines] == ["", "Accidental dial"]

    # Put back by hand, it stays back even when more calls come in.
    worker.db.update_incident(incident, hidden=0)
    call(worker, "Medic 49, negative emergency, back in service.", 480)
    assert worker.db.get_incident(incident)["hidden"] == 0

    # A rebuild takes it off again by itself (the radio traffic still says so).
    worker.db.clear_map()
    worker._claims.clear()
    worker._released.clear()        # as rebuild() does
    for call_id in range(fire, ems + 3):
        worker.process(call_id)
    assert worker.db.get_incident(worker.db.incident_of_call(ems))["hidden"] == 1


def test_no_emergency_found(tmp_path, gaz):
    worker = make_worker(tmp_path, gaz)
    fire, _ = two_incidents(worker)
    call(worker, "Engine 41, North 3rd and Northampton, negative emergency, odor of burnt food.", 200)
    assert worker.db.get_incident(worker.db.incident_of_call(fire))["dismissed"] == "No emergency"


@pytest.mark.parametrize("text, reason", [
    ("Engine 41 to county, accidental dial, we are clearing", "Accidental dial"),
    ("Caller states it was a pocket dial", "Accidental dial"),
    ("she accidentally called 9-1-1", "Accidental dial"),
    ("County, child was playing with the phone", "Accidental dial"),
    ("Alarm company advises accidental activation", "Accidental alarm"),
    ("Medic 49, negative emergency, back in service", "No emergency"),
    ("PD on location, no emergency found", "No emergency"),
    ("Unfounded, clearing", "No emergency"),
    ("Units respond, 911 hang-up, 12 Main Street", None),     # the dispatch itself is not the answer
    ("Station 4 respond no emergency for the alarm", None),   # how to drive there
    ("This was not accidental, she needs help", None),
    ("MVA accident call Route 22", None),
    ("no emergency traffic on this channel", None),
])
def test_dismissal_words(text, reason):
    assert dismissal([{"transcript": text}]) == reason
