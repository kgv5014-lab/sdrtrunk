import numpy as np

from sdrtrunk_transcriber import airtalk, audio
from sdrtrunk_transcriber.config import Config
from sdrtrunk_transcriber.worker import PROMPT_LIMIT, prompt_terms


def test_tail_numbers_in_the_phonetic_alphabet_become_compact():
    assert airtalk.normalize("November eight one two Lima Foxtrot, runway two four") == "N812LF, runway 24"
    assert airtalk.normalize("November 8, 1, 2 Lima Foxtrot") == "N812LF"
    assert airtalk.normalize("cleared ILS 24, 275, Fox Alpha") == "cleared ILS 24, 275FA"


def test_single_phonetic_words_and_airline_call_signs_stay():
    for text in ["information Alpha", "taxi via Bravo", "5, Sierra, say again", "Skyline 735, heading 220"]:
        assert airtalk.normalize(text) == text


def test_numbers_read_digit_by_digit_are_joined():
    assert airtalk.normalize("Right heading 1, 7, 0") == "Right heading 170"
    assert airtalk.normalize("Runway 2-4, cleared to land") == "Runway 24, cleared to land"
    assert airtalk.normalize("Elm Town Approach, Altimeter 3, 0, 3, 1") == "Allentown Approach, Altimeter 3031"
    assert airtalk.normalize("squad 3353") == "squawk 3353"
    # Not a number word: left alone
    assert airtalk.normalize("one of the clouds") == "one of the clouds"


def test_mishearings_seen_on_kens_air_band():
    assert airtalk.normalize("Archer 257, Fox Street, Alpha") == "Archer 257FA"
    assert airtalk.normalize("contact Parisburg Approach") == "contact Harrisburg Approach"
    assert airtalk.normalize("Blue Sheik 51-56") == "Blue Streak 51-56"
    assert airtalk.normalize("1-3-0-4-6-3, Fox Street, Alpha") == "1-3-0-4-6-3, Foxtrot, Alpha"


def test_air_talkgroups_by_number_or_name():
    talkgroups = Config().air_transcripts.talkgroups
    assert airtalk.is_air(talkgroups, {"talkgroup": "9102", "talkgroup_label": "ABE Tower"})
    assert not airtalk.is_air(talkgroups, {"talkgroup": "10000", "talkgroup_label": "Countywide"})
    assert airtalk.is_air(["ABE Tower"], {"talkgroup": "9102", "talkgroup_label": "ABE Tower"})


def test_air_prompt_fits_with_room_for_kens_own_lines():
    terms = prompt_terms(airtalk.PROMPT_TERMS)
    assert terms == airtalk.PROMPT_TERMS
    assert len(", ".join(terms)) <= PROMPT_LIMIT - 30
    # The phonetic alphabet is at the end, where Whisper weighs it most
    assert terms[-1] == "Zulu"


def _speech(level_db, seconds, seed):
    rng = np.random.default_rng(seed)
    n = int(seconds * audio.WHISPER_RATE)
    syllables = np.abs(np.sin(np.arange(n) * np.pi * 4 / audio.WHISPER_RATE))
    return (rng.normal(0, 1, n) * syllables * 10 ** (level_db / 20)).astype(np.float32)


def test_loud_pilot_and_quiet_tower_come_out_level():
    hiss = np.random.default_rng(3).normal(0, 10 ** (-60 / 20), int(0.6 * audio.WHISPER_RATE)).astype(np.float32)
    call = np.concatenate([_speech(-12, 2.0, 1), hiss, _speech(-30, 2.0, 2)])
    before = [level for _s, _e, level in audio.speaker_runs(call)]
    after = [level for _s, _e, level in audio.speaker_runs(audio.level_speakers(call))]
    assert len(before) == 2 and before[0] - before[1] > 15
    assert len(after) == 2 and abs(after[0] - after[1]) < 2


def test_one_speaker_is_left_alone():
    call = _speech(-20, 2.0, 1)
    assert audio.level_speakers(call) is call
