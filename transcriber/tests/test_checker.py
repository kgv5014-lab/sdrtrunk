"""The Whisper checker page and the whisper_check report."""

from fastapi.testclient import TestClient

from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.server import create_app
from sdrtrunk_transcriber.whisper_check import check, edits, mistakes, report, suggestions

from test_corrections import FixedBackend
from test_service import CALL, make_mp3, post, sdrtrunk_body


def test_edits_name_the_mistakes():
    n, found = edits("Engine 2311 respond to Nevin Street", "engine 2311 to never mind street")
    assert n == 3
    assert ("wrong", "never mind", "nevin") in found and ("missed", "", "respond") in found


def test_checker_hides_whisper_until_typed_and_scores_it(tmp_path):
    config = Config()
    config.base_dir = tmp_path
    backend = FixedBackend("Engine 2311 to never mind street")
    with TestClient(create_app(config, backend_factory=lambda _c: backend)) as client:
        for _ in range(3):
            post(client, sdrtrunk_body(CALL, make_mp3(4.0)))
        assert client.app.state.worker.wait_idle()
        assert client.post("/api/checker/pick", json={"count": 2}).json() == {"added": 2}
        calls = client.get("/api/checker").json()
        assert len(calls) == 2 and all(c["whisper"] is None and not c["done"] for c in calls)
        saved = client.post(f"/api/checker/{calls[0]['id']}", json={"text": " Engine 2311 to  Nevin Street "}).json()
        assert saved["typed"] == "Engine 2311 to Nevin Street" and saved["whisper"]
        client.post(f"/api/checker/{calls[1]['id']}", json={"skip": True})
        calls = client.get("/api/checker").json()
        assert all(c["done"] for c in calls) and calls[0]["whisper"]
        # Typing in the checker leaves the call log alone, and checker audio is never cleaned up.
        assert client.get("/api/calls").json()[-1]["transcript"] == "Engine 2311 to never mind street"
        assert {c["id"] for c in client.app.state.db.audio_to_delete()}.isdisjoint(c["id"] for c in calls)
        assert client.post("/api/checker/99999", json={"skip": True}).status_code == 404
        assert client.get("/checker").status_code == 200
        # Only the third call is left to pick.
        assert client.post("/api/checker/pick", json={}).json() == {"added": 1}

    rows, models = check(config, [("fake", lambda: backend)])
    assert len(rows) == 1
    logged, fake = models
    assert logged["label"] == "As logged" and fake["score"].wrong == 0.4
    found = mistakes(rows * 2, "fake")
    assert found["wrong"][("never mind", "nevin")] == 2
    corrections, vocab = suggestions(found)
    assert corrections == [("never mind", "nevin", 2)] and ("nevin", 2) in vocab
    page = report(rows, models)
    assert "40.0%" in page and "never mind" in page
