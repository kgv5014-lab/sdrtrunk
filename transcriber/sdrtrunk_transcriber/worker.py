"""Background thread that transcribes queued calls one at a time."""

import logging
import queue
import threading
import time
from pathlib import Path

from . import audio
from .backends import Backend
from .config import Config
from .db import Database

log = logging.getLogger(__name__)


def load_vocabulary(path: Path) -> str:
    """Builds a Whisper prompt from one-term-per-line vocabulary, ignoring blanks and # comments."""
    if not path.exists():
        return ""
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    # Whisper only reads the last ~224 tokens of a prompt, so keep it short.
    return ", ".join(terms)[:800]


class TranscriptionWorker:
    def __init__(self, config: Config, db: Database, backend_factory):
        self.config = config
        self.db = db
        self._backend_factory = backend_factory
        self._backend: Backend | None = None
        self._queue: "queue.Queue[int | None]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self.ready = threading.Event()
        self.prompt = load_vocabulary(config.vocabulary_path)

    def start(self):
        for call_id in self.db.pending_ids():
            self._queue.put(call_id)
        self._thread.start()

    def stop(self, timeout: float = 30):
        self._queue.put(None)
        self._thread.join(timeout)

    def submit(self, call_id: int):
        self._queue.put(call_id)

    def queue_size(self) -> int:
        return self._queue.qsize()

    def wait_idle(self, timeout: float = 30) -> bool:
        """Test helper: waits until the queue is drained."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False

    def _run(self):
        try:
            self._backend = self._backend_factory(self.config)
        except Exception:
            log.exception("Could not start the transcription backend; calls will be logged but not transcribed")
            self._backend = None
        self.ready.set()

        while True:
            call_id = self._queue.get()
            try:
                if call_id is None:
                    return
                self._process(call_id)
            except Exception as e:
                log.exception("Call %s failed", call_id)
                self.db.update_call(call_id, status="failed", error=str(e)[:500])
            finally:
                self._queue.task_done()

    def _process(self, call_id: int):
        call = self.db.get_call(call_id)
        if call is None:
            return

        self.db.update_call(call_id, status="processing")
        samples = audio.decode(self.config.data_path / call["audio_path"])
        duration = round(audio.duration_seconds(samples), 2)

        if self._backend is None:
            self.db.update_call(call_id, status="failed", duration_s=duration,
                                error="transcription backend did not start, see the log")
            return

        if duration < self.config.transcription.min_duration_s:
            self.db.update_call(call_id, status="skipped", duration_s=duration, error="too short")
            return

        started = time.perf_counter()
        result = self._backend.transcribe(samples, self.prompt)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self.db.update_call(
            call_id,
            status="done",
            duration_s=duration,
            transcript=result.text,
            confidence=result.confidence,
            backend=self._backend.name,
            model=self._backend.model,
            transcribe_ms=elapsed_ms,
            error=None,
        )
        log.info("Call %d TG %s (%.1fs) in %d ms: %s", call_id, call["talkgroup"], duration,
                 elapsed_ms, result.text or "(no speech)")
