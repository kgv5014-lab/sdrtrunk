"""Local radio codes: Whisper hints, spoken-code cleanup, and meaning labels.

Everything comes from one CSV that the user edits (codes.csv), with columns:
    type        ten-code | signal | unit-suffix | station | police-esn | call-type | acronym
    code        10-23, Signal 33, 81-89 (a range of the last two digits of a call sign), 20 (a station or
                police department number), ALS (a call type or acronym, matched as written)
    meaning     what it means, shown next to the transcript
    applies_to  all | police | fire | ems | fire-ems   (picks between agencies that use a code differently)
    prompt      yes to include it in Whisper's hint list (keep this short; Whisper only reads ~200 words)
    source      free text, ignored

The file is re-read whenever it changes, so edits apply without restarting.
"""

import csv
import io
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}

_ONES = "|".join(ONES)
_TENS = "|".join(TENS)
# A spoken number from 0 to 99: "four", "twenty three", "twenty-three", or digits.
_NUMBER = rf"(?:(?:{_TENS})(?:[\s-](?:{_ONES}))?|{_ONES}|\d{{1,2}})"

# "ten four", "Ten-twenty three", "10 4", "10, 23". Bare "1023" is left alone: in this county
# that is usually a call sign (station 10, unit 23).
SPOKEN_TEN_CODE = re.compile(rf"\b(?:ten|10)(?:,?[\s-]+)({_NUMBER})\b", re.IGNORECASE)
SPOKEN_SIGNAL = re.compile(rf"\bsignal\s+({_NUMBER})\b", re.IGNORECASE)

TEN_CODE = re.compile(r"\b10-(\d{1,2})\b")
SIGNAL = re.compile(r"\bSignal (\d{1,3})\b")
# Four-digit call signs: two-digit station + two-digit unit. Skipped when followed by a street word.
CALL_SIGN = re.compile(r"\b(\d{2})(\d{2})\b(?!\s+(?:[A-Z][a-z]+\s+)?(?:street|st|avenue|ave|road|rd|lane|ln|"
                       r"drive|dr|boulevard|blvd|way|court|ct|pike|highway|hwy|place|pl)\b)", re.IGNORECASE)

# Dispatch times: "1346", "13 46" or "13:46". Unit words before the number mean it is a call sign.
SPOKEN_TIME = re.compile(r"(?<![\d:])(?<!-)([01]\d|2[0-3])[\s:]?([0-5]\d)\b(?![:-]\d)")
UNIT_WORD_BEFORE = re.compile(r"(?:engine|medic|ladder|tanker|truck|rescue|squad|ems|unit|car|chief|"
                              r"station|ambulance|qrs|qrv|brush|tower|utility|box|number|#)\s*$", re.IGNORECASE)
STREET_WORD_AFTER = re.compile(r"^\s+(?:[A-Z][a-z]+\s+)?(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|"
                               r"boulevard|blvd|way|court|ct|pike|highway|hwy|place|pl)\b", re.IGNORECASE)
# Dispatch ends a call with the current time, give or take a minute or two of clock difference.
# Whisper writes it many ways: "1416", "14-16", "14, 11", "13.43", "30 hours", and sometimes glued to
# the channel number before it ("Ops 113-30" is Ops 1, 13:30).
TRAILING_TIME = re.compile(r"(?<![\w.,:-])(\d[\d\s,.:-]*\d)(\s+hours)?\s*[.!]?\s*$", re.IGNORECASE)
# Words dispatch says just before the time, so a "10-28" there is the time and not a 10-code.
DISPATCH_WORD_BEFORE = re.compile(r"(?:ops|tac|dispatch|communications|office|ems|fire|channel|"
                                  r"talkgroup|countywide)\W*(?:\d\W*)?$", re.IGNORECASE)
# Call signs written with a dash ("15-82"), and station numbers said with the service ("EMS 15").
DASHED_CALL_SIGN = re.compile(r"(?<![\d-])(\d{2})-(\d{2})(?![\d-])(?!\s+(?:[A-Z][a-z]+\s+)?(?:street|st|"
                              r"avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|way|court|ct|pike|"
                              r"highway|hwy|place|pl)\b)", re.IGNORECASE)
STATION_REF = re.compile(r"\b(?:fire|ems|station|medic|company)\s+(\d{2})\b|\b(\d{2})\s+command\b", re.IGNORECASE)
# Police cars: department number then car number ("2741" is Palmer Twp car 41).
POLICE_CAR = re.compile(r"(?<![\d.,-])(\d{2})(\d{1,2})(?![\d-])(?!,?\s+(?:[A-Z][a-z]+\s+){0,2}(?:street|st|"
                        r"avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|way|court|ct|pike|highway|hwy|"
                        r"place|pl|circle|north|south|east|west)\b)", re.IGNORECASE)
