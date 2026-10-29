"""Two-tone pages found in call recordings (P25 talkgroups, where SDRTrunk's own decoder can't hear them)."""

import io
import json
import time
from collections import Counter

import av
import numpy as np
import pytest
from fastapi.testclient import TestClient

from sdrtrunk_transcriber import alerts
from sdrtrunk_transcriber.codes import CodeBook
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


def test_stations_named():
    from sdrtrunk_transcriber.tones import stations_named
    assert stations_named("EMS 27, Medic 2781 respond for a fall") == ({"EMS 27"}, {27})
    assert stations_named("Station 20, Fire 20 Easton, Engine 2011") == ({"Station 20", "Fire 20"}, {20})
    assert stations_named("E-M-S 10 at 13:46, 10-4") == ({"EMS 10"}, {10})


def log_call(db, t, talkgroup, label, seconds, transcript):
    call_id = db.insert_call({"call_time": int(t), "received_at": int(t), "talkgroup": talkgroup,
                              "talkgroup_label": label, "audio_path": "x.mp3"})
    db.update_call(call_id, duration_s=seconds, status="done", transcript=transcript)


def test_unlisted_tones_are_named_after_the_station_dispatch_keeps_naming(tmp_path):
    from sdrtrunk_transcriber.db import Database
    from sdrtrunk_transcriber.tones import ToneFinder
    config = Config()
    config.base_dir = tmp_path
    (tmp_path / "paging_tones.csv").write_bytes(b"tone_a,tone_b,name\r\n1593.8,1781.3,EMS 10\r\n")
    db = Database(tmp_path / "calls.db")
    now = time.time()
    said = {
        # A pair named the same station after most tone-outs (once twice in a row), one heard elsewhere.
        (2468.8, 656.2): ["EMS 27, respond for a fall", "EMS 27 Medic 2781 respond", "Ems 27 for chest pain",
                          "Station 13 respond"],
        # The tone every fire call ends with: a different station each time.
        (406.3, 437.5): ["Fire 54 respond", "Fire 50 respond", "Station 20 respond"],
        # Fire and EMS from the same station.
        (1093.7, 906.3): ["Fire 20 Easton respond", "EMS 20 respond", "Station 20 for an alarm"],
        # Seen only twice.
        (687.5, 937.5): ["Rescue 71 respond", "Rescue 71 respond"],
        # Already on the list, logged before it was.
        (1593.8, 1781.3): ["EMS 10"],
    }
    t = now - 20 * 86400
    for (a, b), texts in said.items():
        for text in texts:
            t += 3600
            for repeat in (0, 30):
                db.insert_page({"page_time": t + repeat, "received_at": int(t), "tone_a": a * (1 + RNG.uniform(-0.004, 0.004)),
                                "tone_b": b, "tone_a_seconds": 1.0, "channel": "Countywide"})
            log_call(db, t - 2, "10000", "Countywide", 20.0, text)
            # Another talkgroup at the same time doesn't count.
            log_call(db, t + 5, "1203", "County Police", 5.0, "Station 99 Station 99")
    finder = ToneFinder(config, db)
    added = finder.learn(now)

    assert sorted((b, name) for _, b, name in added) == [(656.2, "EMS 27"), (906.3, "Station 20")]
    text = (tmp_path / "paging_tones.csv").read_bytes()
    assert b",656.2,EMS 27\r\n" in text and b"# Learned " in text and b"\n" not in text.replace(b"\r\n", b"")
    labels = Counter(p["label"] for p in db.pages(limit=1000))
    assert labels == {"EMS 27": 8, "Station 20": 6, "EMS 10": 2, None: 10}
    # Nothing new to learn the next time.
    assert finder.learn(now) == []
    assert finder.tone_list.name(2468.0, 656.0) == "EMS 27"


