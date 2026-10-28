"""Self-check: spots transcripts that look garbled, so the worker can run Whisper again more carefully.

Quick checks run on every finished transcript and need nothing but the local lists: Whisper was unsure,
a street that isn't in places.csv, a fire or EMS unit whose station isn't in codes.csv, words going round
in a loop, or far too many or too few words for the call's length. When a call passes those but Whisper
was only middling sure, the Ollama model used for summaries is asked for a second opinion. It only says
yes or no; it never rewrites the text.
"""

import logging
import re
import time

from .config import Config
from .summaries import OllamaClient

log = logging.getLogger(__name__)

# How the call log shows the outcome.
OK = "ok"              # nothing looked wrong
FIXED = "rechecked"    # looked wrong, the careful retry came out clean
UNSURE = "unsure"      # still looks wrong after the retry (or no retry was possible)

WORDS = re.compile(r"[A-Za-z0-9']+")
# Seconds to leave Ollama alone after it could not be reached.
OLLAMA_RETRY_S = 300

OLLAMA_SYSTEM = (
    "You check speech recognition of public safety radio (fire, EMS, police) in Northampton and Lehigh "
    "County, Pennsylvania. Radio talk is short and full of unit numbers, 10-codes, street names and "
    "clipped phrases; that is normal. Say a transcript is garbled only when words clearly make no sense "
    "there: nonsense or invented words, a sentence that falls apart, names that can't be right. Answer in JSON."
)
OLLAMA_PROMPT = (
    "Talkgroup: {talkgroup}\nTranscript: {transcript}\n\n"
    "Set \"garbled\" to true only if the speech recognition clearly got words wrong. Give \"reason\" in at "
    "most 8 words, naming the words that look wrong."
)
OLLAMA_SCHEMA = {
    "type": "object",
    "properties": {"garbled": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["garbled", "reason"],
}


def loop_words(text: str) -> str | None:
    """A word said 4 times running, or 2-4 words said 3 times running, that the junk filter left."""
    words = [w.lower() for w in WORDS.findall(text)]
    for size in range(1, 5):
        times = 4 if size == 1 else 3
        for i in range(len(words) - size * times + 1):
            chunk = words[i:i + size]
            if all(words[i + k * size:i + (k + 1) * size] == chunk for k in range(1, times)):
                return " ".join(chunk)
    return None


class SelfCheck:
    def __init__(self, config: Config, placebook, codebook, session=None):
        self.config = config
        self.sc = config.self_check
        self.placebook = placebook
        self.codebook = codebook
        self.ollama = None
        if self.sc.ollama and config.summaries.enabled:
            s = config.summaries
            self.ollama = OllamaClient(s.url, s.model, self.sc.ollama_timeout_s, s.keep_alive, session)
        self._ollama_down_until = 0.0

    def problems(self, transcript: str | None, confidence: float | None, speech_s: float,
                 *context: str | None) -> list[str]:
        """What looks wrong with a finished transcript, in plain words. Empty = looks fine."""
        text = transcript or ""
        if not text.strip():
            return []
        sc = self.sc
        found = []
        if confidence is not None and confidence < sc.low_confidence:
            found.append("Whisper unsure")
        if sc.streets:
            found += [f"unknown street \"{name}\"" for name in self.placebook.unknown_streets(text)]
        if sc.units:
            found += [f"unknown unit \"{name}\"" for name in self.codebook.unknown_units(text, *context)]
        loop = loop_words(text)
        if loop:
            found.append(f"\"{loop}\" repeats")
        words = len(WORDS.findall(text))
        if speech_s > 0 and sc.max_words_per_second and words / speech_s > sc.max_words_per_second \
                and words >= 8:
            found.append("too many words for the call's length")
        if speech_s >= sc.min_words_speech_s and words / speech_s < sc.min_words_per_second:
            found.append("too few words for the call's length")
        return found

    def ask_ollama(self, transcript: str | None, confidence: float | None, talkgroup: str | None) -> str | None:
        """Ollama's reason when it thinks a transcript is garbled, else None. Only asked about calls with
        enough words that Whisper was not sure of."""
        if self.ollama is None or not transcript or time.monotonic() < self._ollama_down_until:
            return None
        if confidence is None or confidence >= self.sc.ollama_below_confidence:
            return None
        if len(WORDS.findall(transcript)) < self.sc.ollama_min_words:
            return None
        try:
            answer = self.ollama.ask(OLLAMA_PROMPT.format(talkgroup=talkgroup or "unknown", transcript=transcript),
                                     OLLAMA_SCHEMA, system=OLLAMA_SYSTEM)
        except Exception as e:  # not running, no model yet, a bad answer: never hold up transcription
            self._ollama_down_until = time.monotonic() + OLLAMA_RETRY_S
            log.warning("Self-check: Ollama did not answer (%s); quick checks only for %d minutes",
                        e, OLLAMA_RETRY_S // 60)
            return None
        if answer.get("garbled") is True:
            reason = " ".join(str(answer.get("reason") or "").split())[:80]
            return f"AI: {reason}" if reason else "AI: looks garbled"
        return None


def better(first: tuple[str, float | None, list[str]], retry: tuple[str, float | None, list[str]]) -> bool:
    """Whether the retry's (transcript, confidence, problems) beats the first try's: fewer problems, or
    as few and Whisper surer. A blank retry never wins over words."""
    text1, conf1, problems1 = first
    text2, conf2, problems2 = retry
    if not (text2 or "").strip():
        return False
    if not (text1 or "").strip():
        return True
    if len(problems2) != len(problems1):
        return len(problems2) < len(problems1)
    return (conf2 if conf2 is not None else -9) > (conf1 if conf1 is not None else -9) + 0.05
