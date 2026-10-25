"""Turns transcribed calls into possible incidents on a map.

For each transcribed call: find an address, intersection or landmark in the transcript (places.py),
put it on the map (offline from the downloaded road shapes, or with the online lookup for house
numbers), then join it to an incident already on the map nearby in the last while, or start a new one.
Runs on its own thread so slow lookups never hold up Whisper.
"""

import logging
import math
import queue
import re
import threading
import time

from .codes import AGENCY_WORDS
from .config import Config
from .db import Database
from .geocode import Geocoder
from .worker import load_vocabulary
from .addresses import Found, Gazetteer, words

log = logging.getLogger(__name__)

# What the call is about, from words in the transcript. First match wins.
NATURES = [
    (r"(structure|working|house|building|facility|apartment|dwelling|commercial|residential|kitchen|chimney) fire|"
     r"fully involved|heavy smoke|smoke showing|flames showing|fire in the", "Structure fire", "fire"),
    (r"vehicle fire|car fire|truck fire", "Vehicle fire", "fire"),
    (r"brush fire|grass fire|field fire|mulch fire|outside fire|dumpster fire", "Outside fire", "fire"),
    (r"odor of gas|gas leak|smell of gas|natural gas", "Gas leak", "fire"),
    (r"carbon monoxide|\bco alarm|co detector", "CO alarm", "fire"),
    (r"fire alarm|automatic alarm|alarm activation|general alarm|smoke detector|alarm sounding",
     "Fire alarm", "fire"),
    (r"wires? down|tree down|pole down|transformer", "Wires or tree down", "fire"),
    (r"\bodor\b|smells? (really |very )?bad|burning smell|smell of (burning|smoke|plastic)|melted",
     "Odor investigation", "fire"),
    (r"water rescue|swift water", "Water rescue", "fire"),
    # MVA: motor vehicle accident. Whisper writes it "MVA", "M.V.A.", "M V A" or "MBI".
    (r"\bm\.?\s?v\.?\s?[ac]\b\.?|\bmbi\b|motor vehicle|vehicle accident|car accident|auto accident|traffic accident|"
     r"\bcrash\b|accident|rollover|roll over|vehicle versus|car versus|pedestrian struck|hit and run|"
     r"vehicle into", "MVA", None),
    (r"dia[bp]etic|blood sugar", "Diabetic emergency", "ems"),
    (r"cardiac arrest|\bcpr\b|not breathing|code blue", "Cardiac arrest", "ems"),
    (r"unresponsive|unconscious", "Unresponsive", "ems"),
    (r"chest pains?", "Chest pain", "ems"),
    (r"difficulty breathing|trouble breathing|short(ness)? of breath|respiratory", "Breathing problem", "ems"),
    (r"overdose|\bod\b|narcan", "Overdose", "ems"),
    (r"stroke|seizure|syncope|passed out|fainted", "Medical", "ems"),
    (r"li[fs]t assist", "Lift assist", "ems"),
    (r"\bfalls?\b|fallen", "Fall", "ems"),
    (r"(leg|back|abdominal|stomach|head|hip|arm|neck|chest wall) pains?|pain in (his|her|the)", "Pain", "ems"),
    (r"\bsick\b|medical emergency|bleeding|injur", "Medical", "ems"),
    (r"shots fired|gunshot|shooting|stabbing|armed", "Weapons", "police"),
    (r"domestic|disturbance|fight|assault", "Disturbance", "police"),
    (r"burglary|robbery|theft|shoplift", "Theft", "police"),
]
NATURES = [(re.compile(pattern, re.IGNORECASE), label, category) for pattern, label, category in NATURES]
# ALS (paramedics) and BLS (EMTs). Whisper often hears BLS as "PLS" or "DLS".
LIFE_SUPPORT = re.compile(r"\b(?:(a)|[bpd])\.?\s?l\.?\s?s\b|\b(advanced|basic) life support", re.IGNORECASE)
# An EMS unit: "Medic 5", "EMS 27", or as Whisper writes them, "MS-5", "AMS-5", "PMS 5", "Mb. 14", "E-M-S-14".
EMS_UNIT = re.compile(r"\b(?:medic|ems|e-m-s|a?ms|pms|mb\.?|ambulance|qrs)[\s#-]*\d", re.IGNORECASE)
PRECISION_RANK = {"address": 0, "intersection": 1, "landmark": 2, "street": 3}
# A channel named in a call, as Whisper writes it: "Tac 2", "TAC-2A", "Tag 3", "Pac-1", "Op 2", "Fire Ground 1",
# often with the time run into it ("OPS 1-1509", "TAC 3-634"). Channels here are numbered 1 to 9.
CHANNEL = re.compile(r"\b(tac|tack|tact|tach|tag|pac|ops|op|fire ?ground)[\s.-]*(\d)([a-d])?(?![\d:]|[a-z])",
                     re.IGNORECASE)