def test_tone_outs_with_no_station_heard_do_not_count_against_a_name():
    from sdrtrunk_transcriber.tones import pick_name
    nothing = (set(), set())
    heard = [({"EMS 49"}, {49})] * 8 + [({"Fire 42"}, {42})] * 4 + [({"Fire 45"}, {45})] * 3 + [nothing] * 27
    assert pick_name(heard, 3, 0.5) == ("EMS 49", 8)
    # Two stations named as often: unclear.
    assert pick_name([({"EMS 50"}, {50})] * 6 + [({"EMS 4"}, {4})] * 6 + [nothing] * 5, 3, 0.5) is None
    # Exactly half isn't enough.
    assert pick_name([({"EMS 50"}, {50})] * 3 + [({"EMS 4"}, {4})] * 2 + [({"EMS 3"}, {3})], 3, 0.5) is None


def test_stations_named_keeps_to_the_countys_stations():
    from sdrtrunk_transcriber.tones import stations_named
    stations = {17, 27, 62}
    # "MS-6" is a garbled EMS 62: there is no station 6
    assert stations_named("62, Tac 6, ALS, MS-6, Fire 17", stations) == ({"Fire 17"}, {17, 62})
    assert stations_named("County, 62 committed, BLS", stations) == (set(), {62})
    assert stations_named("4882, EMS, 27, BLS", stations) == ({"EMS 27"}, {27})
    # Without the list any number counts, and a leading number isn't trusted
    assert stations_named("62, Tac 6, MS-6") == ({"EMS 6"}, {6})


def test_learned_names_are_corrected_as_more_tone_outs_come_in(tmp_path):
    from sdrtrunk_transcriber.db import Database
    from sdrtrunk_transcriber.tones import ToneFinder
    config = Config()
    config.base_dir = tmp_path
    (tmp_path / "paging_tones.csv").write_bytes(
        b"tone_a,tone_b,name\r\n"
        b"1093.7,906.3,Station 20 Easton\r\n"
        b"# Learned 2026-10-28: dispatch named EMS 6 after 3 of 5 tone-outs\r\n"
        b"718.7,656.3,EMS 6\r\n"
        b"# Learned 2026-10-28: dispatch named Fire 17 after 4 of 5 tone-outs\r\n"
        b"2625.0,1187.5,Fire 17\r\n")
    (tmp_path / "codes.csv").write_text("type,code,meaning\nstation,17,Bethlehem Twp\nstation,20,Easton\n"
                                        "station,27,Palmer\nstation,62,Cetronia EMS\n", encoding="utf-8")
    db = Database(tmp_path / "calls.db")
    now = time.time()
    said = {
        (718.7, 656.3): ["62, Tac 6, ALS, MS-6", "EMS 62 committed", "62 and 6, BLS", "EMS 6 for a fall",
                         "62, Ops 2, ALS"],
        # The same station said another way isn't a correction
        (2625.0, 1187.5): ["Fire 17, EMS 17", "Station 17", "EMS 17 respond", "Fire 17, EMS 17"],
        # Typed in by hand: never changed
        (1093.7, 906.3): ["EMS 27", "EMS 27", "EMS 27", "EMS 27"],
    }
    t = now - 10 * 86400
    for (a, b), texts in said.items():
        for text in texts:
            t += 3600
            page = db.insert_page({"page_time": t, "received_at": int(t), "tone_a": a, "tone_b": b,
                                   "tone_a_seconds": 1.0, "channel": "Countywide"})
            db.name_pages([page], {718.7: "EMS 6", 2625.0: "Fire 17"}.get(a, "Station 20 Easton"))
            log_call(db, t + 3, "10000", "Countywide", 20.0, text)
    finder = ToneFinder(config, db, codebook=CodeBook(tmp_path / "codes.csv"))

    assert finder.learn(now) == [(718.7, 656.3, "Station 62")]
    text = (tmp_path / "paging_tones.csv").read_bytes().decode()
    assert "718.7,656.3,Station 62\r\n" in text and "(was EMS 6)" in text and "EMS 6\r\n" not in text
    assert "2625.0,1187.5,Fire 17" in text and "Station 20 Easton" in text
    labels = Counter(p["label"] for p in db.pages(limit=1000))
    assert labels == {"Station 62": 5, "Fire 17": 4, "Station 20 Easton": 4}
    # Settled: nothing changes the next time
    assert finder.learn(now) == []
