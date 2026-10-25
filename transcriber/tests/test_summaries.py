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
from sdrtrunk_transcriber.summaries import Summarizer, clean_tags, clip_words

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


def test_rebuilding_the_map_forgets_incident_summaries(tmp_path):
    db = Database(tmp_path / "calls.db")
    db.store_summary({"kind": "incident", "ref_id": 1, "basis": "x", "summary": "s", "made_at": 1})
    db.store_summary({"kind": "call", "ref_id": 1, "basis": "x", "summary": "s", "made_at": 1})
    db.clear_map()
    assert db.get_summary("incident", 1) is None
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
