"""Self-check: spotting garbled transcripts and keeping the better of two Whisper tries."""

import math
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from sdrtrunk_transcriber import audio
from sdrtrunk_transcriber.backends import Backend, Transcript
from sdrtrunk_transcriber.codes import CodeBook
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.db import Database
from sdrtrunk_transcriber.places import PlaceBook
from sdrtrunk_transcriber.selfcheck import FIXED, OK, UNSURE, SelfCheck, better, loop_words
from sdrtrunk_transcriber.worker import TranscriptionWorker

ROOT = Path(__file__).resolve().parents[1]
FIRE = ("Fire Dispatch", "Fire")


@pytest.fixture
def placebook(tmp_path):
    path = tmp_path / "places.csv"
    shutil.copy(ROOT / "places.example.csv", path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write("Northampton Street,street,Easton,,yes,80,,manual\n"
                "Nevin Avenue,street,Palmer Township,,,30,,manual\n"
                "Oak Lane,street,Easton,,,10,,manual\n")
    return PlaceBook(path)


@pytest.fixture
def codebook(tmp_path):
    path = tmp_path / "codes.csv"
    path.write_text("type,code,meaning,applies_to,prompt,source\n"
                    "station,20,Easton,fire,,\n"
                    "station,45,Palmer,fire,,\n"
                    "unit-suffix,11-19,Engine,fire,,\n"
                    "unit-suffix,81-89,Ambulance,ems,,\n", encoding="utf-8")
    return CodeBook(path)


@pytest.fixture
def checker(placebook, codebook):
    return SelfCheck(Config(), placebook, codebook)


def test_unknown_streets(placebook):
    assert placebook.unknown_streets("Engine 2011 to Northampton Street and Nevin Ave.") == []
    assert placebook.unknown_streets("crash at Northbampton Street near Oak Lane") == ["Northbampton Street"]
    # Everyday words that end in a street word are not street names.
    assert placebook.unknown_streets("we'll drive over, on the way, same road, Main Road") == []
    assert placebook.unknown_streets("I'll Drive") == ["I'll Drive"]
    assert PlaceBook(None).unknown_streets("Northbampton Street") == []


def test_unknown_units(codebook):
    assert codebook.unknown_units("Engine 2011 and Medic 4581 responding, Station 45", *FIRE) == []
    assert codebook.unknown_units("Engine 9711 responding", *FIRE) == ["Engine 9711"]
    # A station that exists but a unit number that is no unit type.
    assert codebook.unknown_units("Ladder 2077 on location", *FIRE) == ["Ladder 2077"]
    assert codebook.unknown_units("Station 77", *FIRE) == ["Station 77"]
    # Police talkgroups number cars differently; short numbers are left alone.
    assert codebook.unknown_units("Engine 9711", "Easton PD", "Police") == []
    assert codebook.unknown_units("Engine 9 and Medic 4", *FIRE) == []


def test_loop_words():
    assert loop_words("Engine 2011 on location") is None
    assert loop_words("go go go go") == "go"
    assert loop_words("copy that copy that copy that, thanks") == "copy that"
    assert loop_words("10-4, 10-4") is None


def test_problems(checker):
    assert checker.problems("Engine 2011 on location, Northampton Street", -0.3, 4.0, *FIRE) == []
    assert checker.problems("", -2.0, 4.0, *FIRE) == []
    assert checker.problems("Engine 9711 at Northbampton Street", -1.2, 4.0, *FIRE) == [
        "Whisper unsure", 'unknown street "Northbampton Street"', 'unknown unit "Engine 9711"']
    many = " ".join(["word"] * 12)
    assert "too many words for the call's length" in checker.problems("a b c d e f g h i j k l", -0.2, 1.5, *FIRE)
    assert "too few words for the call's length" in checker.problems("okay", -0.2, 20.0, *FIRE)
    assert '"word" repeats' in checker.problems(many, -0.2, 6.0, *FIRE)


def test_better():
    assert better(("Northbampton Street", -1.0, ["a", "b"]), ("Northampton Street", -0.9, []))
    assert not better(("Northampton Street", -0.3, []), ("Northampton Street", -0.29, []))
    assert better(("x y", -1.2, ["unsure"]), ("x z", -0.9, ["unsure"]))
    # A blank second try never replaces words.
    assert not better(("Engine 9711", -1.2, ["a", "b"]), ("", None, []))


class FakeOllama:
    def __init__(self, garbled):
        self.garbled = garbled
        self.asked = []

    def ask(self, prompt, schema, system=None):
        self.asked.append(prompt)
        if self.garbled is None:
            raise ConnectionError("refused")
        return {"garbled": self.garbled, "reason": "nonsense words"}


def test_ollama_second_opinion(checker):
    checker.ollama = FakeOllama(True)
    text = "Engine 2011 the wobble gronk went on the pass at the place"
    assert checker.ask_ollama(text, -0.6, "Fire Dispatch") == "AI: nonsense words"
    # Whisper was sure enough, or too few words: not asked.
    assert checker.ask_ollama(text, -0.2, "Fire Dispatch") is None
    assert checker.ask_ollama("Engine 2011 copy", -0.6, "Fire Dispatch") is None
    assert len(checker.ollama.asked) == 1

    checker.ollama = FakeOllama(None)
    assert checker.ask_ollama(text, -0.6, "Fire Dispatch") is None
    # Left alone for a while after it failed.
    assert checker.ask_ollama(text, -0.6, "Fire Dispatch") is None
    assert len(checker.ollama.asked) == 1


def test_ollama_only_with_summaries_on(placebook, codebook):
    assert SelfCheck(Config(), placebook, codebook).ollama is None
    config = Config()
    config.summaries.enabled = True
    assert SelfCheck(config, placebook, codebook).ollama is not None


class TwoTries(Backend):
    """First try garbled, the careful try clean (or whatever is given)."""
    name = "fake"
    careful_retry = True

    def __init__(self, first, careful):
        self.first, self.careful = first, careful
        self.calls = []

    def transcribe(self, samples, prompt, careful=False):
        self.calls.append(careful)
        return self.careful if careful else self.first


def run_call(tmp_path, placebook, codebook, backend, **self_check):
    config = Config()
    config.base_dir = tmp_path
    for key, value in self_check.items():
        setattr(config.self_check, key, value)
    rate = audio.WHISPER_RATE
    seconds = 4.0
    t = np.arange(int(seconds * rate)) / rate
    # A wobbling tone, so the tone remover leaves it as "speech".
    samples = (0.3 * np.sin(2 * math.pi * (300 + 200 * np.sin(2 * math.pi * 3 * t)) * t)).astype(np.float32)
    (tmp_path / "data" / "calls").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "calls" / "1.wav").write_bytes(audio.to_wav_bytes(samples))
    db = Database(tmp_path / "data" / "calls.db")
    call_id = db.insert_call({"call_time": int(time.time()), "received_at": int(time.time()), "talkgroup": "10000",
                              "talkgroup_label": "Fire Dispatch", "talkgroup_group": "Fire",
                              "audio_path": "calls/1.wav"})
    worker = TranscriptionWorker(config, db, lambda _c: backend, codebook, placebook)
    worker.start()
    assert worker.wait_idle()
    worker.stop()
    return db.get_call(call_id)


