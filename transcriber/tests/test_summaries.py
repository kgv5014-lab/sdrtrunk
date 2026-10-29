"""Incident and call summaries from a (fake) local Ollama."""

import json
import time

import pytest
import requests
from fastapi.testclient import TestClient

from sdrtrunk_transcriber import summaries
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.server import create_app
from sdrtrunk_transcriber.summaries import Summarizer, clean_tags, clip_words, update_note

from test_incidents import GEO, Radio
from test_service import CALL, make_mp3, post, sdrtrunk_body


class FakeOllama:
    """Answers /api/chat like Ollama does, recording each prompt."""

    def __init__(self, answers=None, down=False):
        self.prompts = []
        self.requests = []
        self.answers = answers or []
        self.down = down

    def post(self, url, json=None, timeout=None):
        if self.down:
            raise requests.ConnectionError("refused")
        self.requests.append((url, json))
        prompt = json["messages"][-1]["content"]
        self.prompts.append(prompt)
        if self.answers:
            answer = self.answers.pop(0)
        elif "about one incident" in prompt:
            answer = {"summary": "Structure fire at North 3rd and Northampton, Engine 41 on location.",
                      "tags": ["working fire", "made up tag"], "status": "active"}
        else:
            answer = {"summary": "A long call.", "tags": ["traffic"]}
        return FakeResponse({"message": {"role": "assistant", "content": _json.dumps(answer)}})


_json = json


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.text = _json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.body


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(summaries.requests, "Session", lambda: fake)
    return fake


def make_config(tmp_path, **settings):
    (tmp_path / "places-geo.json").write_text(json.dumps(GEO), encoding="utf-8")
    config = Config()
    config.base_dir = tmp_path
    config.map.online_lookup = False
    config.summaries.enabled = True
    config.summaries.delay_s = 0
    for name, value in settings.items():
        setattr(config.summaries, name, value)
    return config


LONG_CALL = ("County to all units, be advised Route 22 westbound is shut down at the 25th Street exit for "
             "a disabled tractor trailer, expect delays, PennDOT has been notified and is en route.")


