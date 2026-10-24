"""Decodes SDRTrunk's MP3 uploads into the 16 kHz mono audio Whisper expects, and cleans it up first."""

import io
import wave
from pathlib import Path

import av
import numpy as np

WHISPER_RATE = 16000

# Voice band kept by clean(). Below it is CTCSS/DCS hum and rumble; above it is hiss.
VOICE_LOW_HZ = (200.0, 300.0)
VOICE_HIGH_HZ = (3400.0, 3800.0)
# Average level of the speech after clean(), in dB below full scale.
TARGET_DB = -20.0
MAX_GAIN_DB = 30.0
# Frames quieter than this are pauses and don't count toward the level.
QUIET_DB = -50.0

# Tone finder: 32 ms frames, 16 ms apart.
FRAME = 512
HOP = 256
# A frame is a tone when this much of its energy sits in one narrow peak. Pure tones score ~0.99,
# speech well under 0.5, since voices spread energy over many harmonics.
TONE_PURITY = 0.75
# Shortest steady tone to cut. Paging tones last 0.5 s and more; vowels never stay this pure this long.
TONE_MIN_S = 0.25
# Silence left where a tone was, so the words either side don't run together.
TONE_GAP_S = 0.25


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


def _db(power: float) -> float:
    return 10.0 * np.log10(max(power, 1e-12))


def band_pass(samples: np.ndarray) -> np.ndarray:
    """Keeps the voice band with gentle (raised cosine) edges, using one FFT over the whole call."""
    if len(samples) == 0:
        return samples
    spectrum = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(len(samples), 1.0 / WHISPER_RATE)
    gain = np.ones_like(freqs)
    (lo0, lo1), (hi0, hi1) = VOICE_LOW_HZ, VOICE_HIGH_HZ
    gain[freqs <= lo0] = 0.0
    rising = (freqs > lo0) & (freqs < lo1)
    gain[rising] = 0.5 - 0.5 * np.cos(np.pi * (freqs[rising] - lo0) / (lo1 - lo0))
    falling = (freqs > hi0) & (freqs < hi1)
    gain[falling] = 0.5 + 0.5 * np.cos(np.pi * (freqs[falling] - hi0) / (hi1 - hi0))
    gain[freqs >= hi1] = 0.0
    return np.fft.irfft(spectrum * gain, n=len(samples)).astype(np.float32)


def find_tones(samples: np.ndarray) -> list[tuple[int, int]]:
    """Sample ranges (start, end) holding a steady single tone, such as two-tone paging or alert beeps."""
    if len(samples) < FRAME:
        return []
    count = 1 + (len(samples) - FRAME) // HOP
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    power = np.abs(np.fft.rfft(samples[index] * np.hanning(FRAME), axis=1)) ** 2
    freqs = np.fft.rfftfreq(FRAME, 1.0 / WHISPER_RATE)
    band = (freqs >= VOICE_LOW_HZ[1]) & (freqs <= VOICE_HIGH_HZ[0])
    power = power[:, band]
    total = power.sum(axis=1)
    peak = power.argmax(axis=1)
    # Energy within 3 bins (~90 Hz) of the strongest bin
    cumulative = np.concatenate([np.zeros((count, 1)), power.cumsum(axis=1)], axis=1)
    rows = np.arange(count)
    lo = np.clip(peak - 3, 0, power.shape[1])
    hi = np.clip(peak + 4, 0, power.shape[1])
    purity = (cumulative[rows, hi] - cumulative[rows, lo]) / np.maximum(total, 1e-20)
    loud = 10.0 * np.log10(np.maximum(total / (FRAME * FRAME * 3.0 / 16.0), 1e-12)) > QUIET_DB
    tonal = (purity >= TONE_PURITY) & loud

    ranges = []
    min_frames = int(np.ceil(TONE_MIN_S * WHISPER_RATE / HOP))
    start = None
    for i, is_tone in enumerate(np.append(tonal, False)):
        if is_tone and start is None:
            start = i
        elif not is_tone and start is not None:
            if i - start >= min_frames:
                ranges.append((start * HOP, min(len(samples), (i - 1) * HOP + FRAME)))
            start = None
    return ranges


def remove_ranges(samples: np.ndarray, ranges: list[tuple[int, int]]) -> np.ndarray:
    """Cuts the ranges out, leaving a short silence in each place, with quick fades so cuts don't click."""
    if not ranges:
        return samples
    fade = int(0.01 * WHISPER_RATE)
    gap = np.zeros(int(TONE_GAP_S * WHISPER_RATE), dtype=np.float32)
    pieces, position = [], 0
    for start, end in ranges:
        piece = samples[position:start].copy()
        if len(piece) > fade:
            piece[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
        if position > 0 and len(piece) > fade:
            piece[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        pieces += [piece, gap]
        position = end
    tail = samples[position:].copy()
    if len(tail) > fade:
        tail[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
    pieces.append(tail)
    return np.concatenate(pieces)


def normalize(samples: np.ndarray) -> np.ndarray:
    """Sets the average level of the speech (pauses left out) to TARGET_DB, with a soft limiter on peaks."""
    if len(samples) < HOP:
        return samples
    frames = samples[: len(samples) // HOP * HOP].reshape(-1, HOP)
    power = (frames.astype(np.float64) ** 2).mean(axis=1)
    active = power[10.0 * np.log10(np.maximum(power, 1e-12)) > QUIET_DB]
    if len(active) == 0:
        return samples
    gain_db = min(MAX_GAIN_DB, TARGET_DB - _db(float(active.mean())))
    out = samples * np.float32(10.0 ** (gain_db / 20.0))
    knee = 0.8
    over = np.abs(out) > knee
    out[over] = np.sign(out[over]) * (knee + (1.0 - knee) * np.tanh((np.abs(out[over]) - knee) / (1.0 - knee)))
    return out.astype(np.float32)


def clean(samples: np.ndarray, remove_tones: bool = True) -> tuple[np.ndarray, float]:
    """Prepares a call for Whisper: voice band only, steady tones cut out, speech at a standard level.
    Returns the cleaned samples and how many seconds of tone were cut."""
    filtered = band_pass(samples.astype(np.float32))
    tone_s = 0.0
    if remove_tones:
        ranges = find_tones(filtered)
        tone_s = sum(end - start for start, end in ranges) / WHISPER_RATE
        filtered = remove_ranges(filtered, ranges)
    cleaned = normalize(filtered)
    # A call that was all tone leaves only silence; give Whisper a full second of it rather than nothing.
    if len(cleaned) < WHISPER_RATE:
        cleaned = np.concatenate([cleaned, np.zeros(WHISPER_RATE - len(cleaned), dtype=np.float32)])
    return cleaned, round(tone_s, 2)
