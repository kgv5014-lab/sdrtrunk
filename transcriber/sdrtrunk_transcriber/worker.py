"""Background thread that transcribes queued calls one at a time."""

import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from . import audio
from .backends import Backend
from .codes import CodeBook
from .config import Config
from .db import Database
from .places import PlaceBook

log = logging.getLogger(__name__)


PROMPT_LIMIT = 800


def load_vocabulary(path: Path) -> list[str]:
    """One-term-per-line vocabulary, ignoring blanks and # comments."""
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def build_prompt(*term_lists: list[str]) -> str:
    """Joins hint terms into a Whisper prompt. Whisper only reads the last ~224 tokens of a prompt,
    so when it is too long the earliest terms are dropped."""
    terms = [t for terms in term_lists for t in terms]
    prompt = ", ".join(terms)
    while len(prompt) > PROMPT_LIMIT and terms:
        terms.pop(0)
        prompt = ", ".join(terms)
    return prompt


class TranscriptionWorker:
    def __init__(self, config: Config, db: Database, backend_factory, codebook: CodeBook,
                 placebook: PlaceBook | None = None):
        self.config = config
        self.db = db
        self._backend_factory = backend_factory
        self._backend: Backend | None = None
        self._queue: "queue.Queue[int | None]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self.ready = threading.Event()
        self.codebook = codebook
        self.placebook = placebook or PlaceBook(None)
        self.vocabulary = load_vocabulary(config.vocabulary_path)
        # Called with the call id after each transcript is saved (the incident map listens here).
        self.on_done = None

    def prompt(self, *context: str | None) -> str:
        # Places first (dropped first when it's too long), then codes, then the user's own vocabulary,
        # which Whisper weighs most as it comes last.
        return build_prompt(self.placebook.prompt_terms(*context), self.codebook.prompt_terms(), self.vocabulary)

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

        tone_s = 0.0
        if self.config.audio.clean:
            samples, tone_s = audio.clean(samples, self.config.audio.remove_tones)

        started = time.perf_counter()
        context = (call["talkgroup_label"], call["talkgroup_group"])
        result = self._backend.transcribe(samples, self.prompt(*context))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # Places first, so "route ten" becomes Route 10 before the codes see "ten".
        transcript = self.placebook.normalize(result.text, *context)
        transcript = self.codebook.normalize(transcript, datetime.fromtimestamp(call["call_time"]))

        self.db.update_call(
            call_id,
            status="done",
            duration_s=duration,
            transcript=transcript,
            transcript_raw=result.text,
            confidence=result.confidence,
            backend=self._backend.name,
            model=self._backend.model,
            transcribe_ms=elapsed_ms,
            error=None,
        )
        log.info("Call %d TG %s (%.1fs%s) in %d ms: %s", call_id, call["talkgroup"], duration,
                 f", {tone_s:.1f}s of tones cut" if tone_s else "", elapsed_ms, transcript or "(no speech)")
        if self.on_done:
            self.on_done(call_id)
