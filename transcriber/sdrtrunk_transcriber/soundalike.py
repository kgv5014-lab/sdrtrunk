"""Smart address lookup: finds the real street behind one Whisper spelled by sound.

Dispatch reads an address like "2545 Schoenersville Road, Cross of Westgate Drive, Bethlehem City
Communications", and Whisper writes what it heard: "2545, Shanersville, Railroad, Cross of Westgate Drive".
The spelling check in address_fix.py only knows streets spelled almost right with the same first letter,
so "Grimminsburg Avenue" (Freemansburg) or "Bass Pike" (Bath Pike) went unplaced.

Here every local street is compared with what was heard by spelling and by sound (a key that keeps the
consonants and folds the ones radio audio blurs: d/t, b/p, g/k, v/f, s/z, sh/ch). A sound-alike
street is only used when what else dispatch said agrees with it:

    house number   the Census ranges (address-ranges.json) have that number on the street
    cross street   "Cross of Westgate Drive": the street meets that one
    town           "Hanover Township Communications", "in Easton": the street is in that town

A guess needs enough of these for how close it sounds, and has to beat every other street clearly.
Otherwise nothing changes.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .addresses import DIRECTIONS, SUFFIXES, UNIT_WORDS, WORDS, split_name, words
from .places import GENERIC_WORDS, RADIO_WORDS

# Street words Whisper writes for the one dispatch said: "Orvieto Corrie" (Court), "Ridgewood Roof" (Road).
MISHEARD_SUFFIXES = {
    "cork": "ct", "cort": "ct", "corrie": "ct", "corey": "ct", "core": "ct", "courts": "ct", "quart": "ct",
    "roof": "rd", "rode": "rd", "rowed": "rd",
    "drove": "dr", "drives": "dr", "lain": "ln", "lanes": "ln",
    "avenues": "ave", "avenida": "ave", "streets": "st", "treat": "st", "trails": "trl", "bike": "pike",
    "psych": "pike", "railroad": "rd", "riddler": "rd", "spike": "pike", "boulevards": "blvd", "circles": "cir",
}
# Words that start the cross street in a dispatch: "Cross of", "Crossroads", "Cross-Up", "Crosses".
CROSS = re.compile(r"\b(?:a?cross(?:es|ed|roads?|-?up|-?of|ing)?|crest|crossover)\b(?:[\s,-]+(?:of|is|with|street|streets|at|the))*[\s,-]*",
                   re.IGNORECASE)
# What a dispatch says right after the town: "Bethlehem Township Communications".
COMMS = re.compile(r"\b(?:communications?|commutations|comms?)\b", re.IGNORECASE)
WORD = re.compile(r"[A-Za-z][A-Za-z'’.-]*")
# A street name word: letters only, so "93-year-old" or "10:15" end it.
NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*\.?")
NOT_NAMES = RADIO_WORDS | GENERIC_WORDS | UNIT_WORDS | {
    "committed", "alert", "dispatch", "respond", "responding", "please", "hours", "minutes", "year", "old",
    "male", "female", "victim", "fall", "call", "calls", "caller", "medical", "alarm", "fire", "als", "bls",
    "mva", "room", "apartment", "suite", "floor", "building", "number", "unknown", "cross", "copy", "your",
    "youre", "were", "going", "out", "up", "in", "over", "will", "has", "have", "this", "there", "here",
    "more", "full", "good", "real", "last", "first", "its", "it", "im", "ill", "about", "just", "end",
    "block", "same", "area", "address", "location", "side", "way", "home", "house", "town", "city",
}

# How close a heard name must sound to a street, by how much else agrees (see SmartLookup._pick).
# House number and town are 1 each (most streets have most numbers, so a number alone says little);
# the cross street is 3, since few streets that sound alike also meet the heard cross street.
NEED = {1: 0.85, 2: 0.82, 3: 0.68, 4: 0.65, 5: 0.62}
# When the two sound the same ("Makeda" and Macada, "Cole" and Coal), less else has to agree.
SAME_SOUND = 0.9
NEED_SAME_SOUND = {1: 0.78, 2: 0.74}
# The best street has to beat the next one by this much.
MARGIN = 0.05


def sound_key(text: str) -> str:
    """"Freemansburg" -> "frmnsprk", "Grimminsburg" -> "krmnsprk": consonants as they sound, with the
    pairs radio audio blurs folded together."""
    w = re.sub(r"[^a-z]", "", text.lower())
    if not w:
        return ""
    w = w.replace("x", "ks")
    # Upper case marks a sound already worked out, so later rules leave it alone.
    for a, b in (("tch", "X"), ("sch", "SK"), ("ph", "F"), ("ck", "K"), ("dg", "J"), ("th", "0"),
                 ("sh", "X"), ("ch", "X"), ("gh", ""), ("wr", "R"), ("kn", "N"), ("qu", "KW")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w)
    w = re.sub(r"g(?=[eiy])", "j", w)
    w = w.lower().translate(str.maketrans("cqdbgvz", "kktpkfs"))
    first, rest = w[0], re.sub(r"[aeiouhwy]", "", w[1:])
    first = "a" if first in "aeiou" else first
    out = first
    for ch in rest:
        if ch != out[-1]:
            out += ch
    return out


def similarity(heard: str, name: str, heard_key: str | None = None, name_key: str | None = None) -> float:
    """0-1: how alike two street names are, by spelling and by sound."""
    heard, name = heard.replace(" ", ""), name.replace(" ", "")
    spelled = SequenceMatcher(None, heard, name).ratio()
    hk = sound_key(heard) if heard_key is None else heard_key
    nk = sound_key(name) if name_key is None else name_key
    if not hk or not nk:
        return spelled
    sound = SequenceMatcher(None, hk, nk).ratio()
    # Sound alone says too little about short names ("ps" for Bass, "p0" for Bath), so it counts half.
    return max(spelled, (spelled + sound) / 2, sound if len(nk) >= 5 else 0.0)


@dataclass
class Heard:
    """An address as Whisper wrote it, split into its parts."""
    start: int          # where the street name starts in the text
    end: int            # where the street name (with its street word) ends
    core: str           # the name, normalized: "makeda"
    direction: str      # "w" for "West Makeda Road", else ""
    suffix: str         # "rd", or "" when no street word was heard
    cross: str          # the cross street as heard, "westgate dr", or ""


@dataclass
class Guess:
    name: str           # the street as the street list writes it
    score: float
    sound: float        # how alike the two sound alone
    agrees: int
    reasons: list


class SmartLookup:
    def __init__(self, gazetteer, book):
        self.gazetteer = gazetteer
        self.book = book
        self._for = None
        self._cores: dict[str, list] = {}
        self._keys: dict[str, str] = {}

    # ---- the street list, by core name

    def _index(self):
        if self._for is self.gazetteer.streets:
            return self._cores
        cores: dict[str, list] = {}
        for street in self.gazetteer.streets:
            core, suffix = split_name(street.name)
            if core and core[0] in DIRECTIONS and len(core) > 1:
                core = core[1:]
            if not core or core[0] == "route" or all(c.isdigit() or c[:1].isdigit() for c in core):
                continue
            cores.setdefault(" ".join(core), []).append(street)
        self._keys = {core: sound_key(core) for core in cores}
        self._cores, self._for = cores, self.gazetteer.streets
        return cores

    # ---- reading the address out of the text

    def heard_after(self, text: str, pos: int) -> Heard | None:
        """The street words right after a house number ending at `pos`: "1745, West Makeda Road"."""
        m = re.match(r"\s*,?\s*", text[pos:])
        at = pos + m.end()
        names, last_end = [], at
        for word in NAME_WORD.finditer(text, at):
            between = text[last_end:word.start()]
            if names and (between.strip(" ") not in ("", ",") or "," in between and not self._suffix(word.group(0))):
                break
            if not names and word.start() != at:
                break
            names.append(word)
            last_end = word.end()
            if self._suffix(word.group(0)) and len(names) > 1:
                break
            if len(names) == 5:
                break
        if not names:
            return None
        after = text[last_end:last_end + 3].lstrip(" ")
        if after[:1].isdigit():
            return None     # "North 6th Street": a numbered street, which address_fix.py handles
        raw = [n.group(0).rstrip(".").lower().replace("’", "'").replace("'", "") for n in names]
        suffix = ""
        if len(raw) > 1 and self._suffix(raw[-1]):
            suffix = self._suffix(raw[-1])
        else:
            # No street word: only the first couple of words can be the name.
            names, raw = names[:2], raw[:2]
            while len(raw) > 1 and raw[-1] in NOT_NAMES:
                names, raw = names[:-1], raw[:-1]
        core_words = raw[:-1] if suffix else raw
        direction = ""
        if len(core_words) > 1 and WORDS.get(core_words[0], core_words[0]) in DIRECTIONS:
            direction, core_words = WORDS.get(core_words[0], core_words[0]), core_words[1:]
        core = " ".join(core_words)
        if not core or any(w in NOT_NAMES or self._suffix(w) for w in core_words) or len(core.replace(" ", "")) < 4:
            return None
        cross = ""
        rest = text[names[-1].end():names[-1].end() + 80]
        c = re.match(r"[\s,]*", rest)
        c2 = CROSS.match(rest, c.end())
        if c2 and c2.end() > c.end():
            tail = re.match(r"[A-Za-z][A-Za-z' .-]*", rest[c2.end():])
            if tail:
                cross = " ".join(words(tail.group(0)))
        return Heard(names[0].start(), names[-1].end(), core, direction, suffix, cross)

    @staticmethod
    def _suffix(word: str) -> str:
        w = word.rstrip(".").lower()
        folded = WORDS.get(w, w)
        if folded in SUFFIXES:
            return folded
        return MISHEARD_SUFFIXES.get(w, "")

    def towns_named(self, text: str) -> set[str]:
        """Towns dispatch named: "Hanover Township Communications", "Bethlehem City Communications",
        "in Easton"."""
        towns = set(self.gazetteer.towns_in(words(text)))
        for m in COMMS.finditer(text):
            before = words(text[max(0, m.start() - 40):m.start()])
            for length in (3, 2, 1):
                key = tuple(before[-length:])
                if len(key) == length and key in self.gazetteer.towns:
                    keys = self.gazetteer.towns[key]
                    if key[-1] == "twp":
                        towns |= keys
                    else:
                        towns |= {k for k in keys if not k.endswith(" twp")} or keys
                    break
        return towns

    # ---- finding the street

    def lookup(self, heard: Heard, number: int | None, block: bool, towns: set[str]) -> Guess | None:
        """The one street the heard address must be, or None when it isn't clear."""
        cores = self._index()
        hk = heard.core
        if hk in cores and (not heard.suffix or any(split_name(s.name)[1] == heard.suffix for s in cores[hk])):
            return None     # a real street, said right: address_fix.py checks its house number
        crossing = self._cross_streets(heard.cross) if heard.cross else []
        guesses: dict[str, Guess] = {}
        heard_key = sound_key(hk)
        for core, streets in cores.items():
            if abs(len(core) - len(hk)) > max(3, len(hk) // 2):
                continue
            score = similarity(hk, core, heard_key, self._keys[core])
            if score < min(NEED.values()):
                continue
            sound = SequenceMatcher(None, heard_key, self._keys[core]).ratio()
            for street in streets:
                street_core, suffix = split_name(street.name)
                if heard.suffix and suffix != heard.suffix:
                    continue        # "Franklin Square" is not Franklin Lane, "Oak Hills Drive" not Oak Hills
                this_score = score
                agrees, reasons = 0, []
                if number is not None and self.book is not None:
                    has_numbers = bool(self.book.sides(street.name))
                    if has_numbers:
                        if self.book.has(street.name, number, {street.town_key} if street.town_key else None, block) \
                                or self.book.has(street.name, number, None, block):
                            agrees += 1
                            reasons.append("house number")
                        else:
                            continue    # the street is real but has no such house: not this one
                if towns and street.town_key in towns:
                    agrees += 1
                    reasons.append("town")
                if crossing and self.gazetteer.meeting_points([street], crossing, towns)[0]:
                    agrees += 3
                    reasons.append("cross street")
                if heard.direction and street_core and street_core[0] == heard.direction:
                    this_score = min(1.0, this_score + 0.02)
                guess = guesses.get(street.name)
                if guess is None or (agrees, this_score) > (guess.agrees, guess.score):
                    guesses[street.name] = Guess(street.name, round(this_score, 3), round(sound, 3), agrees, reasons)
        return self._pick(list(guesses.values()), heard)

    def _cross_streets(self, cross: str) -> list:
        """The streets the heard cross street could be: by name, else by sound."""
        w = cross.split()
        mentions = self.gazetteer.mentions(w)
        if mentions and mentions[0].start == 0:
            return mentions[0].streets
        core = [x for x in w if x not in SUFFIXES]
        if core and core[0] in DIRECTIONS and len(core) > 1:
            core = core[1:]
        heard = " ".join(core[:3])
        if len(heard) < 4:
            return []
        out = []
        for name, streets in self._index().items():
            if abs(len(name) - len(heard)) <= 3 and similarity(heard, name) >= 0.8:
                out += streets
        return out

    @staticmethod
    def _pick(guesses: list[Guess], heard: Heard) -> Guess | None:
        # Something has to agree, and without a street word ("2545, Shanersville") it has to be the
        # cross street: otherwise any word after any number could become a street.
        def level(g):
            if not g.agrees:
                return -1
            need = NEED[min(g.agrees, 5)]
            if g.sound >= SAME_SOUND and g.agrees in NEED_SAME_SOUND:
                need = NEED_SAME_SOUND[g.agrees]
            return g.score - need

        fit = [g for g in guesses if level(g) >= 0 and (heard.suffix or "cross street" in g.reasons)]
        if not fit:
            return None
        fit.sort(key=lambda g: (g.agrees, g.score), reverse=True)
        best = fit[0]
        # Something else as likely: a street agreeing as much and sounding about as close.
        for other in fit[1:]:
            if other.agrees >= best.agrees and best.score - other.score < MARGIN:
                return None
        # One agreeing a lot less but sounding much closer leaves it unclear too.
        for other in guesses:
            if other is not best and other.score - best.score >= 0.1 and other.agrees >= best.agrees - 1 \
                    and other.agrees and level(other) >= -0.05:
                return None
        return best