def test_incidents_and_long_calls_get_summaries(tmp_path, ollama):
    config = make_config(tmp_path)
    radio = Radio([
        "Station 4 Engine, respond North Third and Northampton for a structure fire, smoke showing.",
        "Engine 41 on location, North 3rd and Northampton, working fire.",
        LONG_CALL,
    ])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        client.post("/api/page", json={"dateTime": now - 30, "toneA": 349.0, "toneB": 832.5, "label": "Station 4"})
        for offset in (0, 120, 200):
            post(client, sdrtrunk_body({**CALL, "dateTime": str(now + offset), "talkgroupLabel": "County Fire",
                                        "talkgroupGroup": ""}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        assert client.app.state.summarizer.wait_idle()
        incidents = client.get("/api/incidents", params={"since": now - 3600}).json()
        calls = client.get("/api/calls").json()
        status = client.get("/api/status").json()

    fire = incidents[0]
    assert fire["summary"].startswith("Structure fire at North 3rd")
    # Tags not on the list are dropped.
    assert fire["summary_tags"] == ["working fire"]
    assert fire["summary_status"] == "active" and fire["summary_current"] is True
    incident_prompts = [p for p in ollama.prompts if "about one incident" in p]
    # The last summary read both calls, the place and the tone-out.
    assert "Engine 41 on location" in incident_prompts[-1] and "smoke showing" in incident_prompts[-1]
    assert "North 3rd Street & Northampton Street, Easton" in incident_prompts[-1]
    assert "Tone-out" in incident_prompts[-1] and "Station 4" in incident_prompts[-1]
    long_call = next(c for c in calls if c["transcript"] == LONG_CALL)
    assert long_call["summary"] == "A long call." and long_call["summary_tags"] == ["traffic"]
    # Short calls don't get their own summary.
    assert all("summary" not in c for c in calls if c is not long_call)
    assert status["summaries"]["enabled"] and status["summaries"]["reachable"] is True
    url, body = ollama.requests[0]
    assert url == "http://127.0.0.1:11434/api/chat"
    assert body["model"] == "qwen2.5:7b" and body["stream"] is False and body["keep_alive"] == -1
    assert body["format"]["properties"]["tags"]["items"]["enum"] == list(summaries.TAGS)


def test_an_incident_is_summarized_again_only_when_its_calls_change(tmp_path, ollama):
    config = make_config(tmp_path)
    db = Database(tmp_path / "calls.db")
    t = int(time.time())
    incident_id = db.create_incident({"first_time": t, "last_time": t, "lat": 40.69, "lon": -75.21,
                                      "label": "Main Street", "category": "ems", "nature": "Fall"})

    def add(transcript, offset):
        call_id = db.insert_call({"call_time": t + offset, "received_at": t, "talkgroup": "1",
                                  "talkgroup_label": "EMS", "audio_path": "x.mp3"})
        db.update_call(call_id, status="done", transcript=transcript)
        db.add_call_place({"call_id": call_id, "incident_id": incident_id, "kind": "address", "precision": "address"})

    summarizer = Summarizer(config, db)
    add("Medic 2081 respond 100 Main Street for a fall.", 0)
    summarizer.summarize_incident(incident_id)
    summarizer.summarize_incident(incident_id)
    assert len(ollama.prompts) == 1
    add("Medic 2081 transporting BLS to St. Luke's.", 60)
    summarizer.summarize_incident(incident_id)
    assert len(ollama.prompts) == 2 and "transporting BLS" in ollama.prompts[-1]

    # Merged away by a fix on the map: its summary goes too.
    db._conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
    summarizer.summarize_incident(incident_id)
    assert db.get_summary("incident", incident_id) is None


def test_a_change_gets_an_update_note(tmp_path, ollama):
    config = make_config(tmp_path)
    db = Database(tmp_path / "calls.db")
    t = int(time.time()) - 600
    incident_id = db.create_incident({"first_time": t, "last_time": t, "lat": 40.69, "lon": -75.21,
                                      "label": "Main Street", "category": "fire", "nature": "Structure fire"})

    def add(transcript, offset, speaker=None):
        call_id = db.insert_call({"call_time": t + offset, "received_at": t, "talkgroup": "1",
                                  "talkgroup_label": "Fire", "audio_path": "x.mp3"})
        db.update_call(call_id, status="done", transcript=transcript, speaker_kind="unit" if speaker else None,
                       speaker=speaker)
        db.add_call_place({"call_id": call_id, "incident_id": incident_id, "kind": "address", "precision": "address"})

    summarizer = Summarizer(config, db)
    add("Station 41 respond 100 Main Street for a structure fire.", 0)
    summarizer.summarize_incident(incident_id)
    # The first summary is the start, not a change.
    assert "NEW" not in ollama.prompts[-1] and db.incident_updates([incident_id]) == {}

    add("Engine 41 to County, we have heavy fire showing, strike a second alarm.", 120, speaker="Engine 41")
    ollama.answers = [{"summary": "Working house fire, second alarm.", "tags": ["working fire"], "status": "active",
                       "update": "second alarm called"}]
    summarizer.summarize_incident(incident_id)
    prompt = ollama.prompts[-1]
    assert "Summary so far" in prompt and "Structure fire at North 3rd" in prompt
    assert "NEW " in prompt and "(Engine 41): Engine 41 to County" in prompt
    assert "NEW " not in prompt.split("Station 41 respond")[0].splitlines()[-1]

    # Routine traffic: no note. Going under control with no words from the model: says so.
    add("Engine 41 is on location.", 180)
    ollama.answers = [{"summary": "s", "tags": [], "status": "active", "update": "Units continue to operate."}]
    summarizer.summarize_incident(incident_id)
    add("Command to County, fire is knocked down, under control.", 900)
    ollama.answers = [{"summary": "s", "tags": [], "status": "under control", "update": ""}]
    summarizer.summarize_incident(incident_id)
    notes = db.incident_updates([incident_id])[incident_id]
    assert [n["note"] for n in notes] == ["Second alarm called.", "Under control."]
    assert notes[0]["time"] == t + 120 and notes[1]["time"] == t + 900

    # Gone from the map: its notes go too.
    db._conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
    summarizer.summarize_incident(incident_id)
    assert db.incident_updates([incident_id]) == {}


def test_update_notes_skip_repeats_and_filler():
    earlier = [{"note": "Second alarm called."}]
    assert update_note("Second alarm called", "active", "active", earlier) is None
    assert update_note("No new information.", "active", "active", []) is None
    assert update_note("N/A", "active", "active", []) is None
    assert update_note("", "active", "active", []) is None
    assert update_note("", "cleared", "active", []) == "Units cleared."
    assert update_note("patient transported to St. Luke's", "active", "active", earlier) == \
        "Patient transported to St. Luke's."


def test_updates_shown_with_incidents(tmp_path, ollama):
    config = make_config(tmp_path)
    radio = Radio(["Station 4 Engine, respond North Third and Northampton for a structure fire, smoke showing."])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        post(client, sdrtrunk_body({**CALL, "dateTime": str(now), "talkgroupLabel": "County Fire",
                                    "talkgroupGroup": ""}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        assert client.app.state.map_worker.wait_idle()
        assert client.app.state.summarizer.wait_idle()
        db = client.app.state.db
        incident_id = client.get("/api/incidents", params={"since": now - 3600}).json()[0]["id"]
        db.add_incident_update({"incident_id": incident_id, "time": now, "note": "Second alarm called.",
                                "status": "active", "made_at": now})
        incident = client.get("/api/incidents", params={"since": now - 3600}).json()[0]
    assert incident["updates"] == [{"time": now, "note": "Second alarm called."}]


def test_rebuilding_the_map_forgets_incident_summaries(tmp_path):
    db = Database(tmp_path / "calls.db")
    db.store_summary({"kind": "incident", "ref_id": 1, "basis": "x", "summary": "s", "made_at": 1})
    db.store_summary({"kind": "call", "ref_id": 1, "basis": "x", "summary": "s", "made_at": 1})
    db.add_incident_update({"incident_id": 1, "time": 1, "note": "n", "made_at": 1})
    db.clear_map()
    assert db.get_summary("incident", 1) is None and db.incident_updates([1]) == {}
    assert db.get_summary("call", 1) is not None


def test_ollama_not_running_changes_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(summaries.requests, "Session", lambda: FakeOllama(down=True))
    monkeypatch.setattr(summaries, "RETRY_S", 3600)
    config = make_config(tmp_path)
    radio = Radio([LONG_CALL])
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        deadline = time.monotonic() + 5
        while client.app.state.summarizer.reachable is not False and time.monotonic() < deadline:
            time.sleep(0.05)
        calls = client.get("/api/calls").json()
        status = client.get("/api/status").json()

    assert calls[0]["transcript"] == LONG_CALL and "summary" not in calls[0]
    assert status["summaries"]["reachable"] is False
    assert "not reachable" in status["summaries"]["error"]
    # Tried again later.
    assert status["summaries"]["queue"] == 1


def test_turned_off_by_default(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    config.map.enabled = False
    client = TestClient(create_app(config, backend_factory=lambda _c: Radio([])))
    with client:
        assert client.app.state.summarizer is None
        assert client.get("/api/status").json()["summaries"] == {"enabled": False}


def test_backlog_on_start(tmp_path, ollama):
    config = make_config(tmp_path)
    db = Database(tmp_path / "calls.db")
    t = int(time.time())
    old = db.insert_call({"call_time": t - 3 * 86400, "received_at": t, "audio_path": "a.mp3"})
    new = db.insert_call({"call_time": t - 60, "received_at": t, "audio_path": "b.mp3"})
    short = db.insert_call({"call_time": t - 60, "received_at": t, "audio_path": "c.mp3"})
    for call_id, text in ((old, LONG_CALL), (new, LONG_CALL), (short, "Medic 49 10-8.")):
        db.update_call(call_id, status="done", transcript=text)
    assert db.calls_needing_summary(t - 86400, 25) == [new]


def test_tags_and_length_are_kept_in_check():
    assert clean_tags(["injuries", "Injuries", "injuries", "made up", 3]) == ["injuries"]
    assert clean_tags("injuries") == []
    assert clip_words("one two three", 2) == "one two three"
    assert clip_words(" ".join(["word"] * 80), 40).endswith("…")


def test_a_model_still_downloading_is_tried_again(tmp_path, monkeypatch):
    class Downloading(FakeOllama):
        def post(self, url, json=None, timeout=None):
            if not self.requests:
                self.requests.append((url, json))
                return FakeResponse({"error": "model 'qwen2.5:7b' not found"}, status_code=404)
            return super().post(url, json, timeout)

    fake = Downloading()
    monkeypatch.setattr(summaries.requests, "Session", lambda: fake)
    monkeypatch.setattr(summaries, "RETRY_S", 0.2)
    config = make_config(tmp_path)
    db = Database(tmp_path / "calls.db")
    t = int(time.time())
    call_id = db.insert_call({"call_time": t, "received_at": t, "talkgroup": "1", "audio_path": "x.mp3"})
    db.update_call(call_id, status="done", transcript=LONG_CALL)
    summarizer = Summarizer(config, db)
    summarizer.start()
    summarizer.call_done(call_id)
    deadline = time.monotonic() + 5
    while db.get_summary("call", call_id) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    summarizer.stop()

    assert db.get_summary("call", call_id)["summary"] == "A long call."
    assert summarizer.reachable is True and summarizer.last_error is None