TALKGROUP_CHANNEL = re.compile(r"\b(tac|ops|fire ?ground|fg)[\s.-]*(\d)([a-d])?\b", re.IGNORECASE)
CHANNEL_NAMES = {"ops": "Ops", "op": "Ops", "fireground": "Fireground", "fg": "Fireground"}
# A call naming an incident's street joins it only this soon after the incident's latest call.
STREET_JOIN_S = 15 * 60
# A new dispatch joins an incident only at the same place (this close) and while it is still busy.
SAME_PLACE_M = 100
REDISPATCH_S = 20 * 60
# The sample hint list vocabulary.example.txt used to have. Whisper read it back into many calls.
OLD_SAMPLE_HINTS = ["Dispatch", "Engine 12", "Medic 4", "Battalion 1", "10-4", "Main Street"]
# Tone-outs read out on Countywide end with the channel: "...Lower Saucon Communications, on TAC 2, 16:32".
DISPATCH = re.compile(r"\brespond (to|for)\b|\bcommunications?\b.{0,25}\b(tac|tag|pac|ops?)\b|"
                      r"\b(second|2nd|third) (alarm|alert)", re.IGNORECASE)
# A call this soon after the one before it on the same talkgroup is an answer to it.
REPLY_S = 20


UNIT = re.compile(r"\b(engine|medic|ems|e-m-s|a?ms|pms|mb\.?|ladder|truck|tanker|rescue|squad|ambulance|chief|"
                  r"brush|utility|qrs|tower|battalion|deputy|car|unit)[\s-]*#?\s*(\d{1,4}(?:[- ]\d{1,2}(?![\d-]))?)\b", re.IGNORECASE)
BARE_CALL_SIGN = re.compile(r"(?<![\d:.-])\b(\d{4})\b(?![:.-]\d)")


def units_in(text: str | None) -> set[str]:
    """Units named in a transcript or talker alias: "Engine 4-1" -> {"engine 41"}; four-digit call
    signs also count on their own, so "Medic 2081" and a later "2081 on location" match."""
    units = set()
    for m in UNIT.finditer(text or ""):
        number = re.sub(r"[- ]", "", m.group(2))
        word = m.group(1).lower().rstrip(".")
        # "EMS 27", "MS-27" and "Medic 27" are the same unit.
        word = "medic" if word in ("ems", "e-m-s", "ms", "ams", "pms", "mb") else word
        units.add(f"{word} {number}")
        if len(number) == 4:
            units.add(number)
    # Spoken times were already rewritten as "13:46", which this skips.
    units |= set(BARE_CALL_SIGN.findall(text or ""))
    return units


def highlights(text: str | None) -> list[tuple[int, int]]:
    """Where the words that set an incident's type are in a transcript, for highlighting."""
    spans = sorted(m.span() for pattern, _, _ in NATURES for m in pattern.finditer(text or ""))
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def life_support(text: str | None) -> str | None:
    """"ALS" or "BLS" when the call asks for an ambulance crew of that level."""
    found = [m for m in LIFE_SUPPORT.finditer(text or "")]
    if not found:
        return None
    return "ALS" if any(m.group(1) or (m.group(2) or "").lower() == "advanced" for m in found) else "BLS"