# The street fixer already corrects near misses like "Northbampton", so this one is far off.
GARBLED = Transcript("Engine 9711 to Zebulon Street", -1.1)
CLEAN = Transcript("Engine 2011 to Northampton Street", -0.4)


def test_careful_retry_replaces_a_garbled_transcript(tmp_path, placebook, codebook):
    backend = TwoTries(GARBLED, CLEAN)
    call = run_call(tmp_path, placebook, codebook, backend)
    assert backend.calls == [False, True]
    assert call["transcript"] == "Engine 2011 to Northampton Street"
    assert call["transcript_raw"] == "Engine 2011 to Northampton Street"
    assert call["confidence"] == -0.4
    assert call["check_status"] == FIXED
    assert call["check_notes"] is None
    assert call["first_transcript"] == "Engine 9711 to Zebulon Street"


def test_worse_retry_is_thrown_away(tmp_path, placebook, codebook):
    backend = TwoTries(GARBLED, Transcript("", None))
    call = run_call(tmp_path, placebook, codebook, backend)
    assert call["transcript"] == "Engine 9711 to Zebulon Street"
    assert call["check_status"] == UNSURE
    assert "Zebulon" in call["check_notes"] and "Engine 9711" in call["check_notes"]
    assert call["first_transcript"] is None


def test_clean_call_runs_once(tmp_path, placebook, codebook):
    backend = TwoTries(CLEAN, GARBLED)
    call = run_call(tmp_path, placebook, codebook, backend)
    assert backend.calls == [False]
    assert call["check_status"] == OK


def test_retry_off_only_marks(tmp_path, placebook, codebook):
    backend = TwoTries(GARBLED, CLEAN)
    call = run_call(tmp_path, placebook, codebook, backend, retry=False)
    assert backend.calls == [False]
    assert call["check_status"] == UNSURE


def test_self_check_off(tmp_path, placebook, codebook):
    backend = TwoTries(GARBLED, CLEAN)
    call = run_call(tmp_path, placebook, codebook, backend, enabled=False)
    assert backend.calls == [False]
    assert call["check_status"] is None