# Words before a three-digit number that make it a road or house number, not a police car.
ADDRESS_WORD_BEFORE = re.compile(r"(?:route|rt|on|at|of|to|out|in|from|near|by)\W*$", re.IGNORECASE)
# A fire or EMS unit named with its number: "Engine 4511" (station 45, unit 11) or "Station 45".
UNIT_NUMBER = re.compile(r"\b(engine|medic|ladder|tanker|truck|rescue|squad|ambulance|brush|tower|utility|"
                         r"station|company)\s+(\d{2})(\d{2})?\b(?![\d:-])", re.IGNORECASE)
WRITTEN_TIME = re.compile(r"(?<![\d:])([01]\d|2[0-3]):([0-5]\d)\b(?!:)")

AGENCY_WORDS = {
    "police": ("police", " pd", "pd ", "law", "sheriff"),
    "fire": ("fire", "fd"),
    "ems": ("ems", "medic", "ambulance", "rescue"),
}


def spoken_number(text: str) -> int | None:
    text = text.lower().replace("-", " ").strip()
    if text.isdigit():
        return int(text)
    parts = text.split()
    if len(parts) == 1:
        return ONES.get(parts[0], TENS.get(parts[0]))
    if len(parts) == 2 and parts[0] in TENS and parts[1] in ONES and ONES[parts[1]] < 10:
        return TENS[parts[0]] + ONES[parts[1]]
    return None


@dataclass
class Entry:
    type: str
    code: str
    meaning: str
    applies_to: str
    prompt: bool


@dataclass
class SuffixRange:
    low: int
    high: int
    meaning: str


def normalize_times(text: str, call_time: datetime | None, window_minutes: int) -> str:
    """Rewrites a four-digit 24-hour time ("1346", "13 46") as "13:46" when it is within
    window_minutes of when the call happened, which is how dispatch reads out the time.
    Numbers after unit words (Engine 1346) or before street words (1346 Main St) are left alone."""
    if not text or call_time is None or window_minutes <= 0:
        return text
    now = call_time.hour * 60 + call_time.minute

    def replace(match):
        hour, minute = int(match.group(1)), int(match.group(2))
        diff = abs(hour * 60 + minute - now)
        if min(diff, 24 * 60 - diff) > window_minutes:
            return match.group(0)
        if UNIT_WORD_BEFORE.search(text[:match.start()]) or STREET_WORD_AFTER.match(text[match.end():]):
            return match.group(0)
        return f"{hour:02d}:{minute:02d}"

    return SPOKEN_TIME.sub(replace, text)


def normalize_dispatch_time(text: str, call_time: datetime | None, window_minutes: int, ten_codes=()) -> str:
    """Rewrites the time dispatch reads at the end of a call ("..., Ops 1, 14-16.") as "14:16" when it
    is within window_minutes of the call. A glued channel number is split off ("Ops 113-30" becomes
    "Ops 1, 13:30")."""
    if not text or call_time is None or window_minutes <= 0:
        return text
    match = TRAILING_TIME.search(text)
    if not match:
        return text
    token = match.group(1)
    groups = list(re.finditer(r"\d+", token))
    now = call_time.hour * 60 + call_time.minute
    # Try the last one, two or three number groups: "1416", "14-16", "1, 14, 11"
    for count in (1, 2, 3):
        if count > len(groups):
            break
        first = groups[-count]
        digits = "".join(g.group(0) for g in groups[-count:])
        if len(digits) not in (4, 5) or len(groups[-1].group(0)) not in (2, 4, 5) or \
                (count > 1 and len(groups[-1].group(0)) != 2):
            continue
        prefix, hour, minute = digits[:-4], int(digits[-4:-2]), int(digits[-2:])
        if hour > 23 or minute > 59:
            continue
        before = text[:match.start(1) + first.start()]
        dispatch_word = DISPATCH_WORD_BEFORE.search(before)
        # A glued channel number only follows a channel word; "10-28" alone may be the 10-code.
        if prefix and not dispatch_word:
            continue
        if not prefix and digits[:2] == "10" and f"10-{minute}" in ten_codes and not dispatch_word:
            continue
        # Groups before this one must end where a number list would ("2000, 14-16" keeps the 2000)
        if before and (before[-1].isdigit() or before[-1] == "-"):
            continue
        diff = abs(now - (hour * 60 + minute))
        if min(diff, 24 * 60 - diff) > window_minutes:
            continue
        written = f"{hour:02d}:{minute:02d}"
        if prefix:
            written = f"{prefix}, {written}"
        return before + written + text[match.start(1) + groups[-1].end():]
    return text


def twelve_hour(hour: int, minute: int) -> str:
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


