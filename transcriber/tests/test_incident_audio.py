"""Play all: an incident's calls joined into one MP3, with each call's start time and deleted audio skipped."""

import io

import av
import numpy as np

from sdrtrunk_transcriber import incident_audio, share
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from test_service import CALL, make_client, make_mp3, post, sdrtrunk_body  # noqa: F401 (fixture)


def setup(tmp_path):
    config = Config()
    config.server.data_dir = str(tmp_path / "data")
    (config.data_path / "calls").mkdir(parents=True)
    return config, Database(config.data_path / "calls.db")


def add_call(config, db, call_time, seconds):
    relative = f"calls/{call_time}.mp3"
    (config.data_path / relative).write_bytes(make_mp3(seconds))
    return db.insert_call({"call_time": call_time, "received_at": call_time, "talkgroup": "1", "audio_path": relative})


def incident_with(db, call_ids):
    with db._lock:
        cur = db._conn.execute("INSERT INTO incidents (first_time, last_time, lat, lon) VALUES (0, 0, 40.6, -75.3)")
        for call_id in call_ids:
            db._conn.execute("INSERT INTO call_places (call_id, incident_id, kind, heard, label, lat, lon, precision) "
                             "VALUES (?, ?, 'address', 'x', 'x', 40.6, -75.3, 'street')", (call_id, cur.lastrowid))
        db._conn.commit()
        return cur.lastrowid


def seconds_of(path):
    with av.open(str(path)) as container:
        return sum(f.samples for f in container.decode(audio=0)) / container.streams.audio[0].rate


def test_calls_are_joined_in_order_with_deleted_audio_skipped(tmp_path):
    config, db = setup(tmp_path)
    late = add_call(config, db, 300, 2.0)
    early = add_call(config, db, 100, 1.0)
    gone = add_call(config, db, 200, 1.5)
    db.mark_audio_deleted([gone], 999)
    incident = incident_with(db, [late, early, gone, early])  # one call naming two places counts once

    info = incident_audio.build(db, config.data_path, incident)
    assert [c["id"] for c in info["calls"]] == [early, late]
    assert info["missing"] == [gone]
    assert info["calls"][0]["start"] == 0
    assert abs(info["calls"][1]["start"] - (1.0 + incident_audio.GAP_S)) < 0.1
    assert abs(info["seconds"] - 4.0) < 0.1
    assert abs(seconds_of(info["file"]) - info["seconds"]) < 0.2

    # Made once, then reused until the incident's calls change.
    again = incident_audio.build(db, config.data_path, incident)
    assert again["file"] == info["file"] and again["calls"] == info["calls"]
    newer = add_call(config, db, 400, 1.0)
    incident_with(db, [])
    with db._lock:
        db._conn.execute("INSERT INTO call_places (call_id, incident_id, kind, heard, label, lat, lon, precision) "
                         "VALUES (?, ?, 'address', 'x', 'x', 40.6, -75.3, 'street')", (newer, incident))
        db._conn.commit()
    changed = incident_audio.build(db, config.data_path, incident)
    assert [c["id"] for c in changed["calls"]] == [early, late, newer]
    assert changed["file"] != info["file"] and not info["file"].exists()


def test_no_audio_left_and_unknown_incident(tmp_path):
    config, db = setup(tmp_path)
    gone = add_call(config, db, 100, 1.0)
    (config.data_path / f"calls/100.mp3").unlink()
    incident = incident_with(db, [gone])
    info = incident_audio.build(db, config.data_path, incident)
    assert info["file"] is None and info["missing"] == [gone]
    assert incident_audio.build(db, config.data_path, 12345) is None


def test_play_routes_and_friends_map(make_client):
    client, _ = make_client()
    with client:
        post(client, sdrtrunk_body(CALL, make_mp3(1.0)))
        post(client, sdrtrunk_body(CALL, make_mp3(1.0)))
        assert client.app.state.worker.wait_idle()
        incident = incident_with(client.app.state.db, [1, 2])
        info = client.get(f"/api/incidents/{incident}/play").json()
        assert [c["id"] for c in info["calls"]] == [1, 2] and info["url"].startswith(f"api/incidents/{incident}/audio.mp3")
        mp3 = client.get("/" + info["url"])
        assert mp3.status_code == 200 and mp3.headers["content-type"] == "audio/mpeg"
        assert "content-encoding" not in mp3.headers  # Safari seeks with range requests
        with av.open(io.BytesIO(mp3.content), format="mp3") as container:
            assert sum(f.samples for f in container.decode(audio=0)) > 0
        assert client.get("/api/incidents/999/play").status_code == 404
        assert client.get("/api/incidents/999/audio.mp3").status_code == 404
    assert share.GUEST_API.match(f"/api/incidents/{incident}/play")
    assert share.GUEST_API.match(f"/api/incidents/{incident}/audio.mp3")
    assert not share.GUEST_API.match(f"/api/incidents/{incident}/merge")
