"""The aviation channels: airport and helicopter radio kept on a page of their own.

Calls on the [aviation] talkgroups (ABE Approach, Tower, the 121.5 emergency frequency, helicopters on
123.025) are left out of the main call log, the incident map, the unit board and the summaries. Calls
from a medevac helicopter are the exception: they stay in the main call log too.

Each call is matched to the aircraft that spoke or was spoken to, by the call sign heard in it
("American 1234", "November 812 Lima Foxtrot", "Cessna 2LF") against the aircraft the ADS-B receiver saw
around that time (aircraft.py), so the aviation page can show which plane it was and where.

Anyone talking on 121.5 sends a phone alert, since that frequency is normally silent.
"""

import json
import logging
import re
import time

from .aircraft import medevac_callsign
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

# What the air column of a call holds.
AVIATION, MEDEVAC = "aviation", "medevac"
EMERGENCY_HZ = 121_500_000

DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "tree": "3", "four": "4", "fower": "4",
          "five": "5", "fife": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
TEENS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
         "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19"}
TENS = {"twenty": "2", "thirty": "3", "forty": "4", "fifty": "5", "sixty": "6", "seventy": "7", "eighty": "8",
        "ninety": "9"}
PHONETIC = {"alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "foxtrot": "F",
            "golf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J", "kilo": "K", "lima": "L",
            "mike": "M", "november": "N", "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S",
            "tango": "T", "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X", "yankee": "Y", "zulu": "Z"}
# Single letters Whisper writes out ("8 1 2 L F"), only inside a call sign already started.
LETTERS = set("bcdefghjklmnopqrstuvwxyz")

# Airline radio names and the ICAO code in front of the flight number in ADS-B.
AIRLINES = {
    "american": "AAL", "delta": "DAL", "united": "UAL", "southwest": "SWA", "jetblue": "JBU", "allegiant": "AAY",
    "frontier": "FFT", "frontier flight": "FFT", "spirit": "NKS", "spirit wings": "NKS", "alaska": "ASA",
    "sun country": "SCX", "moxy": "MXY", "breeze": "MXY", "fedex": "FDX", "fed ex": "FDX", "ups": "UPS",
    "endeavor": "EDV", "envoy": "ENY", "piedmont": "PDT", "blue streak": "JIA", "brickyard": "RPA",
    "skywest": "SKW", "sky west": "SKW", "air shuttle": "ASH", "wisconsin": "AWI", "lindbergh": "GJS",
    "commutair": "UCA", "commuter": "UCA", "execjet": "EJA", "exec jet": "EJA", "flexjet": "LXJ",
    "flex jet": "LXJ", "jet linx": "JTL", "giant": "GTI", "abex": "ABX", "air transport": "ATN",
    "connie": "CKS", "amerijet": "AJT", "horizon": "QXE", "hawaiian": "HAL", "air canada": "ACA",
    "jazz": "JZA", "westjet": "WJA", "reach": "RCH", "mountain": "MTN", "wiggins": "WIG", "polar": "PAC",
    "southern air": "SOO", "airshare": "XSR", "air share": "XSR", "skyline": "SKY", "cactus": "AWE",
    "speedbird": "BAW", "lufthansa": "DLH", "air france": "AFR", "kalitta": "CKS", "atlas": "GTI",
}
# Words said before a tail number: "Cessna 2LF", "helicopter 812LF".
TYPE_WORDS = {"cessna", "piper", "cherokee", "archer", "warrior", "arrow", "seneca", "skyhawk", "skylane",
              "cirrus", "mooney", "bonanza", "baron", "beech", "beechcraft", "king", "citation", "lear",
              "learjet", "diamond", "diamondstar", "challenger", "gulfstream", "falcon", "pilatus", "phenom",
              "embraer", "hawker", "comanche", "navajo", "aztec", "cub", "grumman", "tiger", "sling",
              "helicopter", "helo", "robinson", "bell", "airbus", "eurocopter", "sikorsky", "agusta",
              "experimental", "lifeguard", "medevac", "november"}