# A 24-hour time as normalize writes it ("13:46"), not already followed by AM or PM.
TWENTY_FOUR_HOUR = re.compile(r"(?<![\d:])([01]\d|2[0-3]):([0-5]\d)(?![\d:])(?!\s*[ap]\.?m\b)", re.IGNORECASE)


def to_twelve_hour(text: str) -> str:
    """"Ops 1, 13:41" -> "Ops 1, 1:41 PM"."""
    return TWENTY_FOUR_HOUR.sub(lambda m: twelve_hour(int(m.group(1)), int(m.group(2))), text or "")


class CodeBook:
    def __init__(self, path: Path | None, time_window_minutes: int = 3):
        self.path = path
        self.time_window_minutes = time_window_minutes
        self._lock = threading.Lock()
        self._mtime = None
        self._codes: dict[str, list[Entry]] = {}
        self._stations: dict[str, str] = {}
        self._suffixes: list[SuffixRange] = []
        self._police: dict[str, str] = {}
        self._words: dict[str, Entry] = {}
        self._word_pattern: re.Pattern | None = None
        self._prompt: list[str] = []
        self.reload_if_changed()

    def reload_if_changed(self):
        try:
            mtime = self.path.stat().st_mtime if self.path else None
        except FileNotFoundError:
            mtime = None
        with self._lock:
            if mtime == self._mtime:
                return
            self._mtime = mtime
            self._load()

    def _load(self):
        codes, stations, suffixes, prompt, police, words = {}, {}, [], [], {}, {}
        for row in self._read_rows():
            entry = Entry(
                type=(row.get("type") or "").strip().lower(),
                code=(row.get("code") or "").strip(),
                meaning=(row.get("meaning") or "").strip(),
                applies_to=(row.get("applies_to") or "all").strip().lower() or "all",
                prompt=(row.get("prompt") or "").strip().lower() in ("yes", "y", "true", "1"),
            )
            if not entry.code or not entry.meaning:
                continue
            if entry.type in ("ten-code", "signal"):
                key = _code_key(entry.code)
                if key:
                    codes.setdefault(key, []).append(entry)
                    if entry.prompt and key not in prompt:
                        prompt.append(key)
            elif entry.type == "station":
                stations[entry.code.zfill(2)] = entry.meaning
            elif entry.type == "police-esn":
                police[entry.code.zfill(2)] = entry.meaning
            elif entry.type in ("call-type", "acronym"):
                # A call type wins over an acronym with the same letters
                if entry.type == "call-type" or entry.code not in words:
                    words[entry.code] = entry
            elif entry.type == "unit-suffix":
                low, _, high = entry.code.partition("-")
                if low.isdigit() and (not high or high.isdigit()):
                    suffixes.append(SuffixRange(int(low), int(high or low), entry.meaning))
        self._codes, self._stations, self._suffixes, self._prompt = codes, stations, suffixes, prompt
        self._police = police
        self._words = words
        self._word_pattern = re.compile(r"(?<![\w-])(" + "|".join(
            re.escape(w) for w in sorted(words, key=len, reverse=True)) + r")(?![\w-])") if words else None
        log.info("Loaded %d codes, %d stations, %d unit types, %d police departments, %d call types and "
                 "acronyms from %s", len(codes), len(stations), len(suffixes), len(police), len(words), self.path)

    def _read_rows(self) -> list[dict]:
        if self._mtime is None:
            return []
        try:
            raw = self.path.read_bytes()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("cp1252", errors="replace")  # Excel's "CSV" on Windows
            return list(csv.DictReader(io.StringIO(content, newline="")))
        except (OSError, csv.Error) as e:
            log.error("Could not read %s: %s", self.path, e)
            return []

    def prompt_terms(self) -> list[str]:
        self.reload_if_changed()
        return list(self._prompt)

    def normalize(self, text: str, call_time: datetime | None = None) -> str:
        """Rewrites spoken codes ("ten four", "10 23", "signal thirty three") into their written form,
        but only for codes that are in the list, and spoken dispatch times into HH:MM."""
        self.reload_if_changed()
        codes = self._codes

        def ten(match):
            n = spoken_number(match.group(1))
            key = f"10-{n}" if n is not None else None
            return key if key in codes else match.group(0)

        def signal(match):
            n = spoken_number(match.group(1))
            key = f"Signal {n}" if n is not None else None
            return key if key in codes else match.group(0)

        text = SPOKEN_TEN_CODE.sub(ten, text)
        text = SPOKEN_SIGNAL.sub(signal, text)
        # After the codes, so "10 23" said at 10:23 still becomes the code 10-23.
        text = normalize_times(text, call_time, self.time_window_minutes)
        return normalize_dispatch_time(text, call_time, self.time_window_minutes, codes)

    def annotate(self, text: str | None, *context: str | None, call_time: float | None = None) -> list[dict]:
        """Labels for the codes and call signs found in a transcript, in order of appearance. With the
        call's time, a dispatch time written the old way ("14-16") is labelled too."""
        if not text:
            return []
        self.reload_if_changed()
        if call_time:
            text = normalize_dispatch_time(text, datetime.fromtimestamp(call_time), self.time_window_minutes,
                                           self._codes)
        agency = _agency(" ".join(c for c in context if c))
        found, seen = [], set()

        for match in TEN_CODE.finditer(text):
            self._add_code(found, seen, match.start(), f"10-{int(match.group(1))}", match.group(0), agency)
        for match in SIGNAL.finditer(text):
            self._add_code(found, seen, match.start(), f"Signal {int(match.group(1))}", match.group(0), agency)

        used = []
        for match in WRITTEN_TIME.finditer(text):
            used.append(match.span())
            if match.group(0) not in seen:
                seen.add(match.group(0))
                found.append((match.start(), {"code": match.group(0), "kind": "time",
                                              "meaning": twelve_hour(int(match.group(1)), int(match.group(2)))}))

        if agency == "police" and self._police:
            for match in POLICE_CAR.finditer(text):
                department = self._police.get(match.group(1))
                car = int(match.group(2))
                if len(match.group(0)) == 3 and ADDRESS_WORD_BEFORE.search(text[:match.start()]):
                    continue
                if department and car and match.group(0) not in seen and not _inside(match, used):
                    seen.add(match.group(0))
                    found.append((match.start(), {"code": match.group(0), "meaning": f"{department}, car {car}",
                                                  "kind": "unit"}))
        elif self._stations and self._suffixes:
            for pattern in (CALL_SIGN, DASHED_CALL_SIGN):
                for match in pattern.finditer(text):
                    if _inside(match, used) or (pattern is DASHED_CALL_SIGN and match.group(1) == "10"):
                        continue
                    station = self._stations.get(match.group(1))
                    unit = next((s.meaning for s in self._suffixes if s.low <= int(match.group(2)) <= s.high), None)
                    if station and unit and match.group(0) not in seen:
                        seen.add(match.group(0))
                        found.append((match.start(), {"code": match.group(0), "meaning": f"{station}, {unit}",
                                                      "kind": "unit"}))

        if self._stations:
            for match in STATION_REF.finditer(text):
                station = self._stations.get(match.group(1) or match.group(2))
                if station:
                    used.append(match.span())
                if station and match.group(0).lower() not in seen:
                    seen.add(match.group(0).lower())
                    found.append((match.start(), {"code": match.group(0), "meaning": station, "kind": "unit"}))

        if self._word_pattern:
            for match in self._word_pattern.finditer(text):
                if match.group(1) not in seen and not _inside(match, used):
                    seen.add(match.group(1))
                    found.append((match.start(), {"code": match.group(1), "meaning": self._words[match.group(1)].meaning,
                                                  "kind": "code"}))

        return [item for _, item in sorted(found, key=lambda pair: pair[0])]

    def unknown_units(self, text: str | None, *context: str | None) -> list[str]:
        """Fire and EMS units in a finished transcript whose station isn't in the list ("Engine 9711"),
        or whose last two digits are no unit type, for the self-check. Police talkgroups are skipped."""
        if not text:
            return []
        self.reload_if_changed()
        if not self._stations or _agency(" ".join(c for c in context if c)) == "police":
            return []
        found = []
        for match in UNIT_NUMBER.finditer(text):
            station, unit = match.group(2), match.group(3)
            if station not in self._stations:
                found.append(match.group(0))
            elif unit and self._suffixes and not any(s.low <= int(unit) <= s.high for s in self._suffixes):
                found.append(match.group(0))
        return found

    def _add_code(self, found, seen, position, key, shown, agency):
        entries = self._codes.get(key)
        if not entries or key in seen:
            return
        seen.add(key)
        entry = _pick(entries, agency)
        found.append((position, {"code": shown, "meaning": entry.meaning, "kind": "code"}))


def _inside(match, spans) -> bool:
    return any(start < match.end() and match.start() < end for start, end in spans)


def _code_key(code: str) -> str | None:
    match = re.fullmatch(r"10-(\d{1,2})", code)
    if match:
        return f"10-{int(match.group(1))}"
    match = re.fullmatch(r"signal\s*(\d{1,3})", code, re.IGNORECASE)
    if match:
        return f"Signal {int(match.group(1))}"
    return None


def _agency(context: str) -> str | None:
    context = f" {context.lower()} "
    for agency, words in AGENCY_WORDS.items():
        if any(word in context for word in words):
            return agency
    return None


def _pick(entries: list[Entry], agency: str | None) -> Entry:
    if agency:
        for entry in entries:
            if agency in entry.applies_to.split("-"):
                return entry
    for entry in entries:
        if entry.applies_to == "all":
            return entry
    return entries[0]