def talkgroup_agency(*names: str | None) -> str | None:
    """fire, ems, police or None from the talkgroup name and group. A "Fire / EMS" talkgroup says
    nothing about which, so it counts as fire only when the calls don't say."""
    text = f" {' '.join(n for n in names if n).lower()} "
    agencies = {agency for agency, words in AGENCY_WORDS.items() if any(word in text for word in words)}
    if agencies == {"fire", "ems"}:
        return "fire-ems"
    return next((a for a in ("police", "fire", "ems") if a in agencies), None)


def classify(calls: list[dict]) -> tuple[str | None, str | None]:
    """(category, nature) of an incident from all its calls, oldest first. Words in the calls beat
    the talkgroup name: a fire nature makes it fire, then anything medical (a medical nature, ALS/BLS,
    an EMS unit) makes it EMS, then a police nature makes it police, then the talkgroups decide."""
    natures, categories, levels, ems_units, talkgroups = [], set(), [], False, []
    for call in calls:
        nature, category = nature_of(call.get("transcript"))
        if nature:
            natures.append(nature)
        if category:
            categories.add(category)
        level = life_support(call.get("transcript"))
        if level:
            levels.append(level)
        ems_units = ems_units or bool(EMS_UNIT.search(call.get("transcript") or ""))
        agency = talkgroup_agency(call.get("talkgroup_label"), call.get("talkgroup_group"))
        if agency:
            talkgroups.append("fire" if agency == "fire-ems" else agency)

    if "fire" in categories:
        category = "fire"
    elif "ems" in categories or levels or ems_units:
        category = "ems"
    elif "police" in categories:
        category = "police"
    elif talkgroups:
        category = max(dict.fromkeys(talkgroups), key=talkgroups.count)
    else:
        category = None

    if "MVA" in natures:
        nature = "MVA"
    elif natures:
        nature = natures[0]
    elif levels:
        nature = "ALS" if "ALS" in levels else "BLS"
    else:
        nature = None
    return category, nature


def nature_of(text: str | None) -> tuple[str | None, str | None]:
    for pattern, label, category in NATURES:
        if text and pattern.search(text):
            return label, category
    return None, None


def made_up(text: str | None, vocabulary=()) -> bool:
    """A transcript Whisper probably invented: two of its prompt words in a row as the prompt has them
    ("Dispatch, Engine 12"), or the same few words over and over ("Battalion 2, Battalion 2, ...")."""
    if not text:
        return False
    low = text.lower()
    terms = [t.lower() for t in vocabulary if t.strip()]
    for a, b in zip(terms, terms[1:]):
        if re.search(rf"(?<!\w){re.escape(a)}\W{{1,3}}{re.escape(b)}(?!\w)", low):
            return True
    phrases = [p.strip() for p in re.split(r"[,.;!?]", low) if p.strip()]
    if len(phrases) >= 4:
        top = max(set(phrases), key=phrases.count)
        if phrases.count(top) >= 3 and phrases.count(top) * 2 >= len(phrases):
            return True
    return False


def is_dispatch(text: str | None) -> bool:
    """A call sending units to a new call: "respond", ALS/BLS, or a tone-out read out."""
    return bool(text and (DISPATCH.search(text) or life_support(text)))


def _channel(m: re.Match) -> str:
    word = m.group(1).lower().replace(" ", "")
    return f"{CHANNEL_NAMES.get(word, 'Tac')} {m.group(2)}{(m.group(3) or '').upper()}"


