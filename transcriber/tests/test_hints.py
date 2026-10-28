"""Whisper hints learned from the call log."""

from sdrtrunk_transcriber.codes import CodeBook
from sdrtrunk_transcriber.config import Config, HintsConfig
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.hints import LearnedHints, fit, place_names, unit_names
from sdrtrunk_transcriber.worker import TranscriptionWorker, prompt_terms

NOW = 1_800_000_000


def add_call(db, when, talkgroup="11002", transcript="", label=None, incident_id=None):
    call_id = db.insert_call({"call_time": when, "received_at": when, "talkgroup": talkgroup,
                              "talkgroup_label": f"TG {talkgroup}", "audio_path": "x.mp3"})
    db.update_call(call_id, status="done", transcript=transcript)
    if label:
        db.add_call_place({"call_id": call_id, "incident_id": incident_id, "kind": "address", "label": label})
    return call_id


def incident(db, when, hidden=False):
    incident_id = db.create_incident({"first_time": when, "last_time": when, "lat": 40.69, "lon": -75.21})
    if hidden:
        db.update_incident(incident_id, hidden=1)
    return incident_id


def test_place_and_unit_names():
    assert place_names("1234 Northampton Street, Easton") == ["Northampton Street"]
    assert place_names("1200 block of Main Street") == ["Main Street"]
    assert place_names("Main Street & 3rd Street, Wilson") == ["Main Street", "3rd Street"]
    assert place_names("Wawa, Palmer Township") == ["Wawa"]
    assert place_names(None) == []
    assert unit_names("Medic 2081 and Engine 4-1 on location, car 5, QRS 2091") == \
        ["Engine 41", "Medic 2081", "QRS 2091"]
    assert unit_names("EMS 2081 transporting") == ["Medic 2081"]


def test_fit_keeps_the_first_terms_that_fit():
    assert fit(["Main Street", "main street", "Oak Lane", "Northampton Street"], 24) == ["Main Street", "Oak Lane"]


def test_live_incident_streets_and_units_come_last(tmp_path):
    db = Database(tmp_path / "calls.db")
    old = incident(db, NOW - 3 * 3600)
    add_call(db, NOW - 3 * 3600, transcript="Engine 2311 respond", label="12 Old Road, Easton", incident_id=old)
    hidden = incident(db, NOW - 600, hidden=True)
    add_call(db, NOW - 600, transcript="Medic 9981", label="5 Hidden Lane, Easton", incident_id=hidden)
    live = incident(db, NOW - 900)
    add_call(db, NOW - 900, transcript="Medic 2081 respond to Northampton Street",
             label="1234 Northampton Street, Easton", incident_id=live)
    # After the call being transcribed: not known yet.
    add_call(db, NOW + 60, transcript="Ladder 2031", label="9 Future Avenue, Easton", incident_id=live)

    hints = LearnedHints(tmp_path / "calls.db", HintsConfig())
    terms = hints.terms("11002", NOW)
    assert terms[-2:] == ["Medic 2081", "Northampton Street"]
    for absent in ("Old Road", "Engine 2311", "Hidden Lane", "Medic 9981", "Future Avenue", "Ladder 2031"):
        assert absent not in terms
    assert "Northampton Street" not in hints.terms("11002", NOW, live=False)


def test_most_heard_per_talkgroup_then_everywhere(tmp_path):
    db = Database(tmp_path / "calls.db")
    for n in range(4):
        add_call(db, NOW - 86400 * (n + 2), talkgroup="11002", label="1 Freemansburg Avenue, Bethlehem")
        add_call(db, NOW - 86400 * (n + 2), talkgroup="10000", transcript="Engine 2011 responding")
    for n in range(2):
        # Heard too few times to count.
        add_call(db, NOW - 86400 * (n + 2), talkgroup="11002", label="1 Rare Road, Bethlehem")
    for n in range(5):
        # Older than history_days.
        add_call(db, NOW - 86400 * 40, talkgroup="11002", label="1 Gone Street, Bethlehem")

    hints = LearnedHints(tmp_path / "calls.db", HintsConfig())
    terms = hints.terms("11002", NOW)
    assert terms == ["Engine 2011", "Freemansburg Avenue"]
    # Another talkgroup's own names are weighed more than the county-wide ones.
    assert hints.terms("10000", NOW) == ["Freemansburg Avenue", "Engine 2011"]
    assert hints.terms("99999", NOW) == ["Engine 2011", "Freemansburg Avenue"]


def test_history_is_counted_again_after_refresh(tmp_path):
    db = Database(tmp_path / "calls.db")
    hints = LearnedHints(tmp_path / "calls.db", HintsConfig())
    assert hints.terms("11002", NOW) == []
    for _ in range(3):
        add_call(db, NOW + 10, label="1 Nevin Street, Wilson")
    assert hints.terms("11002", NOW + 60) == []
    assert hints.terms("11002", NOW + 31 * 60) == ["Nevin Street"]


def test_room_and_off_switch(tmp_path):
    db = Database(tmp_path / "calls.db")
    for name in ("Alpha Street", "Bravo Street", "Charlie Street", "Delta Street"):
        for _ in range(3):
            add_call(db, NOW - 3600 * 5, label=f"1 {name}")
    hints = LearnedHints(tmp_path / "calls.db", HintsConfig(prompt_chars=30))
    assert len(", ".join(hints.terms("11002", NOW))) <= 30
    assert LearnedHints(tmp_path / "calls.db", HintsConfig(enabled=False)).terms("11002", NOW) == []
    assert LearnedHints(tmp_path / "missing.db", HintsConfig()).terms("11002", NOW) == []


def test_prompt_keeps_vocabulary_and_drops_duplicates(tmp_path):
    assert prompt_terms(["Main Street", "Oak Lane"], ["main street"], ["Tac 1"]) == ["Oak Lane", "main street", "Tac 1"]

    db = Database(tmp_path / "calls.db")
    live = incident(db, NOW - 300)
    add_call(db, NOW - 300, transcript="Medic 2081 respond", label="1234 Northampton Street, Easton",
             incident_id=live)
    config = Config()
    config.base_dir = tmp_path
    (tmp_path / "vocabulary.txt").write_text("Tac 1\nNorco Ops 1\n", encoding="utf-8")
    worker = TranscriptionWorker(config, db, None, CodeBook(None), hints=LearnedHints(tmp_path / "calls.db", config.hints))
    terms = worker.prompt_terms("TG 11002", None, call={"talkgroup": "11002", "call_time": NOW})
    assert terms[-4:] == ["Medic 2081", "Northampton Street", "Tac 1", "Norco Ops 1"]
    assert worker.prompt_terms("TG 11002", None)[-2:] == ["Tac 1", "Norco Ops 1"]
