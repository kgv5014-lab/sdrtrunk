"""Old call audio is deleted by age and by total size; incident and alert audio is kept."""

import time

from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.recordings import RecordingCleanup

DAY = 86400
NOW = 1_800_000_000


def setup(tmp_path, max_age_days=7.0, max_total_gb=0.0, map_enabled=True):
    config = Config()
    config.server.data_dir = str(tmp_path / "data")
    config.recordings.max_age_days = max_age_days
    config.recordings.max_total_gb = max_total_gb
    config.map.enabled = map_enabled
    (config.data_path / "calls").mkdir(parents=True)
    db = Database(config.data_path / "calls.db")
    return config, db, RecordingCleanup(config, db)


def add_call(config, db, age_days, size=1000, status="done", map_status="none"):
    call_time = int(NOW - age_days * DAY)
    relative = f"calls/2026/10/{int(age_days):02d}/{call_time}.mp3"
    path = config.data_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    call_id = db.insert_call({"call_time": call_time, "received_at": call_time, "talkgroup": "1",
                              "audio_path": relative})
    db.update_call(call_id, status=status, map_status=map_status)
    return call_id, path


def put_on_incident(db, call_id):
    with db._lock:
        cur = db._conn.execute("INSERT INTO incidents (first_time, last_time, lat, lon) VALUES (0, 0, 40.6, -75.3)")
        db._conn.execute("INSERT INTO call_places (call_id, incident_id, kind, heard, label, lat, lon, precision) "
                         "VALUES (?, ?, 'address', 'x', 'x', 40.6, -75.3, 'street')", (call_id, cur.lastrowid))
        db._conn.commit()


def test_deletes_audio_older_than_max_age(tmp_path):
    config, db, cleanup = setup(tmp_path)
    old, old_path = add_call(config, db, 8)
    new, new_path = add_call(config, db, 2)

    assert cleanup.run_once(NOW) == (1, 1000)
    assert not old_path.exists() and new_path.exists()
    assert not old_path.parent.exists()  # the emptied day folder goes too
    assert db.get_call(old)["audio_deleted_at"] == NOW
    assert db.get_call(old)["status"] == "done"  # the call itself stays logged
    assert db.get_call(new)["audio_deleted_at"] is None
    assert cleanup.run_once(NOW) == (0, 0)


def test_keeps_incident_alert_and_unfinished_calls(tmp_path):
    config, db, cleanup = setup(tmp_path)
    on_incident, p1 = add_call(config, db, 10)
    put_on_incident(db, on_incident)
    alerted, p2 = add_call(config, db, 10)
    with db._lock:
        db._conn.execute("INSERT OR IGNORE INTO alerts (id, time, kind, call_id) VALUES ('a1', 0, 'incident', ?)",
                         (alerted,))
        db._conn.commit()
    queued, p3 = add_call(config, db, 10, status="queued")
    unplaced, p4 = add_call(config, db, 10, map_status=None)

    assert cleanup.run_once(NOW) == (0, 0)
    assert all(p.exists() for p in (p1, p2, p3, p4))


def test_map_off_does_not_wait_for_placing(tmp_path):
    config, db, cleanup = setup(tmp_path, map_enabled=False)
    _, path = add_call(config, db, 10, map_status=None)
    assert cleanup.run_once(NOW)[0] == 1
    assert not path.exists()


def test_size_limit_deletes_oldest_first(tmp_path):
    gb = 1024 ** 3
    config, db, cleanup = setup(tmp_path, max_age_days=0, max_total_gb=2500 / gb)
    oldest, p1 = add_call(config, db, 3)
    kept, p2 = add_call(config, db, 4)
    put_on_incident(db, kept)
    middle, p3 = add_call(config, db, 2)
    newest, p4 = add_call(config, db, 1)

    # 4000 bytes over a 2500 byte limit: the two oldest deletable calls go, the incident call stays.
    assert cleanup.run_once(NOW) == (2, 2000)
    assert not p1.exists() and not p3.exists()
    assert p2.exists() and p4.exists()


def test_zero_turns_both_limits_off(tmp_path):
    config, db, cleanup = setup(tmp_path, max_age_days=0, max_total_gb=0)
    assert not cleanup.enabled