def channels_in(text: str | None) -> list[str]:
    """The channels a transcript names, in order: "Response on TAC-2A, Fireground, TAC-2" -> ["Tac 2A",
    "Tac 2"]. A Fireground channel named next to a Tac channel is dropped: here those are the Tac channels."""
    found = list(dict.fromkeys(_channel(m) for m in CHANNEL.finditer(text or "")))
    if any(not ch.startswith("Fireground") for ch in found):
        found = [ch for ch in found if not ch.startswith("Fireground")]
    return found


def channel_in(text: str | None) -> str | None:
    """The last channel a transcript names: "switch to Tac 2" -> "Tac 2"."""
    found = channels_in(text)
    return found[-1] if found else None


def talkgroup_channel(label: str | None) -> str | None:
    """The channel a talkgroup is: "Norco Tac 2A" -> "Tac 2A"."""
    m = TALKGROUP_CHANNEL.search(label or "")
    return _channel(m) if m else None


def channel_of(calls: list[dict], followed=lambda channel: True) -> tuple[str | None, int | None]:
    """(channels, when) an incident is worked on: the channels its latest dispatch names ("Tac 2A, Tac 2"),
    else the latest call naming any, else the first followed channel (a Tac channel) one of its calls is on."""
    named = [(", ".join(chs), c["call_time"], is_dispatch(c.get("transcript")))
             for c in calls if (chs := channels_in(c.get("transcript")))]
    dispatched = [n for n in named if n[2]]
    if dispatched or named:
        return (dispatched or named)[-1][:2]
    on = [(ch, c["call_time"]) for c in calls if (ch := talkgroup_channel(c.get("talkgroup_label"))) and followed(ch)]
    return on[0] if on else (None, None)


def street_words(label: str | None) -> list[str]:
    """The street names of an incident's place, as words(): "N 3rd St & Main St, Easton" ->
    ["n 3rd st", "main st"]. Only names of two words or more, so "Main" alone never matches."""
    streets = []
    for part in re.split(r"\s*&\s*|\s+and\s+", (label or "").split(",")[0]):
        ws = [w for w in words(part) if not w.isdigit()]
        if len(ws) >= 2:
            streets.append(" ".join(ws))
    return streets


def distance_m(lat1, lon1, lat2, lon2) -> float:
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110_574
    return math.hypot(dx, dy)


