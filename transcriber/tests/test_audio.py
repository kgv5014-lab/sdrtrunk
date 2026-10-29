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


def hiss(seconds, amplitude, seed=2):
    """FM-style hiss: noise that gets stronger with pitch, kept to the voice band."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    spectrum = np.fft.rfft(rng.normal(0, 1, n)) * np.clip(np.fft.rfftfreq(n, 1 / RATE) / 1000, 0.3, None)
    noise = audio.band_pass(np.fft.irfft(spectrum, n).astype(np.float32))
    return (amplitude * noise / np.sqrt(np.mean(noise ** 2))).astype(np.float32)


def error_db(reference, samples):
    return 10 * np.log10(np.sum((samples - reference) ** 2) / np.sum(reference ** 2))


def test_clean_digital_voice_is_left_alone():
    speech = audio.band_pass(voice(6.0))
    out, applied = audio.reduce_hiss(speech)
    assert not applied
    assert np.array_equal(out, speech)


def test_fm_hiss_is_turned_down():
    speech = audio.band_pass(voice(6.0, amplitude=0.1))
    noisy = speech + hiss(6.0, amplitude=0.05)
    out, applied = audio.reduce_hiss(noisy)
    assert applied
    assert len(out) == len(noisy)
    # At least 3 dB closer to the clean speech than before
    assert error_db(speech, out) <= error_db(speech, noisy) - 3.0


def test_hiss_reducer_survives_bad_samples():
    noisy = audio.band_pass(voice(2.0)) + hiss(2.0, amplitude=0.05)
    noisy[100] = np.nan
    noisy[200] = np.inf
    out, _ = audio.reduce_hiss(noisy)
    assert np.all(np.isfinite(out))


def test_short_call_is_not_touched_by_hiss_reducer():
    noisy = hiss(0.3, amplitude=0.05)
    out, applied = audio.reduce_hiss(noisy)
    assert not applied and out is noisy


def morse(text, wpm=18, freq=812.0, noise=0.02, seed=3):
    """Morse code on one pitch with carrier hiss, like a station ID on an FM channel."""
    codes = {"W": ".--", "N": "-.", "C": "-.-.", "1": ".----", "2": "..---", "3": "...--", "4": "....-"}
    unit = 1.2 / wpm
    pieces = [np.zeros(int(0.6 * RATE), dtype=np.float32)]
    for letter in text:
        for mark in codes[letter]:
            pieces += [tone(freq, unit * (3 if mark == "-" else 1), 0.5), np.zeros(int(unit * RATE), np.float32)]
        pieces.append(np.zeros(int(2 * unit * RATE), dtype=np.float32))
    signal = np.concatenate(pieces)
    return signal + np.random.default_rng(seed).normal(0, noise, len(signal)).astype(np.float32)


def test_morse_station_id_is_found():
    assert audio.is_morse_id(audio.band_pass(morse("WNC1234")))
    assert audio.is_morse_id(audio.band_pass(morse("WNC1234", wpm=12, freq=1000.0)))


def test_alert_beeps_pages_and_voice_are_not_morse():
    beeps = np.concatenate([np.concatenate([tone(1000, 0.5), np.zeros(int(0.3 * RATE), np.float32)])] * 5)
    page = np.concatenate([tone(853.2, 1.0), tone(1304.0, 3.0), voice(4.0)])
    assert not audio.is_morse_id(audio.band_pass(beeps))
    assert not audio.is_morse_id(audio.band_pass(page))
    for seed in range(5):
        assert not audio.is_morse_id(audio.band_pass(voice(6.0, seed=seed)))
    # A few short chirps on a call (talk-permit tones) are far fewer beeps than an ID
    chirps = np.concatenate([voice(1.0), tone(1000, 0.1), voice(1.0), tone(1000, 0.1), voice(1.0)])
    assert not audio.is_morse_id(audio.band_pass(chirps))
