"""Whisper backends. Each one turns 16 kHz mono samples into a Transcript."""

import logging
import re
from dataclasses import dataclass

import numpy as np
import requests

from . import audio
from .config import Config

log = logging.getLogger(__name__)

# Phrases Whisper invents on silence or noise because it was trained on subtitled video.
# Plain "thank you" is left alone because dispatchers really say it.
HALLUCINATIONS = {
    "thanks for watching",
    "thanks for watching!",
    "thank you for watching",
    "thank you for watching.",
    "please subscribe",
    "please subscribe.",
    "subscribe to my channel",
    "subtitles by the amara.org community",
    "you",
    ".",
}


@dataclass
class Segment:
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass
class Transcript:
    text: str
    confidence: float | None


class Backend:
    name = "none"
    model = ""

    def transcribe(self, samples: np.ndarray, prompt: str) -> Transcript:
        raise NotImplementedError


def clean_segments(segments: list[Segment], no_speech_threshold: float,
                   log_prob_threshold: float) -> Transcript:
    """Drops segments Whisper flags as probably-not-speech and known hallucinated phrases."""
    kept = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        # Whisper's own rule: high no-speech probability AND low confidence means silence.
        if (seg.no_speech_prob is not None and seg.avg_logprob is not None
                and seg.no_speech_prob > no_speech_threshold
                and seg.avg_logprob < log_prob_threshold):
            continue
        if re.sub(r"\s+", " ", text.lower()) in HALLUCINATIONS:
            continue
        kept.append(Segment(text, seg.avg_logprob, seg.no_speech_prob))

    text = " ".join(seg.text for seg in kept).strip()
    probs = [seg.avg_logprob for seg in kept if seg.avg_logprob is not None]
    confidence = round(sum(probs) / len(probs), 3) if probs else None
    return Transcript(text, confidence)


class FasterWhisperBackend(Backend):
    name = "faster-whisper"

    def __init__(self, config: Config):
        from faster_whisper import WhisperModel

        fw = config.faster_whisper
        self.config = config
        self.model = fw.model
        log.info("Loading faster-whisper model %s on %s:%d (%s), first run downloads it",
                 fw.model, fw.device, fw.device_index, fw.compute_type)
        self._model = WhisperModel(
            fw.model,
            device=fw.device,
            device_index=fw.device_index,
            compute_type=fw.compute_type,
            download_root=fw.download_root or None,
        )
        log.info("faster-whisper model loaded")

    def transcribe(self, samples: np.ndarray, prompt: str) -> Transcript:
        tc = self.config.transcription
        segments, _info = self._model.transcribe(
            samples,
            language=tc.language or None,
            beam_size=tc.beam_size,
            initial_prompt=prompt or None,
            # Each radio call stands alone; carrying text over spreads one bad guess.
            condition_on_previous_text=False,
            vad_filter=self.config.faster_whisper.vad_filter,
            no_speech_threshold=tc.no_speech_threshold,
            log_prob_threshold=tc.log_prob_threshold,
        )
        return clean_segments(
            [Segment(s.text, s.avg_logprob, s.no_speech_prob) for s in segments],
            tc.no_speech_threshold, tc.log_prob_threshold,
        )


class WhisperCppBackend(Backend):
    """Talks to whisper.cpp's bundled HTTP server (whisper-server)."""

    name = "whisper-cpp"

    def __init__(self, config: Config):
        self.config = config
        self.url = config.whisper_cpp.url
        self.model = "whisper.cpp server"
        self._session = requests.Session()

    def transcribe(self, samples: np.ndarray, prompt: str) -> Transcript:
        tc = self.config.transcription
        data = {
            "response_format": "verbose_json",
            "temperature": "0.0",
            "beam_size": str(tc.beam_size),
            "no_speech_thold": str(tc.no_speech_threshold),
            # verbose_json otherwise runs an extra language-detection pass on every call.
            "no_language_probabilities": "true",
            "token_timestamps": "false",
        }
        if tc.language:
            data["language"] = tc.language
        if prompt:
            data["prompt"] = prompt

        response = self._session.post(
            self.url,
            files={"file": ("call.wav", audio.to_wav_bytes(samples), "audio/wav")},
            data=data,
            timeout=self.config.whisper_cpp.timeout_s,
        )
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"whisper.cpp server error: {body['error']}")

        segments = body.get("segments")
        if segments is None:
            segments = [{"text": body.get("text", "")}]
        return clean_segments(
            [Segment(s.get("text", ""), s.get("avg_logprob"), s.get("no_speech_prob")) for s in segments],
            tc.no_speech_threshold, tc.log_prob_threshold,
        )


class NoBackend(Backend):
    """Logs calls without transcribing them."""

    def transcribe(self, samples: np.ndarray, prompt: str) -> Transcript:
        return Transcript("", None)


def create_backend(config: Config) -> Backend:
    name = config.transcription.backend
    if name == "faster-whisper":
        return FasterWhisperBackend(config)
    if name == "whisper-cpp":
        return WhisperCppBackend(config)
    if name == "none":
        return NoBackend()
    raise ValueError(f"Unknown transcription backend '{name}' (use faster-whisper, whisper-cpp or none)")