class MapWorker:
    def __init__(self, config: Config, db: Database, gazetteer: Gazetteer, geocoder: Geocoder | None):
        self.config = config
        self.db = db
        self.gazetteer = gazetteer
        self.geocoder = geocoder
        self._queue: "queue.Queue[int | None]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="incident-map", daemon=True)
        self._backlog_loaded = False
        # Called with (call id, incident id, call) after a call is placed (phone alerts listen here).
        self.on_placed = None
        # Called with the ids of incidents whose calls the user changed by hand.
        self.on_changed = None
        # Whisper's prompt: on unclear audio it repeats these words back ("Dispatch, Engine 12, Main Street").
        # The old sample hint list too, since calls already in the log were transcribed with it.
        self.vocabulary = load_vocabulary(config.vocabulary_path)
        self.echo_lists = [self.vocabulary, OLD_SAMPLE_HINTS]
        # Channel -> (time, incident id or None) of the latest call sending an incident there, placed or not.
        self._claims: dict[str, tuple[int, int | None]] = {}
        # Hand edits from the map page and the worker thread take turns.
        self.lock = threading.RLock()

    def start(self):
        self._load_backlog()
        self._thread.start()

    def stop(self, timeout: float = 10):
        self._queue.put(None)
        self._thread.join(timeout)

    def submit(self, call_id: int):
        self._queue.put(call_id)

    def queue_size(self) -> int:
        return self._queue.qsize()

    def wait_idle(self, timeout: float = 30) -> bool:
        """Test helper: waits until the queue is drained."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False

    def rebuild(self):
        """Places every call again, e.g. after the street list changed. Fixes made by hand are kept."""
        with self.lock:
            self.db.clear_map()
        self._backlog_loaded = False
        self._load_backlog()

    def _load_backlog(self):
        # Calls from before the street list existed wait until it does.
        if self._backlog_loaded or not self.gazetteer.loaded:
            return
        self._backlog_loaded = True
        backlog = self.db.calls_to_map()
        if backlog:
            log.info("Incident map: looking for places in %d earlier calls", len(backlog))
        for call_id in backlog:
            self._queue.put(call_id)

    def _run(self):
        while True:
            call_id = self._queue.get()
            try:
                if call_id is None:
                    return
                self.gazetteer.reload_if_changed()
                self._load_backlog()
                with self.lock:
                    self.process(call_id)
            except Exception:
                log.exception("Incident map: call %s failed", call_id)
            finally:
                self._queue.task_done()

    def process(self, call_id: int):
        if not self.gazetteer.loaded:
            return
        call = self.db.get_call(call_id)
        if call is None or call["status"] != "done":
            return
        try:
            self._process(call)
        finally:
            if is_dispatch(call["transcript"]):
                incident_id = self.db.incident_of_call(call_id)
                for channel in channels_in(call["transcript"]):
                    self._claims[channel] = (call["call_time"], incident_id)

    def _process(self, call: dict):
        call_id = call["id"]
        edit = self.db.map_edit(call_id)
        if edit and edit["anchor_call_id"] is None:
            self.db.set_map_status(call_id, "removed")      # taken off the map by hand
            return
        if not edit and any(made_up(call["transcript"], terms) for terms in self.echo_lists):
            self.db.set_map_status(call_id, "noise")
            return
        context = " ".join(filter(None, (call["talkgroup_label"], call["talkgroup_group"])))
        found = self.gazetteer.find(call["transcript"], context)
        point, precision = self.place(found, call["call_time"]) if found else (None, None)
        place = None
        if found:
            place = {"call_id": call_id, "kind": found.kind, "heard": found.heard, "label": found.label,
                     "precision": precision}
        if edit:
            self.apply_edit(call, edit, place, point)
            return
        if point is None:
            if self.join_without_place(call):
                self.db.set_map_status(call_id, "follow-up")
                return
            if place is None:
                self.db.set_map_status(call_id, "none")
                return
            self.db.add_call_place(place)
            self.db.set_map_status(call_id, "unplaced")
            log.info("Incident map: call %d mentions %s but it could not be placed", call_id, found.label)
            return
        place["lat"], place["lon"] = point
        place["incident_id"] = self.assign(call, found, point, precision)
        self.db.add_call_place(place)
        self.reclassify(place["incident_id"])
        self.db.set_map_status(call_id, "placed")
        log.info("Incident map: call %d at %s (%s)", call_id, found.label, precision)
        if self.on_placed:
            self.on_placed(call_id, place["incident_id"], call)

    def join_without_place(self, call: dict) -> bool:
        """Joins a call that names no place to an incident: by a unit it names, then by the Tac
        channel it is on, then by a street of an incident it names, then as a quick reply on the same
        talkgroup. A new dispatch is a new call, even when its place can't be found, so it joins none.
        Returns True when it joined one."""
        if is_dispatch(call["transcript"]):
            return False
        return (self.follow_up(call) or self.follow_channel(call) or self.follow_street(call)
                or self.follow_reply(call))

    def follow_up(self, call: dict) -> bool:
        """Joins a call that names no place ("Engine 2011 on scene") to the recent incident a unit
        it names, or its talker alias, already appears in. Returns True when it joined one."""
        units = units_in(call["transcript"]) | units_in(call["talker_alias"])
        if not units:
            return False
        t = call["call_time"]
        window = self.config.map.incident_window_minutes * 60
        for incident in self.db.nearby_incidents(t - window, t + 60):   # most recent first
            if incident["id"] == call.get("incident_id"):
                continue
            known = set()
            for other in self.db.incident_calls_brief(incident["id"]):
                known |= units_in(other["transcript"]) | units_in(other["talker_alias"])
            shared = units & known
            if not shared:
                continue
            # Name the unit the call said out loud rather than its radio's alias.
            spoken = shared & units_in(call["transcript"])
            unit = max(spoken or shared, key=lambda u: (len(u), u))
            self._join(call, incident, "follow-up", unit, unit.title())
            return True
        return False

    def follow_channel(self, call: dict) -> bool:
        """Joins a call on a Tac channel to the incident that was last moved to that channel."""
        channel = talkgroup_channel(call["talkgroup_label"])
        if channel is None or not self._followed(channel):
            return False
        t = call["call_time"]
        window = self.config.map.channel_window_minutes * 60
        moved = [i for i in self.db.nearby_incidents(t - window, t + 60)
                 if channel in (i["channel"] or "").split(", ") and (i["channel_time"] or 0) <= t + 60]
        if not moved:
            return False
        incident = max(moved, key=lambda i: i["channel_time"] or 0)
        claim = self._claims.get(channel)
        if claim and claim[0] > (incident["channel_time"] or 0) and claim[1] != incident["id"]:
            return False    # since then a call the map couldn't place was sent to that channel
        self._join(call, incident, "channel", channel.lower(), channel)
        return True

    def follow_street(self, call: dict) -> bool:
        """Joins a call that names a street of exactly one incident from the last few minutes
        ("on scene, staging on Center Street")."""
        text = f" {' '.join(words(call['transcript'] or ''))} "
        t = call["call_time"]
        matches = []
        for incident in self.db.nearby_incidents(t - STREET_JOIN_S, t + 60):
            streets = street_words(incident["label"])
            if streets and any(f" {street} " in text for street in streets):
                matches.append(incident)
        if len(matches) != 1:
            return False
        street = next(st for st in street_words(matches[0]["label"]) if f" {st} " in text)
        self._join(call, matches[0], "same-street", street, matches[0]["label"])
        return True

    def follow_reply(self, call: dict) -> bool:
        """Joins a quick answer ("copy, en route") to the incident of the call just before it on the
        same talkgroup."""
        if not call["talkgroup"]:
            return False
        before = self.db.last_call_on(call["talkgroup"], call["call_time"], call["id"])
        if not before or before["incident_id"] is None or call["call_time"] - before["call_time"] > REPLY_S:
            return False
        incident = self.db.get_incident(before["incident_id"])
        if incident is None:
            return False
        self._join(call, incident, "reply", None, "Reply")
        return True

    def _followed(self, channel: str) -> bool:
        kinds = [k.strip().lower() for k in self.config.map.follow_channels]
        return channel.split()[0].lower() in kinds

    def _join(self, call: dict, incident: dict, kind: str, heard: str | None, label: str):
        """Adds a call that names no place to an incident."""
        t = call["call_time"]
        self.db.add_call_place({"call_id": call["id"], "incident_id": incident["id"], "kind": kind,
                                "heard": heard, "label": label, "precision": kind})
        self.db.update_incident(incident["id"], first_time=min(incident["first_time"], t),
                                last_time=max(incident["last_time"], t))
        self.reclassify(incident["id"])
        log.info("Incident map: call %d joins incident %d (%s: %s)", call["id"], incident["id"], kind, label)
        if self.on_placed:
            self.on_placed(call["id"], incident["id"], call)

    def reclassify(self, incident_id: int):
        """Works out an incident's category, nature and channel again from all of its calls."""
        calls = self.db.incident_calls_brief(incident_id)
        category, nature = classify(calls)
        if category is None and calls and self.db.pages(since=calls[0]["call_time"] - 600,
                                                        until=calls[0]["call_time"] + 60, limit=1):
            category = "fire"      # a tone-out just before, and nothing else to go on
        channel, channel_time = channel_of(calls, self._followed)
        self.db.update_incident(incident_id, category=category, nature=nature, channel=channel,
                                channel_time=channel_time)

    # ---- fixes by hand from the map page. Each is remembered in map_edits so a rebuild keeps it.

    def apply_edit(self, call: dict, edit: dict, place: dict | None, point):
        """Places a call the user moved by hand where they put it, when the map is rebuilt."""
        incident_id = None
        if edit["anchor_call_id"] != call["id"]:
            incident_id = self.db.incident_of_call(edit["anchor_call_id"])
        if place and point:
            place = {**place, "lat": point[0], "lon": point[1]}
        else:
            place = {"call_id": call["id"], "kind": "manual", "heard": None, "label": edit["label"],
                     "lat": edit["lat"], "lon": edit["lon"], "precision": "manual"}
        if incident_id is None:
            if place["lat"] is None:
                self.db.set_map_status(call["id"], "none")
                return
            t = call["call_time"]
            incident_id = self.db.create_incident({"first_time": t, "last_time": t, "lat": place["lat"],
                                                   "lon": place["lon"], "label": place["label"] or edit["label"]})
        place["incident_id"] = incident_id
        self.db.add_call_place(place)
        self.refresh(incident_id)
        self.db.set_map_status(call["id"], "placed")

    def move_call(self, call_id: int, incident_id: int) -> list[int]:
        """Moves a call into another incident. Returns the incidents changed."""
        with self.lock:
            target = self.db.get_incident(incident_id)
            if target is None or self.db.get_call(call_id) is None:
                raise KeyError("no such call or incident")
            source = self.db.incident_of_call(call_id)
            self.db.move_call_places(call_id, incident_id, {
                "kind": "manual", "label": target["label"], "lat": target["lat"], "lon": target["lon"],
                "precision": "manual"})
            self.db.set_map_status(call_id, "placed")
            return self._edited([incident_id, source])

    def split_call(self, call_id: int) -> list[int]:
        """Takes a call out of its incident into an incident of its own."""
        with self.lock:
            source_id = self.db.incident_of_call(call_id)
            if source_id is None:
                raise KeyError("that call is not on the map")
            source = self.db.get_incident(source_id)
            own = next(c for c in self.db.incident_calls_brief(source_id) if c["id"] == call_id)
            lat, lon = (own["lat"], own["lon"]) if own["lat"] is not None else (source["lat"], source["lon"])
            new_id = self.db.create_incident({"first_time": own["call_time"], "last_time": own["call_time"],
                                              "lat": lat, "lon": lon, "label": own["label"] if own["lat"] is not None
                                              else source["label"]})
            self.db.move_call_places(call_id, new_id)
            return self._edited([new_id, source_id])

    def merge(self, source_id: int, target_id: int) -> list[int]:
        """Puts every call of one incident into another."""
        with self.lock:
            if source_id == target_id or not self.db.get_incident(source_id) or not self.db.get_incident(target_id):
                raise KeyError("no such incidents")
            for other in self.db.incident_calls_brief(source_id):
                self.db.move_call_places(other["id"], target_id)
            return self._edited([target_id, source_id])

    def remove_call(self, call_id: int) -> list[int]:
        """Takes one call off the map."""
        with self.lock:
            source = self.db.incident_of_call(call_id)
            if source is None:
                raise KeyError("that call is not on the map")
            self.db.move_call_places(call_id, None)
            self.db.set_map_status(call_id, "removed")
            self.db.set_map_edits([call_id], None)
            return self._edited([source])

    def _edited(self, incident_ids: list[int | None]) -> list[int]:
        """Brings changed incidents up to date and remembers their calls as a group."""
        kept = []
        for incident_id in dict.fromkeys(i for i in incident_ids if i is not None):
            if not self.refresh(incident_id):
                continue
            kept.append(incident_id)
            incident = self.db.get_incident(incident_id)
            call_ids = [c["id"] for c in self.db.incident_calls_brief(incident_id)]
            self.db.set_map_edits(call_ids, min(call_ids), incident["lat"], incident["lon"], incident["label"])
        if self.on_changed:
            self.on_changed(kept)
        return kept

    def refresh(self, incident_id: int) -> bool:
        """Times, place and type of an incident from the calls now in it; deletes it if it has none."""
        calls = self.db.incident_calls_brief(incident_id)
        if not calls:
            self.db.delete_incident(incident_id)
            return False
        changes = {"first_time": min(c["call_time"] for c in calls), "last_time": max(c["call_time"] for c in calls)}
        placed = [c for c in calls if c["lat"] is not None and c["precision"] in PRECISION_RANK]
        if placed:
            best = min(placed, key=lambda c: (PRECISION_RANK[c["precision"]], c["call_time"]))
            changes.update(lat=best["lat"], lon=best["lon"], label=best["label"])
        self.db.update_incident(incident_id, **changes)
        self.reclassify(incident_id)
        return True

    def place(self, found: Found, call_time: int) -> tuple[tuple[float, float] | None, str]:
        """Where a found place is, and how exactly: address, intersection, landmark or street."""
        if len(found.points) == 1:
            return found.points[0], found.kind
        if len(found.points) > 1:
            # Main St & 3rd St exist in several towns and nothing said which: take the one an
            # incident is already at, if any.
            window = self.config.map.incident_window_minutes * 60
            active = [i for i in self.db.nearby_incidents(call_time - window, call_time + window)]
            for point in found.points:
                if any(distance_m(*point, i["lat"], i["lon"]) <= self.config.map.incident_radius_m for i in active):
                    return point, found.kind
            return None, found.kind

        if found.query and self.geocoder:
            result = self.geocoder.lookup(found.query)
            if result:
                point = (result["lat"], result["lon"])
                with_shapes = [s for s in found.streets if s.lines]
                distance = self.gazetteer.distance_to(with_shapes, *point) if with_shapes else None
                # The online search sometimes answers with the right number on the wrong street.
                if distance is None or distance <= 250:
                    precision = result["kind"] if found.kind == "address" else found.kind
                    return point, precision

        if found.kind == "address":
            # Offline: a short street in one town is close enough to be useful.
            with_shapes = [s for s in found.streets if s.lines]
            if with_shapes and len({s.town_key for s in with_shapes}) == 1:
                longest = max(with_shapes, key=self.gazetteer.length_m)
                if self.gazetteer.length_m(longest) <= 2000:
                    return self.gazetteer.middle(longest), "street"
        return None, found.kind

    def assign(self, call: dict, found: Found, point: tuple[float, float], precision: str) -> int:
        """Joins the call to a nearby recent incident, or starts one. Returns the incident id."""
        settings = self.config.map
        t = call["call_time"]
        window = settings.incident_window_minutes * 60
        nearest, best = None, None
        dispatch = is_dispatch(call["transcript"])
        for incident in self.db.nearby_incidents(t - window, t + window):
            d = distance_m(*point, incident["lat"], incident["lon"])
            if dispatch and (d > SAME_PLACE_M or t - incident["last_time"] > REDISPATCH_S):
                continue    # a new dispatch to the next block, or to the same place much later, is a new call
            if d <= settings.incident_radius_m and (best is None or d < best):
                nearest, best = incident, d

        if nearest is None:
            # Category and nature are filled in by reclassify() once the call is attached.
            return self.db.create_incident({
                "first_time": t, "last_time": t, "lat": point[0], "lon": point[1], "label": found.label})

        changes = {"first_time": min(nearest["first_time"], t), "last_time": max(nearest["last_time"], t)}
        # A house number heard later beats the intersection heard first.
        current = min((PRECISION_RANK.get(p, 9) for p in self.db.incident_precisions(nearest["id"])), default=9)
        if PRECISION_RANK.get(precision, 9) < current:
            changes.update(lat=point[0], lon=point[1], label=found.label)
        self.db.update_incident(nearest["id"], **changes)
        return nearest["id"]
