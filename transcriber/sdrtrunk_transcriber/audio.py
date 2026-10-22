"""Decodes SDRTrunk's MP3 uploads into the 16 kHz mono audio Whisper expects."""

import io
import wave
from pathlib import Path

import av
import numpy as np

WHISPER_RATE = 16000


def decode(path: Path) -> np.ndarray:
    """Returns float32 samples in [-1, 1] at 16 kHz mono."""
    chunks = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=WHISPER_RATE)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))

    if not chunks:
        return np.zeros(0, dtype=np.float32)

    return np.concatenate(chunks).astype(np.float32) / 32768.0


def duration_seconds(samples: np.ndarray) -> float:
    return len(samples) / WHISPER_RATE


def to_wav_bytes(samples: np.ndarray) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(WHISPER_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()
