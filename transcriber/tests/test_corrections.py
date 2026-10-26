"""Learned corrections from the Edit button, and the junk filter for made-up text."""

from fastapi.testclient import TestClient

from sdrtrunk_transcriber.backends import Backend, Transcript
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.corrections import CorrectionBook, learn_pairs
from sdrtrunk_transcriber.junk import remove_junk
from sdrtrunk_transcriber.server import create_app
from sdrtrunk_transcriber.worker import load_vocabulary

from test_service import CALL, make_mp3, post, sdrtrunk_body


# ---- learning from an edit

def test_replaced_words_are_learned():
    assert learn_pairs("Engine 2311 responding to never mind street", "Engine 2311 responding to Nevin Street") \
        == [("never mind", "Nevin")]
    assert learn_pairs("Medic 20 81 on location", "Medic 2081 on location") == [("20 81", "2081")]


def test_one_off_edits_are_not_learned():
    # Added or removed words, capitals only, lone numbers, everyday words, long rewrites
    assert learn_pairs("Engine 12 responding", "Engine 12 is responding") == []
    assert learn_pairs("engine 12 responding", "Engine 12 responding") == []
    assert learn_pairs("Engine 12 responding", "Engine 14 responding") == []
    assert learn_pairs("the car is on the left", "a car is on the left") == []
    assert learn_pairs("one two three four five six", "seven eight nine ten eleven twelve") == []


def test_learned_corrections_apply_to_later_calls(tmp_path):
    path = tmp_path / "corrections.csv"
    book = CorrectionBook(path)
    learned = book.learn("Units respond to never mind street", "Units respond to Nevin Street")
    assert [(r["heard"], r["correct"], r["count"]) for r in learned] == [("never mind", "Nevin", "1")]
    assert path.exists()

    fresh = CorrectionBook(path)
    assert fresh.normalize("Medic 20, Never Mind Street and Ninth") == "Medic 20, Nevin Street and Ninth"
    # Whole words only
    assert fresh.normalize("whenever mindful") == "whenever mindful"
    assert fresh.prompt_terms() == ["Nevin"]

    book.learn("Engine on never mind street", "Engine on Nevin Street")
    assert CorrectionBook(path).items()[0].count == 2

    assert book.forget("never mind", "Nevin")
    assert CorrectionBook(path).normalize("never mind") == "never mind"


def test_hand_made_rows_and_talkgroup_scope(tmp_path):
    path = tmp_path / "corrections.csv"
    path.write_text("heard,correct,talkgroup,hint,count,source\n"
                    "palmer town ship,Palmer Township,,yes,,\n"
                    "station 20,Station 20,Easton,no,,\n", encoding="utf-8")
    book = CorrectionBook(path)
    assert book.normalize("Palmer town ship fire") == "Palmer Township fire"
    assert book.normalize("go to station 20", "Easton Fire") == "go to Station 20"
    assert book.normalize("go to station 20", "Bethlehem Fire") == "go to station 20"
    assert book.prompt_terms() == ["Palmer Township"]


def test_sample_vocabulary_is_ignored(tmp_path):
    path = tmp_path / "vocabulary.txt"
    path.write_text("# comment\nDispatch\nEngine 12\nMedic 4\nBattalion 1\n10-4\nMain Street\n", encoding="utf-8")
    assert load_vocabulary(path) == []
    path.write_text("Dispatch\nMedic 2081\n", encoding="utf-8")
    assert load_vocabulary(path) == ["Dispatch", "Medic 2081"]


# ---- junk filter, with real transcripts from the call log

def test_low_confidence_stock_phrase_is_dropped():
    assert remove_junk("Thank you.", -0.82, []) == ("", ["stock phrase"])
    assert remove_junk("Thank you very much.", -0.9, [])[0] == ""
    # Confident, or part of a real message: kept
    assert remove_junk("Thank you.", -0.3, [])[0] == "Thank you."
    assert remove_junk("10-4, thank you.", -0.9, [])[0] == "10-4, thank you."


def test_loops_are_collapsed():
    loop = "10-27, 10-4, 10-6, " + ", ".join(["10-4"] * 22) + ","
    assert remove_junk(loop, -0.5, []) == ("10-27, 10-4, 10-6, 10-4.", ["loop"])
    assert remove_junk("11-8, 10-4, Main Street, 11-8, 10-4, Main Street, 11-8, 10-4, Main Street,", -0.5, [])[0] \
        == "11-8, 10-4, Main Street."
    assert remove_junk("20-Ladder 1, " + ", ".join(["10-7"] * 20) + ", 10", -0.5, [])[0] == "20-Ladder 1, 10-7."
    # Said twice is normal radio talk
    assert remove_junk("10-4, 10-4.", -0.5, [])[0] == "10-4, 10-4."