def _named(call: dict, names: list) -> bool:
    """Whether the call's talkgroup number, name or group is one of these (case doesn't matter)."""
    wanted = {str(n).strip().lower() for n in names if str(n).strip()}
    have = {str(call.get(k) or "").strip().lower() for k in ("talkgroup", "talkgroup_label", "talkgroup_group")}
    return bool(wanted & (have - {""}))


def is_aviation(call: dict, config: Config) -> bool:
    """On one of the air band talkgroups ([air_transcripts] talkgroups) or on 121.5."""
    return config.aviation.enabled and (_named(call, config.air_transcripts.talkgroups)
                                        or is_emergency(call, config.aviation))


def is_emergency(call: dict, settings) -> bool:
    """On 121.5: a talkgroup in emergency_talkgroups, or a conventional channel on that frequency."""
    if not settings.enabled:
        return False
    frequency = call.get("frequency") or 0
    return _named(call, settings.emergency_talkgroups) or abs(frequency - EMERGENCY_HZ) < 5000


def is_medevac_call(text: str | None) -> bool:
    return bool(medevac_callsign(text or ""))


# ---- call signs

def _tokens(text: str) -> list[str]:
    text = (text or "").lower().replace("x-ray", "xray")
    # Punctuation is kept as "," so "Allegiant 1234, 6 mile final" stays two numbers.
    return ["," if t[0] in ",.;:?!" else t for t in re.findall(r"[a-z0-9]+|[,.;:?!]", text)]


def runs(text: str) -> list[tuple[list[str], str]]:
    """Each run of digits and letters spoken one by one, with the words said just before it.

    "American twelve thirty four" -> (["american"], "1234"); "November 8-1-2 Lima Foxtrot" -> ([...], "N812LF").
    """
    found, words, chars, before = [], [], "", []
    tokens = _tokens(text)
    i = 0
    while i < len(tokens):
        token, piece = tokens[i], None
        if token.isdigit() or (re.fullmatch(r"[a-z0-9]+", token) and re.search(r"\d", token)):
            piece = token.upper()
        elif token in DIGITS:
            piece = DIGITS[token]
        elif token in TEENS:
            piece = TEENS[token]
        elif token in TENS:
            piece = TENS[token]
            if i + 1 < len(tokens) and tokens[i + 1] in DIGITS and tokens[i + 1] != "zero":
                i += 1
                piece += DIGITS[tokens[i]]
            else:
                piece += "0"
        elif token in ("hundred", "thousand") and chars:
            piece = "00" if token == "hundred" else "000"
        elif token in PHONETIC and (chars or token == "november" or _starts_tail(tokens, i)):
            piece = PHONETIC[token]
        elif token in LETTERS and chars:
            piece = token.upper()
        if piece is not None and chars and len(piece) > 1 and piece.isdigit() and chars[-1].isalpha():
            # A new number after letters starts a new run: "5432 Kilo 3500" is a call sign and an altitude.
            found.append((before, chars))
            chars = ""
            words.append(",")
        if piece is None:
            if chars:
                found.append((before, chars))
                chars = ""
            words.append(token)
        else:
            if not chars:
                # The words just before, back to the last comma.
                before = words[-2:]
                if "," in before:
                    before = before[len(before) - before[::-1].index(","):]
            chars += piece
        i += 1
    if chars:
        found.append((before, chars))
    return found


def _starts_tail(tokens: list[str], i: int) -> bool:
    """A phonetic word that starts a call sign rather than meaning a taxiway or ATIS letter: followed by
    another phonetic word or a digit ("Lima Foxtrot", "Charlie 1")."""
    after = tokens[i + 1] if i + 1 < len(tokens) else ""
    return after in PHONETIC or after in DIGITS or after.isdigit()


