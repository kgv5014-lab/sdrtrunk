"""Cleanup applied to calls before Whisper: voice band, tone removal and level."""

import numpy as np

from sdrtrunk_transcriber import audio

RATE = audio.WHISPER_RATE


def tone(freq, seconds, amplitude=0.9):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def voice(seconds, amplitude=0.1, seed=1):
    """Voice-like sound: a wobbling 110-140 Hz pitch with many harmonics, in syllables with pauses."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    pitch = 125 + 15 * np.sin(2 * np.pi * 3 * t) + rng.normal(0, 2, len(t)).cumsum() * 0.01
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    sound = sum(np.sin(k * phase) / k ** 0.7 for k in range(1, 28))
    sound += rng.normal(0, 0.3, len(t))
    syllables = np.clip(np.sin(2 * np.pi * 2.5 * t), 0, None)
    sound *= syllables
    return (amplitude * sound / np.sqrt(np.mean(sound ** 2))).astype(np.float32)


def level_db(samples):
    frames = samples[: len(samples) // 256 * 256].reshape(-1, 256)
    power = (frames.astype(np.float64) ** 2).mean(axis=1)
    active = power[10 * np.log10(np.maximum(power, 1e-12)) > audio.QUIET_DB]
    return 10 * np.log10(active.mean())


def test_two_tone_page_is_cut_and_voice_kept():
    page = np.concatenate([tone(853.2, 1.0), tone(1304.0, 3.0), voice(4.0)])
    ranges = audio.find_tones(audio.band_pass(page))
    cut = sum(end - start for start, end in ranges) / RATE
    assert 3.7 <= cut <= 4.1
    # Nothing in the voice part is cut
    assert all(end <= 4.1 * RATE for _start, end in ranges)

    cleaned, tone_s = audio.clean(page)
    assert 3.7 <= tone_s <= 4.1
    assert abs(len(cleaned) / RATE - (8.0 - tone_s + audio.TONE_GAP_S * len(ranges))) < 0.05


def test_voice_alone_is_not_cut():
    for seed in range(5):
        assert audio.find_tones(audio.band_pass(voice(6.0, seed=seed))) == []


def test_short_beep_is_kept():
    # A beep shorter than TONE_MIN_S (like a talk-permit chirp) is left for the band-pass and level only
    assert audio.find_tones(np.concatenate([voice(1.0), tone(1000, 0.1), voice(1.0)])) == []


def test_quiet_and_loud_calls_end_up_at_the_same_level():
    quiet, _ = audio.clean(voice(5.0, amplitude=0.005))
    loud, _ = audio.clean(voice(5.0, amplitude=0.5))
    assert abs(level_db(quiet) - audio.TARGET_DB) < 1.5
    assert abs(level_db(loud) - audio.TARGET_DB) < 1.5
    assert np.abs(loud).max() <= 1.0


def test_ctcss_hum_is_filtered_out():
    hum = tone(100.0, 3.0, amplitude=0.2)
    filtered = audio.band_pass(hum)
    assert np.sqrt(np.mean(filtered ** 2)) < 0.002


def test_all_tone_call_gives_whisper_silence():
    cleaned, tone_s = audio.clean(tone(1000, 2.0))
    assert tone_s > 1.8
    assert len(cleaned) >= RATE
    assert np.abs(cleaned).max() < 0.05


def test_cleanup_can_keep_tones():
    cleaned, tone_s = audio.clean(np.concatenate([tone(853.2, 1.0), voice(2.0)]), remove_tones=False)
    assert tone_s == 0.0
    assert abs(len(cleaned) / RATE - 3.0) < 0.01
