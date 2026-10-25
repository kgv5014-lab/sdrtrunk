"""Two-tone pages found in call recordings (P25 talkgroups, where SDRTrunk's own decoder can't hear them)."""

import io
import json
import time

import av
import numpy as np
import pytest
from fastapi.testclient import TestClient

from sdrtrunk_transcriber import alerts
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.server import create_app
from sdrtrunk_transcriber.tones import ToneList, find_pages

from test_alerts import FakeSession
from test_incidents import Radio
from test_service import CALL, post, sdrtrunk_body

RATE = 16000
RNG = np.random.default_rng(7)


def tone(hz, seconds, rate=RATE, level=0.3):
    return level * np.sin(2 * np.pi * hz * np.arange(int(seconds * rate)) / rate)


def silence(seconds, rate=RATE):
    return np.zeros(int(seconds * rate))


def voice(seconds, rate=RATE):
    """Speech-like sound: a voice with many harmonics whose pitch changes every 100 ms."""
    pieces = []
    for _ in range(int(seconds * 10)):
        pitch = RNG.uniform(90, 220)
        t = np.arange(int(0.1 * rate)) / rate
        pieces.append(sum(0.08 / k ** 0.5 * np.sin(2 * np.pi * k * pitch * t) for k in range(1, 20)))
    return np.concatenate(pieces)


def noisy(samples, level=0.01):
    return samples + level * RNG.standard_normal(len(samples))


def test_quick_call_pair_is_found_with_its_tones():
    audio = noisy(np.concatenate([silence(0.5), tone(349.0, 1.0), silence(0.1), tone(832.5, 3.0), silence(0.3),
                                  voice(4)]))
    [page] = find_pages(audio, RATE)
    assert abs(page.tone_a - 349.0) < 1.5 and abs(page.tone_b - 832.5) < 2.5
    assert 0.9 <= page.tone_a_seconds <= 1.1 and 0.4 <= page.offset_s <= 0.6


def test_long_tone_and_back_to_back_pairs():
    pages = find_pages(np.concatenate([tone(1084.0, 7), silence(1), tone(600.9, 1), tone(1530.0, 3)]), RATE)
    assert [(round(p.tone_a), p.tone_b and round(p.tone_b)) for p in pages] == [(1084, None), (601, 1530)]


@pytest.mark.parametrize("audio", [
    voice(6),
    # CTCSS under the voice is below the paging band
    voice(6) + tone(136.5, 6, level=0.1),
    # One short beep before the dispatcher talks is not a page
    np.concatenate([tone(1000, 0.5), voice(5)]),
    # Two beeps too short to be paging tones
    np.concatenate([tone(800, 0.2), tone(1200, 0.2), voice(3)]),
])
def test_no_page_in_ordinary_calls(audio):
    assert find_pages(noisy(audio), RATE) == []


def test_tone_list_names_pages_within_one_and_a_half_percent(tmp_path):
    path = tmp_path / "paging_tones.csv"
    path.write_text("# comment\ntone_a,tone_b,name\n349,832.5,Station 4\n1084,,County all-call\nbad,line\n",
                    encoding="utf-8")
    tones = ToneList(path)
    assert tones.name(350.2, 829.0) == "Station 4"
    assert tones.name(349, 870) is None
    assert tones.name(1084.4, None) == "County all-call"
    assert tones.name(1084.4, 1500) is None


def mp3(samples, rate=8000) -> bytes:
    """An 8 kHz MP3 like SDRTrunk's uploads."""
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=rate, layout="mono")
        frame = av.AudioFrame.from_ndarray((np.clip(samples, -1, 1) * 32767).astype("<i2").reshape(1, -1),
                                           format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def test_tones_in_an_uploaded_call_are_logged_named_and_alerted(tmp_path, monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(alerts.requests, "Session", lambda: session)
    (tmp_path / "paging_tones.csv").write_text("tone_a,tone_b,name\n349,832.5,Station 4\n", encoding="utf-8")
    config = Config()
    config.base_dir = tmp_path
    config.map.enabled = False
    config.alerts.enabled = True
    config.alerts.stations = ["Station 4"]
    config.tones.talkgroups = ["Fire Dispatch"]
    rate = 8000
    paged = noisy(np.concatenate([silence(0.3, rate), tone(349.0, 1.0, rate), tone(832.5, 3.0, rate),
                                  voice(3, rate)]))
    radio = Radio(["Station 4 Engine, respond 1234 Northampton Street.", "Medic 2 on scene.",
                   "Station 4 Engine, respond."])
    now = int(time.time())
    client = TestClient(create_app(config, backend_factory=lambda _c: radio))
    with client:
        post(client, sdrtrunk_body({**CALL, "dateTime": str(now)}, mp3(paged, rate)))
        # Same tones on another talkgroup are not checked
        post(client, sdrtrunk_body({**CALL, "dateTime": str(now + 1), "talkgroupLabel": "EMS Ops"},
                                   mp3(paged, rate)))
        assert client.app.state.worker.wait_idle()
        client.app.state.alerter.flush()
        pages = client.get("/api/pages").json()
        sent = [json.loads(line) for line in client.get("/sdrtrunk-alerts/json").text.splitlines()]
        calls = client.get("/api/calls").json()

    assert len(pages) == 1
    assert pages[0]["label"] == "Station 4" and pages[0]["channel"] == "Fire Dispatch"
    assert abs(pages[0]["page_time"] - (now + 0.3)) < 0.2
    assert [m["title"] for m in sent] == ["Tone-out: Station 4"]
    # Whisper still got the call, with the tones cut out
    assert all(c["status"] == "done" for c in calls)
