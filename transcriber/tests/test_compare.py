"""The before/after compare report."""

from fastapi.testclient import TestClient

from sdrtrunk_transcriber.compare import compare, report, word_error_rate
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.server import create_app

from test_corrections import FixedBackend
from test_service import CALL, make_mp3, post, sdrtrunk_body


def test_word_error_rate():
    assert word_error_rate("Engine 2311 to Nevin Street", "engine 2311 to nevin street.") == 0.0
    assert word_error_rate("Engine 2311 to Nevin Street", "Engine 2311 to never mind street") == 0.4
    assert word_error_rate("", "") == 0.0


def test_compare_scores_against_hand_fixes(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    backend = FixedBackend("Engine 2311 to never mind street, thank you, thank you, thank you, thank you")
    with TestClient(create_app(config, backend_factory=lambda _c: backend)) as client:
        for _ in range(3):
            post(client, sdrtrunk_body(CALL, make_mp3(3.0)))
        assert client.app.state.worker.wait_idle()
        first = client.get("/api/calls").json()[-1]
        client.post(f"/api/calls/{first['id']}/transcript", json={"text": "Engine 2311 to Nevin Street"})

    rows, summary = compare(config, backend, 10)
    assert summary["calls"] == 3 and summary["fixed"] == 1
    assert rows[0]["fixed"] and rows[0]["call"]["id"] == first["id"]
    assert rows[0]["before"] == "Engine 2311 to never mind street, thank you, thank you, thank you, thank you"
    assert rows[0]["after"] == "Engine 2311 to Nevin street, thank you."
    assert summary["after_wer"] < summary["before_wer"]
    page = report(rows, summary, "fake")
    assert "words wrong: before" in page and "Nevin" in page


def test_compare_hints_runs_with_and_without_learned_hints(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    backend = FixedBackend("Engine 2311 on location")
    with TestClient(create_app(config, backend_factory=lambda _c: backend)) as client:
        for _ in range(2):
            post(client, sdrtrunk_body(CALL, make_mp3(3.0)))
        assert client.app.state.worker.wait_idle()

    prompts = []
    original = backend.transcribe
    backend.transcribe = lambda samples, prompt: prompts.append(prompt) or original(samples, prompt)
    rows, summary = compare(config, backend, 10, hints=True)
    assert summary["calls"] == 2 and summary["hints"]
    # Engine 2311 was heard only twice before, under min_count, so both sides got the same prompt.
    assert len(prompts) == 4 and prompts[0] == prompts[1]
    assert "without learned hints" in report(rows, summary, "fake")
