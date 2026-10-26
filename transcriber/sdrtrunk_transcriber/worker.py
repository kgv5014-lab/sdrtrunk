"""Background thread that transcribes queued calls one at a time."""

import itertools
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
from .corrections import CorrectionBook
from .db import Database
from .junk import remove_junk
from .places import PlaceBook

log = logging.getLogger(__name__)


PROMPT_LIMIT = 800
# A call with no words and at least this much tone (paging tones, alert beeps) is labelled tones only.
TONES_ONLY_MIN_S = 1.0
TONES_ONLY = "tones only"
LAST_RANK = 1_000_000
# The sample terms that vocabulary.example.txt used to ship with. Left in place, Whisper echoes them
# back on unclear audio ("10-4, Main Street, Battalion 1"), so they are ignored.
SAMPLE_VOCABULARY = ["Dispatch", "Engine 12", "Medic 4", "Battalion 1", "10-4", "Main Street"]


def load_vocabulary(path: Path) -> list[str]:
    """One-term-per-line vocabulary, ignoring blanks and # comments."""
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    if terms == SAMPLE_VOCABULARY:
        log.warning("%s still holds only the sample terms; ignoring them. Put your own unit and street "
                    "names there, or leave it empty.", path)
        return []
    return terms


def prompt_terms(*term_lists: list[str]) -> list[str]:
    """The hint terms that fit in a Whisper prompt. Whisper only reads the last ~224 tokens of a
    prompt, so when there are too many the earliest terms are dropped."""
    terms = [t for terms in term_lists for t in terms]
    while terms and len(", ".join(terms)) > PROMPT_LIMIT:
        terms.pop(0)
    return terms


def build_prompt(*term_lists: list[str]) -> str:
    """Joins hint terms into a Whisper prompt (see prompt_terms)."""
    return ", ".join(prompt_terms(*term_lists))


class TranscriptionWorker:
    def __init__(self, config: Config, db: Database, backend_factory, codebook: CodeBook,
                 placebook: PlaceBook | None = None, corrections: CorrectionBook | None = None):
        self.config = config
        self.db = db
        self._backend_factory = backend_factory
        self._backend: Backend | None = None
        # (rank, arrival order, call id): lower ranks first, oldest first within a rank.
        self._queue: "queue.PriorityQueue[tuple[int, int, int | None]]" = queue.PriorityQueue()
        self._order = itertools.count()
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self.ready = threading.Event()
        self.codebook = codebook
        self.placebook = placebook or PlaceBook(None)
        self.corrections = corrections or CorrectionBook(None)
        self.vocabulary = load_vocabulary(config.vocabulary_path)
        # Called with the call id after each transcript is saved (the incident map listens here).
        self.on_done = None
        # Called with (call, samples) with the audio as uploaded, before cleaning (the tone finder).
        self.on_audio = None

    def prompt_terms(self, *context: str | None) -> list[str]:
        # Places first (dropped first when it's too long), then codes, then words fixed on the call log
        # page, then the user's own vocabulary, which Whisper weighs most as it comes last.
        return prompt_terms(self.placebook.prompt_terms(*context), self.codebook.prompt_terms(),
                            self.corrections.prompt_terms(), self.vocabulary)

    def prompt(self, *context: str | None) -> str:
        return ", ".join(self.prompt_terms(*context))

    def start(self):
        for call_id in self.db.pending_ids():
            self.submit(call_id)
        self._thread.start()

    def stop(self, timeout: float = 30):
        # Ranked after every call, so the queue drains first as before.
        self._queue.put((LAST_RANK, next(self._order), None))
        self._thread.join(timeout)

    def rank(self, call: dict | None) -> int:
        """Where a call goes in line: its place in first_talkgroups, or after all of them."""
        first = self.config.transcription.first_talkgroups
        if call:
            names = " ".join(str(call.get(k) or "") for k in ("talkgroup_label", "talkgroup_group", "talkgroup"))
            names = names.lower()
            for i, word in enumerate(first):
                if str(word).lower() in names:
                    return i
        return len(first)

    def submit(self, call_id: int):
        self._queue.put((self.rank(self.db.get_call(call_id)), next(self._order), call_id))

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
            _rank, _order, call_id = self._queue.get()
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

        if self.on_audio:
            try:
                self.on_audio(call, samples)
            except Exception:
                log.exception("Tone finder failed on call %d", call_id)

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
        terms = self.prompt_terms(*context)
        result = self._backend.transcribe(samples, ", ".join(terms))
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        transcript, junk = result.text, []
        if self.config.transcription.junk_filter:
            transcript, junk = remove_junk(transcript, result.confidence, terms,
                                           self.config.transcription.stock_phrase_logprob,
                                           self.config.transcription.english_only)
        # Places first, so "route ten" becomes Route 10 before the codes see "ten".
        transcript = self.placebook.normalize(transcript, *context)
        transcript = self.codebook.normalize(transcript, datetime.fromtimestamp(call["call_time"]))
        # Ken's own fixes last: they were learned from the finished transcript.
        transcript = self.corrections.normalize(transcript, call["talkgroup_label"])

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
            # Shown in place of "no speech found", so a tone-out doesn't look like a missed call.
            error=TONES_ONLY if not transcript and tone_s >= TONES_ONLY_MIN_S else None,
        )
        notes = ([f"{tone_s:.1f}s of tones cut"] if tone_s else []) + [f"{name} removed" for name in junk]
        log.info("Call %d TG %s (%.1fs%s) in %d ms: %s", call_id, call["talkgroup"], duration,
                 "".join(", " + note for note in notes), elapsed_ms, transcript or "(no speech)")
        if self.on_done:
            self.on_done(call_id)
