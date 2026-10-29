"""Background thread that transcribes queued calls one at a time."""

import itertools
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from . import airtalk, audio, selfcheck
from .address_fix import heard_note
from .backends import Backend
from .codes import CodeBook, to_twelve_hour
from .config import Config
from .corrections import CorrectionBook
from .db import Database
from .junk import remove_junk
from .places import PlaceBook

log = logging.getLogger(__name__)


# Whisper keeps only the last ~224 tokens of a prompt; Ken's 800-character prompts were 291 tokens, so
# the first place names were silently dropped. About 2.75 characters a token.
PROMPT_LIMIT = 600
# A call with no words and at least this much tone (paging tones, alert beeps) is labelled tones only.
TONES_ONLY_MIN_S = 1.0
TONES_ONLY = "tones only"
# Calls that are only a Morse-code station ID are skipped with this label and left out of the call log.
STATION_ID = "station ID"
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
    # A term in two lists (a learned street that is also a big road) is kept where it is weighed most.
    last = {t.lower(): i for i, t in enumerate(terms)}
    terms = [t for i, t in enumerate(terms) if last[t.lower()] == i]
    while terms and len(", ".join(terms)) > PROMPT_LIMIT:
        terms.pop(0)
    return terms


def build_prompt(*term_lists: list[str]) -> str:
    """Joins hint terms into a Whisper prompt (see prompt_terms)."""
    return ", ".join(prompt_terms(*term_lists))


