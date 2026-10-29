"""Live audio: calls queued per phone, the endless MP3 stream, and the secret key on the public page."""

import asyncio
import io
import time

import av
import numpy as np
from fastapi.testclient import TestClient

from sdrtrunk_transcriber import live
from sdrtrunk_transcriber.live import LiveHub
from test_service import CALL, make_client, make_mp3, post, sdrtrunk_body  # noqa: F401 (fixture)


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * live.RATE)) / live.RATE
    return (0.3 * np.sin(2 * np.pi * 600 * t)).astype(np.float32)


def decode(data: bytes) -> np.ndarray:
    with av.open(io.BytesIO(data), format="mp3") as container:
        # MP3 decodes to float samples in [-1, 1].
        return np.concatenate([f.to_ndarray().reshape(-1) for f in container.decode(audio=0)]).astype(np.float32)


def first_chunks(hub, listener, count):
    async def take():
        out = []
        stream = hub.stream(listener)
        async for data in stream:
            out.append(data)
            if len(out) == count:
                await stream.aclose()
                return out
    return asyncio.run(take())


def test_calls_go_only_to_phones_listening_to_that_talkgroup():
    hub = LiveHub()
    fire = hub.join("a", {"1203"})
    everything = hub.join("b", None)
    assert hub.wants("1203") and hub.wants("9999")

    hub.add_call({"id": 1, "talkgroup": "1203", "talkgroup_label": "Fire Dispatch", "call_time": 5}, tone(1))
    hub.add_call({"id": 2, "talkgroup": "9999", "call_time": 6}, tone(1))

    assert [c["id"] for c in fire.waiting] == [1]
    assert [c["id"] for c in everything.waiting] == [1, 2]
    assert everything.waiting[1]["label"] == "9999"


def test_a_phone_far_behind_skips_the_oldest_calls():
    hub = LiveHub()
    listener = hub.join("a", None)
    for i in range(live.MAX_WAITING + 3):
        hub.add_call({"id": i, "talkgroup": "1"}, tone(0.2))
    assert [c["id"] for c in listener.waiting] == list(range(3, live.MAX_WAITING + 3))


def test_stream_plays_the_call_then_silence_and_reports_it():
    hub = LiveHub()
    listener = hub.join("a", None)
    hub.add_call({"id": 7, "talkgroup": "1203", "talkgroup_label": "Fire Dispatch", "call_time": 5}, tone(1.0))

    # The first LEAD_S of audio goes out at once, so the phone has something to buffer.
    chunks = first_chunks(hub, listener, 6)
    samples = decode(b"".join(chunks))

    assert 2.4 < len(samples) / live.RATE < 3.1
    level = lambda a, b: float(np.sqrt(np.mean(samples[int(a * live.RATE):int(b * live.RATE)] ** 2)))
    assert level(0.0, 0.4) < 0.01      # gap before the call
    assert level(0.8, 1.4) > 0.05      # the call
    assert level(1.9, 2.3) < 0.01      # silence after it
    # The stream closed, so the phone left.
    assert not hub.now("a")["connected"]


def test_now_shows_what_is_playing_and_the_same_phone_reconnecting_replaces_itself():
    hub = LiveHub()
    first = hub.join("a", None)
    hub.add_call({"id": 7, "talkgroup": "1203", "talkgroup_label": "Fire Dispatch", "call_time": 5}, tone(0.5))
    hub._next(first)
    now = hub.now("a")
    assert now["playing"]["label"] == "Fire Dispatch" and now["playing"]["seconds"] == 0.5
    assert "samples" not in now["playing"]

    second = hub.join("a", {"1203"})
    assert first.closed and not second.closed
    assert hub.now("a")["playing"] is None


def test_too_many_phones_pushes_out_the_oldest():
    hub = LiveHub()
    listeners = [hub.join(str(i), None) for i in range(live.MAX_LISTENERS + 1)]
    assert listeners[0].closed
    assert not any(l.closed for l in listeners[1:])


def test_upload_reaches_a_listening_phone_and_the_talkgroup_list(make_client):
    client, _ = make_client(api_key="secret")
    with client:
        listener = client.app.state.live.join("phone", {"1203"})
        # A call from just now: the talkgroup list only covers the last week, and CALL's date ages out.
        call = {**CALL, "dateTime": str(int(time.time()))}
        assert post(client, sdrtrunk_body(call, make_mp3(2.0))).status_code == 200
        talkgroups = client.get("/listen/talkgroups").json()
        page = client.get("/listen")

    assert len(listener.waiting) == 1
    call = listener.waiting[0]
    assert call["label"] == "Fire Dispatch" and 1.8 < call["seconds"] < 2.3
    assert talkgroups[0]["talkgroup"] == "1203" and talkgroups[0]["label"] == "Fire Dispatch"
    assert page.status_code == 200 and "Listen live" in page.text


def test_public_page_needs_the_secret_key_and_has_nothing_else(make_client, tmp_path):
    client, _ = make_client()
    with client:
        link = client.get("/api/listen/link").json()
        public = TestClient(live.create_public_app(client.app, tmp_path / "data"))
        key = link["key"]
        assert len(key) >= 30
        assert public.get("/listen").status_code == 404
        assert public.get("/listen?key=wrong").status_code == 404
        assert public.get(f"/listen?key={key}").status_code == 200
        assert public.get(f"/listen/talkgroups?key={key}").status_code == 200
        for path in ("/", "/map", "/api/calls", "/api/listen/link", "/docs", "/openapi.json"):
            assert public.get(f"{path}?key={key}").status_code == 404, path

        # A new link stops the old one.
        new = client.post("/api/listen/new-link").json()["key"]
        assert new != key
        assert public.get(f"/listen?key={key}").status_code == 404
        assert public.get(f"/listen?key={new}").status_code == 200
