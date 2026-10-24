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
from .addresses import Found, Gazetteer

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
        """Places every call again, e.g. after the street list changed."""
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
        context = " ".join(filter(None, (call["talkgroup_label"], call["talkgroup_group"])))
        found = self.gazetteer.find(call["transcript"], context)
        if found is None:
            self.db.set_map_status(call_id, "follow-up" if self.follow_up(call) else "none")
            return
        point, precision = self.place(found, call["call_time"])
        place = {"call_id": call_id, "kind": found.kind, "heard": found.heard, "label": found.label,
                 "precision": precision}
        if point is None:
            if self.follow_up(call):
                self.db.set_map_status(call_id, "follow-up")
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
            self.db.add_call_place({"call_id": call["id"], "incident_id": incident["id"], "kind": "follow-up",
                                    "heard": unit, "label": unit.title(), "precision": "follow-up"})
            self.db.update_incident(incident["id"], first_time=min(incident["first_time"], t),
                                    last_time=max(incident["last_time"], t))
            self.reclassify(incident["id"])
            log.info("Incident map: call %d follows up incident %d (%s)", call["id"], incident["id"], unit)
            if self.on_placed:
                self.on_placed(call["id"], incident["id"], call)
            return True
        return False

    def reclassify(self, incident_id: int):
        """Works out an incident's category and nature again from all of its calls."""
        calls = self.db.incident_calls_brief(incident_id)
        category, nature = classify(calls)
        if category is None and calls and self.db.pages(since=calls[0]["call_time"] - 600,
                                                        until=calls[0]["call_time"] + 60, limit=1):
            category = "fire"      # a tone-out just before, and nothing else to go on
        self.db.update_incident(incident_id, category=category, nature=nature)

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
        for incident in self.db.nearby_incidents(t - window, t + window):
            d = distance_m(*point, incident["lat"], incident["lon"])
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
