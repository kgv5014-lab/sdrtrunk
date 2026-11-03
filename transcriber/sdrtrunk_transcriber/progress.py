"""Progress of long catch-up jobs (re-placing every call on the map, summaries after a restart, calls that
waited while the transcriber was closed), so the window, the map page and fetch-places.bat can show
"4,210 / 9,800  43%  about 6 min left" instead of nothing.

A job knows the items it started with. Items that arrive later (new calls) are not counted, so the bar only
moves forward. Each running job writes a line to the log every LOG_EVERY_S seconds and when it finishes.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

LOG_EVERY_S = 30
# Jobs smaller than this finish too quickly to be worth a line in the log or a note on the map.
MIN_ITEMS = 20
BAR_WIDTH = 24


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "working out time left"
    if seconds < 60:
        return "under a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min left"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min left"


def bar(done: int, total: int, width: int = BAR_WIDTH) -> str:
    filled = int(width * done / total) if total else width
    return "[" + "#" * filled + "-" * (width - filled) + "]"


class Job:
    def __init__(self, key: str, title: str, items, clock=time.monotonic):
        self.key = key
        self.title = title
        self._left = set(items)
        self.total = len(self._left)
        self.done = 0
        self._clock = clock
        self.started = clock()
        self._last_log = self.started
        self.finished = self.total == 0

    def eta_s(self) -> float | None:
        # Needs a few items (and a few seconds) before the speed means anything.
        elapsed = self._clock() - self.started
        if self.done < 3 or elapsed < 5:
            return None
        return (self.total - self.done) * elapsed / self.done

    def percent(self) -> int:
        return int(100 * self.done / self.total) if self.total else 100

    def line(self) -> str:
        return (f"{self.title}  {bar(self.done, self.total)} {self.done:,} / {self.total:,}  "
                f"{self.percent()}%  {format_eta(self.eta_s())}")

    def snapshot(self) -> dict:
        eta = self.eta_s()
        return {"key": self.key, "title": self.title, "done": self.done, "total": self.total,
                "percent": self.percent(), "eta_s": None if eta is None else round(eta),
                "eta": format_eta(eta), "finished": self.finished}


class Progress:
    """The catch-up jobs of one transcriber. Thread safe; each worker reports on its own thread."""

    def __init__(self, clock=time.monotonic):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def start(self, key: str, title: str, items) -> Job:
        """Starts (or restarts, e.g. a second map rebuild) a job over these items."""
        job = Job(key, title, items, self._clock)
        with self._lock:
            self._jobs[key] = job
        if job.total >= MIN_ITEMS:
            log.info("%s: %s to go", title, f"{job.total:,}")
        return job

    def step(self, key: str, item) -> None:
        """One item was handled (it doesn't matter whether it worked). Items not in the job are ignored."""
        message = None
        with self._lock:
            job = self._jobs.get(key)
            if job is None or job.finished or item not in job._left:
                return
            job._left.discard(item)
            job.done += 1
            now = self._clock()
            if job.done >= job.total:
                job.finished = True
                if job.total >= MIN_ITEMS:
                    message = f"{job.title}: finished all {job.total:,} in {format_minutes(now - job.started)}"
            elif job.total >= MIN_ITEMS and now - job._last_log >= LOG_EVERY_S:
                job._last_log = now
                message = job.line()
        if message:
            log.info("%s", message)

    def jobs(self) -> list[dict]:
        """Running jobs big enough to show."""
        with self._lock:
            return [j.snapshot() for j in self._jobs.values() if not j.finished and j.total >= MIN_ITEMS]

    def get(self, key: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(key)
            return job.snapshot() if job else None


def format_minutes(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"