def spoken_callsigns(text: str) -> list[dict]:
    """The call signs said in a transcript, in order. Each is {"said", "flight"} for an airline or other
    named call sign ("AAL1234"), {"said", "tail"} for a full N-number, or {"said", "short", "cue"} for a
    run that may be the end of a tail number (cue: said after an aircraft type, like "Cessna 2LF")."""
    signs = []
    for before, chars in runs(text):
        named = " ".join(before)
        last = before[-1] if before else ""
        code = AIRLINES.get(named) or AIRLINES.get(last)
        if code and chars.isdigit():
            said = f"{(named if named in AIRLINES else last).title()} {chars}"
            signs.append({"said": said, "flight": code + chars.lstrip("0"), "words": before, "number": chars})
        elif re.fullmatch(r"N[1-9][0-9A-Z]{0,4}", chars):
            signs.append({"said": chars, "tail": chars})
        else:
            signs.append({"said": " ".join(filter(None, (last, chars))), "short": chars, "words": before,
                          "cue": last in TYPE_WORDS or (len(before) > 1 and before[-2] in TYPE_WORDS)})
    return signs


def _flight_parts(flight: str) -> tuple[str, str]:
    match = re.fullmatch(r"([A-Z]+)(\d[0-9A-Z]*)", flight)
    return (match.group(1), match.group(2)) if match else ("", "")


def _score(sign: dict, plane: dict) -> tuple[float, str]:
    """How well a call sign heard fits an aircraft: 3 sure, 2 likely, 0 no."""
    flight = (plane.get("flight") or "").strip().upper()
    reg = (plane.get("reg") or "").strip().upper()
    if "flight" in sign:
        letters, number = _flight_parts(flight)
        if flight and letters + number.lstrip("0") == sign["flight"]:
            return 3, f"said {sign['said']}"
        return 0, ""
    if "tail" in sign:
        if sign["tail"] in (reg, flight):
            return 3, f"said tail number {sign['tail']}"
        return 0, ""
    short = sign["short"]
    # A named call sign other than an airline's: flight LIFE12 said as "Life 12" or "Life Flight 12".
    letters, number = _flight_parts(flight)
    if letters and number == short and len(letters) >= 3:
        for i, word in enumerate(sign["words"]):
            if word.upper().startswith(letters[:4]) or (len(word) >= 3 and letters.startswith(word.upper())):
                return 3, "said " + " ".join(w.title() for w in sign["words"][i:] + [short])
    for tail in filter(None, {reg, flight if flight.startswith("N") else ""}):
        if tail[1:] == short and re.search(r"[A-Z]", short):
            return 2.5, f"said tail number {short}"
        if tail[1:] == short and sign["cue"]:
            return 2.5, f"said {sign['said']}"
        # The short form: aircraft type and the last three of the tail ("Cessna 2LF").
        if len(short) >= 3 and tail.endswith(short) and (sign["cue"] or re.search(r"[A-Z]", short)):
            return 2, f"said {sign['said']}"
    return 0, ""


def match_call(text: str | None, planes: list[dict]) -> list[dict]:
    """The aircraft whose call signs are heard in a transcript, in the order heard (on the radio the first
    is the one talking or being talked to). Each is the plane plus "how".

    A plane fits only on its own call sign: a call sign two planes fit equally well is left out.
    """
    found: dict[str, dict] = {}
    for order, sign in enumerate(spoken_callsigns(text or "")):
        scored = [(score, how, plane) for plane in planes for score, how in [_score(sign, plane)] if score]
        if not scored:
            continue
        best = max(s for s, _h, _p in scored)
        top = [(how, plane) for score, how, plane in scored if score == best]
        if len(top) > 1:
            continue
        how, plane = top[0]
        if plane["hex"] not in found or found[plane["hex"]]["score"] < best:
            found[plane["hex"]] = dict(plane, how=how, score=best, order=order)
    return sorted(found.values(), key=lambda p: p["order"])


def position_at(plane: dict, when: float) -> dict:
    """Where the plane was at the time of the call, from its timed trail: lat, lon, alt_ft, speed_kt."""
    trail = plane.get("track_points") or []
    if not trail:
        return {}
    point = min(trail, key=lambda p: abs(p[0] - when))
    return {"lat": point[1], "lon": point[2], "alt_ft": point[3], "speed_kt": point[4], "at": point[0]}


