"""Airport and helicopter radio (AM air band): the Whisper hints and the cleanup used on those talkgroups in
place of the county ones.

County hints (streets, units, RIT, Tac 2, Cetronia EMS) made Whisper hear county words in pilot talk: on
Ken's first day of air band, "RIT" was written 347 times and "Fox Street" 78 times. Air calls get their own
hint list instead, and the county cleanup (street fixes, 10-codes, times) is skipped, since it turns
"one zero four thousand" into "10-4,000".
"""

import re
from pathlib import Path

# The hints Whisper gets on air talkgroups, least weighted first (Whisper weighs the end of a prompt most).
# About 560 characters, so Ken's own air-vocabulary.txt lines fit before the 600 character limit by
# pushing out the first of these.
PROMPT_TERMS = [
    "FedEx", "UPS", "Allegiant", "Execjet", "Blue Streak", "Lifeguard",
    "Life Flight",
    "New York Center", "Harrisburg Approach", "Queen City", "Lehigh Valley",
    "Allentown Approach", "Allentown Tower",
    "VFR", "IFR", "ILS", "RNAV", "wilco",
    "squawk", "altimeter", "heading", "maintain", "descend",
    "runway 24", "runway 6", "runway 13", "runway 31", "taxiway", "hold short", "line up and wait",
    "cleared for takeoff", "cleared to land", "niner",
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet", "Kilo",
    "Lima", "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo", "Sierra", "Tango", "Uniform", "Victor",
    "Whiskey", "X-ray", "Yankee", "Zulu",
]

PHONETIC = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "foxtrot": "F",
    "fox": "F", "golf": "G", "gulf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J",
    "kilo": "K", "lima": "L", "mike": "M", "november": "N", "oscar": "O", "papa": "P", "quebec": "Q",
    "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U", "victor": "V", "whiskey": "W", "x-ray": "X",
    "xray": "X", "yankee": "Y", "zulu": "Z",
}
DIGIT_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9"}

# Things Whisper mishears on Ken's air band, (pattern, fix), case ignored. Only applied on air talkgroups.
FIXES = [
    (r"\bfox street\b", "Foxtrot"),
    (r"\b(?:elm|ellen|allen|alan|alton) ?town\b(?= (?:tower|approach|ground|departure))", "Allentown"),
    (r"\bparisburg\b", "Harrisburg"),
    (r"\bsquad\b(?= \d)", "squawk"),
    (r"\bsquak\b", "squawk"),
    (r"\bniner\b", "9"),
    (r"\bexec jet\b", "Execjet"),
    (r"\bblue (?:sheik|streek|sheet)\b", "Blue Streak"),
    (r"\bRN(?:F|AP)\b", "RNAV"),
    (r"\bflex jet\b", "Flexjet"),
    (r"\bfed ex\b", "FedEx"),
    (r"\bclear(?:ed)? the land\b", "cleared to land"),
    (r"\bline up a wait\b", "line up and wait"),
]

_PHONETIC_WORD = "|".join(sorted((re.escape(w) for w in PHONETIC), key=len, reverse=True))
_SEP = r"(?:[,.]?\s+|-)"
# A number ("812") or one read digit by digit ("8, 1, 2"); never two numbers ("24, 275").
_DIGIT_RUN = r"(?:\d(?:(?:,\s*|-|\s)\d(?!\d))+|\d+)"
# "November 8, 1, 2 Lima Foxtrot", "463 Fox Alpha", "N812LF" left alone.
_CALL_SIGN = re.compile(
    rf"\b(?P<november>November{_SEP})?(?P<digits>{_DIGIT_RUN})(?P<letters>(?:{_SEP}(?:{_PHONETIC_WORD})\b){{1,3}})",
    re.IGNORECASE)
# A number read digit by digit after a word that takes one: "heading 1, 7, 0" -> "heading 170",
# "runway 2-4" -> "runway 24", "squawk 3, 3, 5, 3" -> "squawk 3353".
_NUMBER_WORDS = r"runway|heading|squawk|altimeter|flight level|wind|ILS|RNAV"
_SPOKEN_NUMBER = re.compile(rf"\b(?P<word>{_NUMBER_WORDS}),?\s+(?P<digits>\d(?:(?:,\s*|-|\s)\d)+)\b", re.IGNORECASE)
_DIGIT_WORD_RUN = re.compile(
    r"\b(?:%s)(?:[,-]?\s*(?:%s))+\b" % ("|".join(DIGIT_WORDS), "|".join(DIGIT_WORDS)), re.IGNORECASE)


def is_air(talkgroups: list, call: dict | None) -> bool:
    """Whether a call is on one of the air band talkgroups (by number or name)."""
    if not call or not talkgroups:
        return False
    names = {str(call.get(k) or "").lower() for k in ("talkgroup", "talkgroup_label")}
    return any(str(t).lower() in names for t in talkgroups)


def load_terms(path: Path | None) -> list[str]:
    """Ken's own air hints, one per line, # comments ignored. Missing file = none."""
    if path is None or not path.exists():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _call_sign(match: re.Match) -> str:
    letters = [PHONETIC[w.lower()] for w in re.findall(_PHONETIC_WORD, match["letters"], re.IGNORECASE)]
    # Tail numbers have at most 5 digits; more is a heading or altitude run into one.
    # One letter after a number is too often something else ("5, Sierra", "runway 24 Alpha"); two or more,
    # or a leading November, is a tail number.
    digits = _digits(match["digits"])
    if not match["november"] and len(letters) < 2 or len(digits) > 5:
        return match[0]
    return ("N" if match["november"] else "") + digits + "".join(letters)


def normalize(text: str) -> str:
    """Air band cleanup: fixes common mishearings, spoken digits after runway/heading/squawk and the like,
    and tail numbers read in the phonetic alphabet ("November 8, 1, 2 Lima Foxtrot" -> "N812LF")."""
    if not text:
        return text
    for pattern, fix in FIXES:
        text = re.sub(pattern, fix, text, flags=re.IGNORECASE)
    text = _DIGIT_WORD_RUN.sub(
        lambda m: "".join(DIGIT_WORDS[w.lower()] for w in re.findall(r"[a-z]+", m[0], re.IGNORECASE)), text)
    text = _SPOKEN_NUMBER.sub(lambda m: f"{m['word']} {_digits(m['digits'])}", text)
    return _CALL_SIGN.sub(_call_sign, text)
