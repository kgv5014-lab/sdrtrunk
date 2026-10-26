"""End-to-end tests: SDRTrunk-shaped uploads in, call log out, with a fake Whisper backend."""

import io
import math

import av
import pytest
from fastapi.testclient import TestClient

from sdrtrunk_transcriber import backends
from sdrtrunk_transcriber.backends import Backend, Segment, Transcript, clean_segments
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.server import SUCCESS_REPLY, create_app

BOUNDARY = "--sdrtrunk-sdrtrunk-sdrtrunk"


def make_mp3(seconds: float, rate: int = 8000) -> bytes:
    """An 8 kHz mono MP3 tone, like SDRTrunk's temporary stream recordings."""
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=rate, layout="mono")
        samples = int(seconds * rate)
        import numpy as np
        tone = (0.3 * np.sin(2 * math.pi * 440 * np.arange(samples) / rate) * 32767).astype("<i2")
        frame = av.AudioFrame.from_ndarray(tone.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def sdrtrunk_body(parts: dict, audio: bytes | None = None, audio_name: str = "20261022_081403.mp3") -> bytes:
    """Byte-for-byte the multipart layout RdioScannerBuilder produces: plain parts first, then the
    file part with filename before name and no Content-Type header."""
    out = io.BytesIO()
    for key, value in parts.items():
        out.write(f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    if audio is not None:
        out.write(f"--{BOUNDARY}\r\nContent-Disposition: form-data; filename=\"{audio_name}\"; "
                  f"name=\"audio\"\r\n\r\n".encode())
        out.write(audio)
    out.write(f"\r\n--{BOUNDARY}--\r\n".encode())
    return out.getvalue()


def post(client, body: bytes, path="/api/call-upload"):
    return client.post(path, content=body, headers={
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "User-Agent": "sdrtrunk",
    })


CALL = {
    "key": "secret",
    "system": "11",
    "dateTime": "1792656843",
    "talkgroup": "1203",
    "source": "4521004",
    "frequency": "851262500",
    "talkerAlias": "ENGINE 12",
    "talkgroupLabel": "Fire Dispatch",
    "talkgroupGroup": "Fire",
    "systemLabel": "County P25",
    "patches": "[]",
}


class FakeBackend(Backend):
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.prompts = []

    def transcribe(self, samples, prompt):
        self.prompts.append(prompt)
        return Transcript(f"heard {len(samples)} samples", -0.25)


@pytest.fixture
def make_client(tmp_path):
    def factory(api_key="", backend=None, vocabulary=None):
        config = Config()
        config.base_dir = tmp_path
        config.server.api_key = api_key
        if vocabulary is not None:
            (tmp_path / "vocabulary.txt").write_text(vocabulary, encoding="utf-8")
        fake = backend or FakeBackend()
        app = create_app(config, backend_factory=lambda _config: fake)
        return TestClient(app), fake
    return factory


def test_connection_test_matches_what_sdrtrunk_expects(make_client):
    client, _ = make_client()
    with client:
        response = post(client, sdrtrunk_body({"key": "secret", "system": "11", "test": "1"}))
    # RdioScannerBroadcaster.connected(): lowercased reply must start with this.
    assert response.text.lower().startswith("incomplete call data: no talkgroup")


def test_upload_is_logged_and_transcribed(make_client, tmp_path):
    client, fake = make_client(api_key="secret", vocabulary="# comment\nEngine 12\n\nMain Street\n")
    with client:
        response = post(client, sdrtrunk_body(CALL, make_mp3(3.0)))
        assert response.status_code == 200
        assert SUCCESS_REPLY in response.text

        assert client.app.state.worker.wait_idle()
        calls = client.get("/api/calls").json()
        audio = client.get(f"/api/calls/{calls[0]['id']}/audio")

    assert len(calls) == 1
    call = calls[0]
    assert call["status"] == "done"
    assert call["talkgroup"] == "1203"
    assert call["talkgroup_label"] == "Fire Dispatch"
    assert call["source"] == "4521004"
    assert call["talker_alias"] == "ENGINE 12"
    assert call["frequency"] == 851262500
    assert call["system_label"] == "County P25"
    assert call["call_time"] == 1792656843
    assert 2.8 < call["duration_s"] < 3.3
    assert call["transcript"].startswith("heard ")
    assert call["confidence"] == -0.25
    assert call["backend"] == "fake"
    assert fake.prompts == ["Engine 12, Main Street"]
    assert audio.status_code == 200 and audio.content[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xe3")
    assert (tmp_path / "data" / call["audio_path"]).exists()


def test_bare_host_path_also_accepts_uploads(make_client):
    client, _ = make_client()
    with client:
        response = post(client, sdrtrunk_body(CALL, make_mp3(2.0)), path="/")
    assert SUCCESS_REPLY in response.text


def test_wrong_api_key_is_rejected(make_client):
    client, _ = make_client(api_key="right")
    with client:
        response = post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        assert client.get("/api/calls").json() == []
    assert response.status_code == 401


def test_short_calls_are_logged_but_skipped(make_client):
    client, fake = make_client()
    with client:
        post(client, sdrtrunk_body(CALL, make_mp3(0.4)))
        assert client.app.state.worker.wait_idle()
        call = client.get("/api/calls").json()[0]
    assert call["status"] == "skipped"
    assert fake.prompts == []


def test_search_filters(make_client):
    client, _ = make_client()
    with client:
        post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        post(client, sdrtrunk_body({**CALL, "talkgroup": "2001", "talkgroupLabel": "Police Main",
                                    "talkerAlias": ""}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        everything = client.get("/api/calls").json()
        police = client.get("/api/calls", params={"talkgroup": "police"}).json()
        by_tg = client.get("/api/calls", params={"talkgroup": "1203"}).json()
        by_text = client.get("/api/calls", params={"q": "heard"}).json()
        no_match = client.get("/api/calls", params={"q": "ambulance"}).json()
        status = client.get("/api/status").json()

    assert [c["talkgroup"] for c in everything] == ["2001", "1203"]  # newest first
    assert [c["talkgroup"] for c in police] == ["2001"]
    assert [c["talkgroup"] for c in by_tg] == ["1203"]
    assert len(by_text) == 2
    assert no_match == []
    assert status["calls"] == {"done": 2}


def test_backend_failure_marks_call_failed_and_keeps_going(make_client):
    class Flaky(FakeBackend):
        def transcribe(self, samples, prompt):
            if not self.prompts:
                self.prompts.append(prompt)
                raise RuntimeError("GPU fell over")
            return super().transcribe(samples, prompt)

    client, _ = make_client(backend=Flaky())
    with client:
        post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        calls = client.get("/api/calls").json()
    assert [c["status"] for c in calls] == ["done", "failed"]
    assert "GPU fell over" in calls[1]["error"]


def test_unfinished_calls_resume_after_restart(make_client):
    # First run: stop the worker so the upload stays queued, as if the service crashed.
    client, _ = make_client()
    with client:
        db = client.app.state.db
        client.app.state.worker.stop()
        post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        assert db.get_call(1)["status"] == "queued"

    client2, fake2 = make_client()
    with client2:
        assert client2.app.state.worker.wait_idle()
        call = client2.get("/api/calls").json()[0]
    assert call["status"] == "done"
    assert len(fake2.prompts) == 1


def test_clean_segments_drops_silence_and_youtube_phrases():
    result = clean_segments([
        Segment(" Engine 12 responding.", -0.3, 0.01),
        Segment(" Thanks for watching!", -0.4, 0.2),
        Segment(" mumble", -1.6, 0.9),
        Segment(" Thank you.", -0.2, 0.05),
    ], no_speech_threshold=0.6, log_prob_threshold=-1.0)
    assert result.text == "Engine 12 responding. Thank you."
    assert result.confidence == -0.25


def test_unknown_backend_name_is_an_error():
    config = Config()
    config.transcription.backend = "nope"
    with pytest.raises(ValueError):
        backends.create_backend(config)


def test_whisper_cpp_backend_speaks_the_server_protocol():
    """Runs a stand-in for whisper.cpp's whisper-server that answers like its verbose_json output."""
    import json
    import threading
    from email.parser import BytesParser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import numpy as np

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser().parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            for part in message.get_payload():
                name = part.get_param("name", header="content-disposition")
                seen[name] = part.get_payload(decode=True)
            seen["path"] = self.path
            reply = json.dumps({
                "task": "transcribe", "language": "english", "duration": 2.0, "text": " Engine 12 en route.",
                "segments": [
                    {"id": 0, "text": " Engine 12 en route.", "start": 0.0, "end": 1.5,
                     "temperature": 0.0, "avg_logprob": -0.2, "no_speech_prob": 0.01},
                    {"id": 1, "text": " Thanks for watching!", "start": 1.5, "end": 2.0,
                     "temperature": 0.0, "avg_logprob": -0.5, "no_speech_prob": 0.3},
                ],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = Config()
        config.whisper_cpp.url = f"http://127.0.0.1:{server.server_port}/inference"
        config.transcription.backend = "whisper-cpp"
        backend = backends.create_backend(config)
        result = backend.transcribe(np.zeros(32000, dtype=np.float32), "Engine 12, Main Street")
    finally:
        server.shutdown()

    assert seen["path"] == "/inference"
    assert seen["file"][:4] == b"RIFF"
    assert seen["response_format"] == b"verbose_json"
    assert seen["language"] == b"en"
    assert seen["prompt"] == b"Engine 12, Main Street"
    assert seen["no_language_probabilities"] == b"true"
    assert result.text == "Engine 12 en route."
    assert result.confidence == -0.2


def test_faster_whisper_backend_passes_gpu_settings(monkeypatch):
    import sys
    import types

    import numpy as np

    created = {}

    class FakeModel:
        def __init__(self, model, **kwargs):
            created["model"] = model
            created.update(kwargs)

        def transcribe(self, samples, **kwargs):
            created["transcribe"] = kwargs
            seg = types.SimpleNamespace(text=" Medic 4 on scene.", avg_logprob=-0.3, no_speech_prob=0.02)
            return iter([seg]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    config = Config()
    config.faster_whisper.device_index = 1
    backend = backends.create_backend(config)
    result = backend.transcribe(np.zeros(16000, dtype=np.float32), "Medic 4")

    assert created["model"] == "large-v3-turbo"
    assert created["device"] == "cuda"
    assert created["device_index"] == 1
    assert created["compute_type"] == "int8"
    assert created["transcribe"]["condition_on_previous_text"] is False
    assert created["transcribe"]["vad_filter"] is True
    assert created["transcribe"]["initial_prompt"] == "Medic 4"
    assert created["transcribe"]["language"] == "en"
    # No tokenizer on the fake model: English only falls back to cleaning transcripts afterwards
    assert created["transcribe"]["suppress_tokens"] == [-1]
    # Radio speech scores low on the voice detector; its 0.5 default blanked real calls
    assert created["transcribe"]["vad_parameters"] == {"threshold": 0.2, "min_silence_duration_ms": 300}
    # A one second call can't run on into a minutes-long loop, and retries stop at 0.4
    assert created["transcribe"]["max_new_tokens"] == 24
    assert created["transcribe"]["temperature"] == [0.0, 0.2, 0.4]
    assert result.text == "Medic 4 on scene."


def test_token_limit_grows_with_the_call_up_to_one_window():
    assert backends.token_limit(1.0, 8.0) == 24
    assert backends.token_limit(0.3, 8.0) == 24
    assert backends.token_limit(10.0, 8.0) == 96
    # Whisper hears 30 s at a time and holds 448 tokens per window, half of them prompt
    assert backends.token_limit(30.0, 8.0) == 216
    assert backends.token_limit(108.0, 8.0) == 216
    assert backends.token_limit(10.0, 0) is None


def test_tone_only_call_is_labelled_not_left_as_no_speech(make_client):
    class Silent(FakeBackend):
        def transcribe(self, samples, prompt):
            return Transcript("", None)

    client, _ = make_client(backend=Silent())
    with client:
        # make_mp3 is a steady 440 Hz tone, like a paging tone or alert beep
        post(client, sdrtrunk_body(CALL, make_mp3(3.0)))
        assert client.app.state.worker.wait_idle()
        call = client.get("/api/calls").json()[0]

    assert call["status"] == "done"
    assert call["transcript"] == ""
    assert call["error"] == "tones only"


def test_codes_are_cleaned_up_and_labelled(make_client, tmp_path):
    import shutil
    from pathlib import Path

    shutil.copy(Path(__file__).resolve().parents[1] / "codes.example.csv", tmp_path / "codes.csv")

    class Radio(FakeBackend):
        def transcribe(self, samples, prompt):
            self.prompts.append(prompt)
            return Transcript("2081 ten twenty three, ten four.", -0.2)

    client, fake = make_client(backend=Radio(), vocabulary="Route 22\n")
    with client:
        post(client, sdrtrunk_body(CALL, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        call = client.get("/api/calls").json()[0]
        found = client.get("/api/calls", params={"q": "10-23"}).json()

    assert call["transcript"] == "2081 10-23, 10-4."
    assert call["transcript_raw"] == "2081 ten twenty three, ten four."
    assert [c["code"] for c in call["codes"]] == ["2081", "10-23", "10-4"]
    assert call["codes"][0]["meaning"] == "Easton Fire/EMS, Ambulance"
    assert "10-23" in fake.prompts[0] and fake.prompts[0].endswith("Route 22")
    assert len(found) == 1


def test_old_call_log_gets_new_columns(tmp_path):
    import sqlite3

    from sdrtrunk_transcriber.db import Database

    path = tmp_path / "calls.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, call_time INTEGER NOT NULL, "
                "received_at INTEGER NOT NULL, system_id TEXT, system_label TEXT, talkgroup TEXT, "
                "talkgroup_label TEXT, talkgroup_group TEXT, source TEXT, talker_alias TEXT, frequency INTEGER, "
                "patches TEXT, audio_path TEXT NOT NULL, duration_s REAL, transcript TEXT, confidence REAL, "
                "status TEXT NOT NULL DEFAULT 'queued', error TEXT, backend TEXT, model TEXT, transcribe_ms INTEGER)")
    old.execute("INSERT INTO calls (call_time, received_at, audio_path, transcript, status) "
                "VALUES (1, 1, 'a.mp3', 'old call', 'done')")
    old.commit()
    old.close()

    db = Database(path)
    db.update_call(1, transcript_raw="old call")
    assert db.get_call(1)["transcript_raw"] == "old call"
    db.close()


# Byte-for-byte what SDRTrunk's TwoTonePageSender.toJson produces (see TwoToneTableTest.pageJson).
SDRTRUNK_PAGE = ('{"key":"","dateTime":1792656843.25,"toneA":349.0,"toneB":832.5,"toneASeconds":1.0,'
                 '"label":"Station 4","system":"Northampton","site":null,"channel":"Fire Dispatch",'
                 '"frequency":154190000}')


def post_page(client, body: str):
    return client.post("/api/page", content=body, headers={"Content-Type": "application/json"})


def test_tone_out_pages_are_logged(make_client):
    client, _ = make_client()
    with client:
        assert post_page(client, SDRTRUNK_PAGE).status_code == 200
        long_tone = '{"key":"","dateTime":1792656900,"toneA":1084.0,"toneB":null,"label":null,"channel":"Fire"}'
        assert post_page(client, long_tone).status_code == 200
        pages = client.get("/api/pages").json()
        between = client.get("/api/pages", params={"since": 1792656800, "until": 1792656850}).json()
        searched = client.get("/api/pages", params={"q": "station 4"}).json()

    assert [p["tone_a"] for p in pages] == [1084.0, 349.0]
    assert pages[0]["tone_b"] is None and pages[0]["label"] is None
    page = pages[1]
    assert page["tone_b"] == 832.5
    assert page["label"] == "Station 4"
    assert page["channel"] == "Fire Dispatch"
    assert page["system_label"] == "Northampton"
    assert page["frequency"] == 154190000
    assert page["page_time"] == 1792656843.25
    assert [p["id"] for p in between] == [page["id"]]
    assert [p["id"] for p in searched] == [page["id"]]


def test_bad_pages_are_refused(make_client):
    client, _ = make_client()
    with client:
        assert post_page(client, "not json").status_code == 400
        assert post_page(client, "[1, 2]").status_code == 400
        assert post_page(client, '{"toneB": 832.5}').status_code == 400
        assert client.get("/api/pages").json() == []


def test_pages_from_another_computer_need_the_key(make_client):
    # The test client is not a loopback address, so it counts as another computer.
    client, _ = make_client(api_key="secret")
    with client:
        assert post_page(client, SDRTRUNK_PAGE).status_code == 401
        assert post_page(client, SDRTRUNK_PAGE.replace('"key":""', '"key":"secret"')).status_code == 200


def test_loopback_detection():
    from sdrtrunk_transcriber.server import _is_loopback

    assert _is_loopback("127.0.0.1") and _is_loopback("::1") and _is_loopback("localhost")
    assert not _is_loopback("192.168.1.20") and not _is_loopback(None) and not _is_loopback("testclient")


def test_streets_are_hinted_and_cleaned_up(make_client, tmp_path):
    import shutil
    from pathlib import Path

    shutil.copy(Path(__file__).resolve().parents[1] / "places.example.csv", tmp_path / "places.csv")

    class Radio(FakeBackend):
        def transcribe(self, samples, prompt):
            self.prompts.append(prompt)
            return Transcript("Engine 2011 to 1201 Northamton Street off route twenty two.", -0.2)

    client, fake = make_client(backend=Radio(), vocabulary="Medic 2081\n")
    with client:
        post(client, sdrtrunk_body({**CALL, "talkgroupLabel": "Easton Fire"}, make_mp3(2.0)))
        assert client.app.state.worker.wait_idle()
        call = client.get("/api/calls").json()[0]

    assert call["transcript"] == "Engine 2011 to 1201 Northampton Street off Route 22."
    assert "Larry Holmes Drive" in fake.prompts[0] and fake.prompts[0].endswith("Medic 2081")


def test_gpu_picked_by_name_overrides_device_index(tmp_path, monkeypatch):
    from sdrtrunk_transcriber.config import load_config
    path = tmp_path / "config.toml"
    path.write_text("[faster_whisper]\ndevice_index = 1\n", encoding="utf-8")
    assert load_config(path).faster_whisper.device_index == 1
    monkeypatch.setenv("WHISPER_DEVICE_INDEX", "0")
    assert load_config(path).faster_whisper.device_index == 0


def test_fire_and_ems_calls_are_transcribed_before_police_when_calls_pile_up(make_client):
    import threading

    gate = threading.Event()
    client, _ = make_client()
    labels = ["Co PD S1 Patch", "Co PD N1 Patch", "Norco Ops 1", "Co PD E1 Patch", "Countywide"]
    with client:
        worker = client.app.state.worker
        order = []

        def process(call_id):
            gate.wait(10)
            order.append(worker.db.get_call(call_id)["talkgroup_label"])

        worker._process = process
        for i, label in enumerate(labels):
            post(client, sdrtrunk_body(dict(CALL, talkgroupLabel=label, talkgroupGroup="", talkgroup=str(1000 + i)), make_mp3(1.5)))
        gate.set()
        assert worker.wait_idle()

    # The first call was already being worked on; then Countywide, Ops, and the police calls oldest first
    assert order == ["Co PD S1 Patch", "Countywide", "Norco Ops 1", "Co PD N1 Patch", "Co PD E1 Patch"]
    assert worker.rank({"talkgroup_label": "Nancy FD"}) == 2
    assert worker.rank({"talkgroup_label": "Co PD S1 Patch"}) == len(worker.config.transcription.first_talkgroups)