def test_counting_runs_are_dropped():
    text = "Dispatch, Engine 12, 1, 10, 11, 11, 12, 13, 14, 14, 14, 15, 16, 16, 17, 18, 19, 20."
    cleaned, removed = remove_junk(text, -0.9, [])
    assert cleaned == "Dispatch, Engine 12."
    assert "counting" in removed


def test_hint_list_echo_is_dropped():
    prompt = ["Route 22", "Nazareth", "10-4", "10-23", "Medic 2081", "Engine 2311"]
    cleaned, removed = remove_junk("Medic 2081, Engine 2311, 10-4, 10-23, Nazareth.", -0.6, prompt)
    assert cleaned == "Medic 2081, Engine 2311, 10-4, 10-23, Nazareth."  # not in hint-list order
    cleaned, removed = remove_junk("Okay, Nazareth, 10-4, 10-23, Medic 2081, on scene.", -0.6, prompt)
    assert cleaned == "Okay, on scene." and removed == ["hint echo"]


# ---- the Edit button end to end

class FixedBackend(Backend):
    name = "fake"

    def __init__(self, text):
        self.text, self.prompts = text, []

    def transcribe(self, samples, prompt):
        self.prompts.append(prompt)
        return Transcript(self.text, -0.3)


def test_edit_saves_learns_and_fixes_later_calls(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    backend = FixedBackend("Engine 2311 to never mind street")
    with TestClient(create_app(config, backend_factory=lambda _c: backend)) as client:
        post(client, sdrtrunk_body(CALL, make_mp3(3.0)))
        assert client.app.state.worker.wait_idle()
        call = client.get("/api/calls").json()[0]
        assert call["transcript"] == "Engine 2311 to never mind street"

        reply = client.post(f"/api/calls/{call['id']}/transcript", json={"text": "Engine 2311 to Nevin Street"})
        assert reply.status_code == 200
        assert [(r["heard"], r["correct"]) for r in reply.json()["learned"]] == [("never mind", "Nevin")]
        saved = client.get("/api/calls").json()[0]
        assert saved["transcript"] == "Engine 2311 to Nevin Street"
        assert saved["corrected_at"]
        assert client.get("/api/calls?q=Nevin").json()[0]["id"] == call["id"]

        # The next call with the same mistake comes out fixed, and Whisper is given the spelling
        post(client, sdrtrunk_body(CALL, make_mp3(3.0)))
        assert client.app.state.worker.wait_idle()
        # Only the misheard words are learned; "street" was heard right and keeps Whisper's capitals
        assert client.get("/api/calls").json()[0]["transcript"] == "Engine 2311 to Nevin street"
        assert backend.prompts[-1].endswith("Nevin")

        assert client.get("/api/corrections").json()[0]["heard"] == "never mind"
        assert client.post("/api/corrections/forget", json={"heard": "never mind", "correct": "Nevin"}).status_code == 200
        assert client.get("/api/corrections").json() == []
        assert client.post("/api/calls/999/transcript", json={"text": "x"}).status_code == 404
        assert client.post(f"/api/calls/{call['id']}/transcript", json={"nope": 1}).status_code == 400
    assert (tmp_path / "corrections.csv").read_text(encoding="utf-8-sig").startswith("heard,correct")


def test_foreign_script_is_removed():
    # Real transcripts from the call log
    assert remove_junk("Engine 12,なしの11, 10-24.", -1.9, [])[0] == "Engine 12, 11, 10-24."
    assert remove_junk("We have up toとう 18-E2.", -2.0, [])[0] == "We have up to 18-E2."
    assert remove_junk("워널브롿, 인보 heights, 20-7, MS4", -3.6, []) == ("", ["foreign script"])
    # Accented Latin letters, curly quotes and dashes are fine
    assert remove_junk("St. Luke’s café — 10-4.", -0.3, []) == ("St. Luke’s café — 10-4.", [])
    # Off when the language isn't English
    assert remove_junk("Engine 12,なしの11.", -1.9, [], english_only=False)[0] == "Engine 12,なしの11."


def test_whisper_is_kept_from_writing_other_scripts():
    from sdrtrunk_transcriber.backends import non_english_tokens

    pieces = [" the", " Engine", " 10", "-4", " café", "’", " Из", "国", "�", " ف", "<|endoftext|>"]

    class Tokenizer:
        def token_to_id(self, token):
            return pieces.index(token)

        def decode(self, ids):
            return "".join(pieces[i] for i in ids)

    assert non_english_tokens(Tokenizer()) == [-1, 6, 7, 8, 9]

    class Broken:
        def token_to_id(self, token):
            raise RuntimeError("no tokenizer")

    assert non_english_tokens(Broken()) is None
