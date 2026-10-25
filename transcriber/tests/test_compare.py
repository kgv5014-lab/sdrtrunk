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