def describe(plane: dict, when: float) -> dict:
    """What the call log keeps about a matched aircraft."""
    shown = {key: plane.get(key) for key in ("hex", "flight", "reg", "type", "desc", "operator", "kind", "how")}
    shown["helicopter"] = bool(plane.get("helicopter"))
    shown.update(position_at(plane, when))
    return shown


# ---- the calls

class AviationLog:
    def __init__(self, config: Config, db: Database, aircraft=None, alerter=None):
        self.config = config
        self.settings = config.aviation
        self.db = db
        # AircraftWatch (aircraft.py), or None when ADS-B is off.
        self.aircraft = aircraft
        self.alerter = alerter

    def backfill(self) -> int:
        """Marks calls logged before this existed, so they leave the main call log too."""
        names = [str(n).strip().lower() for n in self.config.air_transcripts.talkgroups
                 + self.settings.emergency_talkgroups]
        return self.db.mark_air(names, EMERGENCY_HZ) if names else 0

    def air_of(self, call: dict) -> str | None:
        return AVIATION if is_aviation(call, self.config) else None

    def urgent(self, call: dict) -> bool:
        """121.5 calls go first in the transcription queue, so the alert isn't held up."""
        return is_emergency(call, self.settings)

    def call_done(self, call_id: int, alert: bool = True) -> str | None:
        """After a call is transcribed (or fixed by hand, alert=False): matches it to aircraft, marks medevac
        calls and alerts on 121.5. Returns the call's air value (None for a county call)."""
        call = self.db.get_call(call_id)
        if call is None or not is_aviation(call, self.config):
            return None
        planes = self.aircraft.recent_aircraft(call["call_time"], self.settings.match_minutes * 60) \
            if self.aircraft else []
        matched = [describe(p, call["call_time"]) for p in match_call(call["transcript"], planes)]
        if self.aircraft and alert:
            # A medevac call sign said on the air marks the helicopter that said it (aircraft.py).
            helicopter = next((p for p in matched if p["helicopter"]), None)
            try:
                self.aircraft.heard_on_radio(call, helicopter)
            except Exception:
                log.exception("Helicopters: call %s failed", call_id)
        medevac = is_medevac_call(call["transcript"]) or any(p["kind"] == MEDEVAC for p in matched)
        air = MEDEVAC if medevac else AVIATION
        self.db.update_call(call_id, air=air, aircraft=json.dumps(matched) if matched else None)
        if matched:
            log.info("Aviation call %d: %s", call_id, "; ".join(
                f"{p['flight'] or p['reg'] or p['hex']} ({p['how']})" for p in matched))
        if alert and is_emergency(call, self.settings):
            self._emergency(dict(call, air=air), matched)
        return air

    def _emergency(self, call: dict, matched: list[dict]):
        heard = (call.get("transcript") or "").strip()
        if not self.alerter or not self.settings.emergency_alert:
            return
        if not heard and not (self.settings.emergency_alert_no_words
                              and (call.get("duration_s") or 0) >= self.settings.emergency_min_s):
            return
        if time.time() - call["call_time"] > self.alerter.settings.max_age_minutes * 60:
            return
        if self.db.alerted(page_label="121.5", since=time.time() - self.settings.emergency_repeat_minutes * 60,
                           kind="aviation"):
            return
        who = matched[0]["flight"] or matched[0]["reg"] if matched else ""
        title = "121.5 emergency: " + (f"{who} talking" if who else "someone is talking" if heard else "sound, no words")
        if len(heard) > 240:
            heard = heard[:239] + "…"
        base = self.alerter.base_url
        self.alerter.send("aviation", title, heard or "Could be an emergency beacon (ELT); listen on the aviation page.",
                          tags=["airplane", "rotating_light"], priority=5,
                          click=f"{base}/aviation" if base else None, call_id=call["id"], page_label="121.5")


def tar1090_url(settings) -> str:
    """The receiver's own map, to open an aircraft on: [aviation] adsb_map_url, else worked out from the
    [aircraft] url (".../data/aircraft.json" -> ".../")."""
    url = settings.aviation.adsb_map_url.strip() or re.sub(r"data/aircraft\.json$", "", settings.aircraft.url)
    return url if url.endswith("/") else url + "/"
