import io

from sdrtrunk_transcriber import fetch_places
from sdrtrunk_transcriber.progress import Progress, bar, format_eta


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_counts_only_the_items_it_started_with():
    clock = Clock()
    progress = Progress(clock)
    progress.start("map", "Placing calls on the map", range(100))
    progress.step("map", 5000)          # a new call, not part of the catch-up
    progress.step("map", 1)
    progress.step("map", 1)             # the same call twice counts once
    assert progress.get("map")["done"] == 1
    for i in range(2, 51):
        clock.now += 1
        progress.step("map", i)
    job = progress.jobs()[0]
    assert (job["done"], job["total"], job["percent"]) == (50, 100, 50)
    # 49 s for the last 49 calls, so about another 50 s.
    assert job["eta"] == "under a minute left" and 45 <= job["eta_s"] <= 55
    for i in [0] + list(range(51, 100)):
        progress.step("map", i)
    assert progress.get("map")["finished"] and progress.jobs() == []


def test_small_jobs_are_not_shown():
    progress = Progress()
    progress.start("transcribe", "Transcribing calls that waited", [1, 2, 3])
    assert progress.jobs() == []
    assert progress.start("summaries", "Catching up", []).finished


def test_wording():
    assert bar(5, 10, width=10) == "[#####-----]"
    assert format_eta(None) == "working out time left"
    assert format_eta(6 * 60) == "about 6 min left"
    assert format_eta(125 * 60) == "about 2 h 5 min left"


def test_fetch_places_waits_for_the_map(monkeypatch):
    answers = iter([
        {"done": 10, "total": 40, "percent": 25, "eta": "about 1 min left", "finished": False},
        {"done": 40, "total": 40, "percent": 100, "eta": "", "finished": True},
    ])
    monkeypatch.setattr(fetch_places, "map_progress", lambda _config: next(answers))
    out = io.StringIO()
    assert fetch_places.wait_for_map(None, out=out, sleep=lambda _s: None)
    assert "10 / 40  25%  about 1 min left" in out.getvalue()
    assert "40 / 40  100%" in out.getvalue()


def test_fetch_places_gives_up_quietly_on_an_old_transcriber(monkeypatch):
    monkeypatch.setattr(fetch_places, "map_progress", lambda _config: None)
    assert not fetch_places.wait_for_map(None, out=io.StringIO(), sleep=lambda _s: None)