class TranscriptionWorker:
    def __init__(self, config: Config, db: Database, backend_factory, codebook: CodeBook,
                 placebook: PlaceBook | None = None, corrections: CorrectionBook | None = None,
                 hints=None, address_fixer=None):
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
        self.air_vocabulary = airtalk.load_terms(config.air_vocabulary_path)
        # LearnedHints (hints.py), or None.
        self.hints = hints
        # AddressFixer (address_fix.py), or None.
        self.address_fixer = address_fixer
        # Spots garbled transcripts for a careful second try (selfcheck.py), or None.
        self.selfcheck = selfcheck.SelfCheck(config, self.placebook, self.codebook) \
            if config.self_check.enabled else None
        # Called with the call id after each transcript is saved (the incident map listens here).
        self.on_done = None
        # Called with (call, samples) with the audio as uploaded, before cleaning (the tone finder).
        self.on_audio = None

    def is_air(self, call: dict | None) -> bool:
        """Whether a call is on an airport or helicopter talkgroup ([air_transcripts])."""
        return airtalk.is_air(self.config.air_transcripts.talkgroups, call)

    def prompt_terms(self, *context: str | None, call: dict | None = None) -> list[str]:
        if self.is_air(call):
            # Air calls hear none of the county hints, which Whisper wrote into pilot talk.
            return prompt_terms(airtalk.PROMPT_TERMS, self.air_vocabulary)
        # Places first (dropped first when it's too long), then codes, then words fixed on the call log
        # page, then streets and units learned from the call log, then the user's own vocabulary, which
        # Whisper weighs most as it comes last.
        learned = self.hints.terms(call.get("talkgroup"), call.get("call_time")) if self.hints and call else []
        return prompt_terms(self.placebook.prompt_terms(*context), self.codebook.prompt_terms(),
                            self.corrections.prompt_terms(), learned, self.vocabulary)

    def prompt(self, *context: str | None, call: dict | None = None) -> str:
        return ", ".join(self.prompt_terms(*context, call=call))

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

        if self.config.audio.hide_station_ids and audio.is_morse_id(audio.band_pass(samples)):
            self.db.update_call(call_id, status="skipped", duration_s=duration, error=STATION_ID)
            log.info("Call %d TG %s (%.1fs): Morse station ID, hidden", call_id, call["talkgroup"], duration)
            return

        tone_s = 0.0
        air = self.is_air(call)
        if self.config.audio.clean:
            samples, tone_s = audio.clean(samples, self.config.audio.remove_tones,
                                          self.config.audio.reduce_hiss)
            if air and self.config.air_transcripts.level_speakers:
                samples = audio.level_speakers(samples)

        started = time.perf_counter()
        context = (call["talkgroup_label"], call["talkgroup_group"])
        terms = self.prompt_terms(*context, call=call)
        prompt = ", ".join(terms)
        result = self._backend.transcribe(samples, prompt)
        transcript, junk = self._finish(result, terms, call, context)
        check = {}
        if self.selfcheck:
            result, transcript, junk, check = self._self_check(
                call, samples, max(duration - tone_s, 0.0), prompt, terms, context, result, transcript, junk)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        address_fix = None
        if self.address_fixer and not air:
            # On the finished text, after any second try, so a real address is checked once.
            try:
                transcript, heard = self.address_fixer.fix(transcript, " ".join(filter(None, context)),
                                                           call["call_time"])
                address_fix = heard_note(heard)
            except Exception:
                log.exception("Address check failed on call %d", call_id)

        self.db.update_call(
            call_id,
            status="done",
            duration_s=duration,
            transcript=transcript,
            transcript_raw=result.text,
            address_fix=address_fix,
            confidence=result.confidence,
            backend=self._backend.name,
            model=self._backend.model,
            transcribe_ms=elapsed_ms,
            # Shown in place of "no speech found", so a tone-out doesn't look like a missed call.
            error=TONES_ONLY if not transcript and tone_s >= TONES_ONLY_MIN_S else None,
            **check,
        )
        notes = ([f"{tone_s:.1f}s of tones cut"] if tone_s else []) + [f"{name} removed" for name in junk] \
            + ([f"address heard as {address_fix}"] if address_fix else [])
        log.info("Call %d TG %s (%.1fs%s) in %d ms: %s", call_id, call["talkgroup"], duration,
                 "".join(", " + note for note in notes), elapsed_ms, transcript or "(no speech)")
        if self.on_done:
            self.on_done(call_id)

    def _self_check(self, call, samples, speech_s, prompt, terms, context, result, transcript, junk):
        """Checks a transcript for signs it is garbled (selfcheck.py). When it looks wrong, Whisper runs
        again more carefully and the better of the two is kept. Returns the kept result, transcript and
        junk, and the check's columns for the call log."""
        county = not self.is_air(call)
        problems = self.selfcheck.problems(transcript, result.confidence, speech_s, *context, county=county)
        if not problems:
            reason = self.selfcheck.ask_ollama(transcript, result.confidence, call["talkgroup_label"])
            problems = [reason] if reason else []
        if not problems:
            return result, transcript, junk, {"check_status": selfcheck.OK, "check_notes": None,
                                              "first_transcript": None}
        notes, first_transcript = "; ".join(problems), None
        if self.config.self_check.retry and self._backend.careful_retry:
            retry = self._backend.transcribe(samples, prompt, careful=True)
            retry_text, retry_junk = self._finish(retry, terms, call, context)
            retry_problems = self.selfcheck.problems(retry_text, retry.confidence, speech_s, *context,
                                                     county=county)
            if problems[0].startswith("AI:") and not retry_problems and retry_text != transcript:
                reason = self.selfcheck.ask_ollama(retry_text, retry.confidence, call["talkgroup_label"])
                retry_problems = [reason] if reason else []
            log.info("Call %d self-check: %s; careful retry: %s", call["id"], notes,
                     "; ".join(retry_problems) or "looks fine")
            if selfcheck.better((transcript, result.confidence, problems),
                                (retry_text, retry.confidence, retry_problems)):
                first_transcript = transcript
                result, transcript, junk, problems = retry, retry_text, retry_junk, retry_problems
            notes = "; ".join(problems) or None
        return result, transcript, junk, {
            "check_status": selfcheck.UNSURE if problems else selfcheck.FIXED,
            "check_notes": notes,
            "first_transcript": first_transcript,
        }

    def _finish(self, result, terms: list[str], call: dict, context: tuple) -> tuple[str, list[str]]:
        """Whisper's text cleaned up: junk removed, then places, codes and Ken's own fixes."""
        transcript, junk = result.text, []
        if self.config.transcription.junk_filter:
            transcript, junk = remove_junk(transcript, result.confidence, terms,
                                           self.config.transcription.stock_phrase_logprob,
                                           self.config.transcription.english_only)
        if self.is_air(call):
            # No county streets, 10-codes or dispatch times on the air band.
            transcript = airtalk.normalize(transcript)
            return self.corrections.normalize(transcript, call["talkgroup_label"]), junk
        # Places first, so "route ten" becomes Route 10 before the codes see "ten".
        transcript = self.placebook.normalize(transcript, *context)
        transcript = self.codebook.normalize(transcript, datetime.fromtimestamp(call["call_time"]))
        # Ken's own fixes last: they were learned from the finished transcript.
        transcript = self.corrections.normalize(transcript, call["talkgroup_label"])
        if self.config.codes.twelve_hour:
            transcript = to_twelve_hour(transcript)
        return transcript, junk
