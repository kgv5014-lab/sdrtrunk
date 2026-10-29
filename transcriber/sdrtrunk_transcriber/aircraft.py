"""Helicopters from an ADS-B receiver (readsb / tar1090 / ultrafeeder), for the incident map.

Reads the receiver's aircraft.json every few seconds and keeps the helicopters, plus any aircraft on the
watch list, with a short trail each. A helicopter that comes down low near a recent incident is marked
as landing there, and can send a phone alert.

Each helicopter is remembered by its ICAO hex code and sorted into medevac, police or other: by hand on the
map, else by its registered owner, else learned from where it sets down. Landing at hospitals (and at
incidents) makes it medevac. Only medevac landings alert.
"""

import json
import logging
import threading
import time

import requests

from .config import Config
from .db import Database
from .incidents import distance_m

log = logging.getLogger(__name__)

# ICAO type designators of helicopters, for receivers that don't report the rotorcraft category (A7).
HELICOPTER_TYPES = {
    "A109", "A119", "A129", "A139", "A149", "A169", "A189", "AS32", "AS50", "AS55", "AS65", "B06", "B06T",
    "B105", "B212", "B222", "B230", "B407", "B412", "B427", "B429", "B430", "B505", "BK17", "EC20", "EC25",
    "EC30", "EC35", "EC45", "EC55", "EC75", "EH10", "EXPL", "H47", "H53", "H60", "H64", "H500", "MD60",
    "R22", "R44", "R66", "S76", "S92", "UH1", "UH1Y", "V22", "H160", "H125", "H130", "H135", "H145", "H175",
    "AW09", "S300", "GAZL", "LYNX", "PUMA", "NH90",
}
METERS_PER_MILE = 1609.344
# Positions kept per aircraft for its trail on the map.
TRAIL_POINTS = 40
# An aircraft not heard for this long is dropped.
STALE_S = 60
# Every aircraft (not only helicopters) is kept this long, with where it was, to match radio calls to it.
RECENT_S = 900


# Operators of air ambulances in and around the Lehigh Valley, matched in the receiver's ownOp field.
MEDICAL_OPERATORS = ("GEISINGER", "LIFE FLIGHT", "LIFEFLIGHT", "MEDEVAC", "MED EVAC", "AIR METHODS", "PHI ",
                     "PHI,", "LIFENET", "JEFFSTAT", "PENNSTAR", "LIFE LION", "HEALTH NETWORK", "ST LUKE",
                     "ST. LUKE", "HOSPITAL", "MEDICAL")


# Owners of police and other law enforcement aircraft.
POLICE_OPERATORS = ("POLICE", "SHERIFF", "STATE TROOPER", "CUSTOMS AND BORDER", "BORDER PROTECTION",
                    "HOMELAND SECURITY", "LAW ENFORCEMENT")
KINDS = ("medevac", "police", "other")


def is_helicopter(plane: dict) -> bool:
    return (plane.get("category") == "A7" or (plane.get("t") or "").upper() in HELICOPTER_TYPES
            or "HELICOPTER" in (plane.get("desc") or "").upper())


def is_medical(plane: dict) -> bool:
    operator = (plane.get("ownOp") or "").upper()
    return any(word in operator for word in MEDICAL_OPERATORS)


def is_police(plane: dict) -> bool:
    operator = (plane.get("ownOp") or "").upper()
    return any(word in operator for word in POLICE_OPERATORS)


def decide_kind(known: dict, learn_landings: int = 2) -> tuple[str, str]:
    """medevac, police or other for a remembered helicopter, and what decided it. `known` is its row in the
    helicopters table: kind picked by hand, operator, hospital_landings, incident_landings."""
    if known.get("kind") in KINDS:
        return known["kind"], "set by hand"
    owner = {"ownOp": known.get("operator")}
    if is_police(owner):
        return "police", "from owner name"
    if is_medical(owner):
        return "medevac", "from owner name"
    hospital, incident = known.get("hospital_landings") or 0, known.get("incident_landings") or 0
    if hospital >= max(1, learn_landings) or (hospital >= 1 and incident >= 1):
        return "medevac", f"learned: {hospital} hospital and {incident} incident landing{'' if incident == 1 else 's'}"
    if hospital or incident:
        return "other", f"{hospital} hospital and {incident} incident landing{'' if incident == 1 else 's'} so far"
    return "other", ""


def load_hospitals(geo_path, extra: list) -> list[dict]:
    """Hospitals from places-geo.json (fetch-places.bat) plus [aircraft] hospitals in config.toml."""
    found = []
    if geo_path and geo_path.exists():
        try:
            data = json.loads(geo_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("ADS-B: could not read hospitals from %s (%s)", geo_path, e)
            data = {}
        for mark in data.get("landmarks", []):
            if mark.get("type") == "amenity=hospital" and mark.get("lat") is not None and mark.get("name"):
                found.append({"name": mark["name"], "lat": mark["lat"], "lon": mark["lon"]})
    for entry in extra:
        if isinstance(entry, dict) and entry.get("lat") is not None and entry.get("lon") is not None:
            found.append({"name": str(entry.get("name") or "Hospital"), "lat": float(entry["lat"]),
                          "lon": float(entry["lon"])})
    return found


def altitude_ft(plane: dict) -> int | None:
    alt = plane.get("alt_baro", plane.get("alt_geom"))
    if alt == "ground":
        return 0
    return int(alt) if isinstance(alt, (int, float)) else None


class WatchList:
    """Aircraft Ken names in config.toml: [aircraft] watch = [{match = "N123AB", name = "St. Luke's 1"}].
    `match` is a tail number, a flight callsign or an ICAO hex code, and may end in * ("LIFE*")."""

    def __init__(self, entries: list):
        self.entries = []
        for entry in entries:
            if isinstance(entry, str):
                entry = {"match": entry}
            match = str(entry.get("match", "")).strip().upper()
            if match:
                self.entries.append((match, str(entry.get("name", "")).strip()))

    def name(self, plane: dict) -> str | None:
        """The watch-list name for this aircraft ("" when listed without one), or None if not listed."""
        keys = [str(plane.get(k) or "").strip().upper() for k in ("r", "flight", "hex")]
        for match, name in self.entries:
            for key in filter(None, keys):
                if key == match or (match.endswith("*") and key.startswith(match[:-1])):
                    return name
        return None


class AircraftWatch:
    def __init__(self, config: Config, db: Database, alerter=None, session=None):
        self.settings = config.aircraft
        self.db = db
        self.alerter = alerter
        self.session = session or requests.Session()
        self.watch = WatchList(self.settings.watch)
        self._lock = threading.Lock()
        self._planes: dict[str, dict] = {}
        # Every aircraft heard lately, by hex, with timed positions (for aviation.py).
        self._recent: dict[str, dict] = {}
        self._landed: set[tuple[str, int]] = set()
        # Every helicopter seen, by hex: what it is and how often it set down at hospitals and incidents.
        self._known: dict[str, dict] = db.helicopters()
        # Where each helicopter last sat low and slow at a hospital, so one landing counts once.
        self._on_pad: dict[str, tuple[str, float]] = {}
        self._set_down_at: set[tuple[str, int]] = set()
        self._geo_path = config.geo_path
        self._geo_mtime = None
        self.hospitals: list[dict] = []
        self.error: str | None = None
        self.updated: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aircraft", daemon=True)

    def start(self):
        log.info("Watching helicopters from %s", self.settings.url)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:
                if self.error is None:
                    log.warning("ADS-B: could not read %s (%s); retrying", self.settings.url, e)
                self.error = str(e)[:200]
            self._stop.wait(max(1.0, self.settings.poll_s))

    def poll(self, now: float | None = None):
        reply = self.session.get(self.settings.url, timeout=5)
        reply.raise_for_status()
        self.update(reply.json(), now)
        if self.error:
            log.info("ADS-B: reading %s again", self.settings.url)
        self.error = None

    def update(self, data: dict, now: float | None = None):
        now = time.time() if now is None else now
        incidents = self._recent_incidents(now)
        self._load_hospitals()
        with self._lock:
            self._record(data, now)
            for plane in data.get("aircraft", []):
                hex_id = plane.get("hex")
                name = self.watch.name(plane)
                if not hex_id or (name is None and not (self.settings.all_helicopters and is_helicopter(plane))):
                    continue
                lat, lon = plane.get("lat"), plane.get("lon")
                if lat is None or lon is None or plane.get("seen_pos", 0) > STALE_S:
                    continue
                shown = self._planes.get(hex_id) or {"hex": hex_id, "trail": []}
                shown.update({
                    "flight": (plane.get("flight") or "").strip(), "reg": plane.get("r") or "",
                    "type": plane.get("t") or "", "desc": plane.get("desc") or "",
                    "operator": plane.get("ownOp") or "",
                    "name": name or "", "watched": name is not None,
                    "lat": lat, "lon": lon, "alt_ft": altitude_ft(plane), "speed_kt": plane.get("gs"),
                    "track": plane.get("track"), "seen": now - plane.get("seen_pos", 0),
                })
                if not shown["trail"] or shown["trail"][-1] != [lat, lon]:
                    shown["trail"] = (shown["trail"] + [[lat, lon]])[-TRAIL_POINTS:]
                shown["near"] = self._near(shown, incidents)
                known = self._remember(shown, now)
                self._set_down(shown, known, now)
                self._classify(shown)
                self._planes[hex_id] = shown
                if shown["near"] and shown["near"]["landing"]:
                    self._landing(shown, shown["near"], now)
            for hex_id in [h for h, p in self._planes.items() if now - p["seen"] > STALE_S]:
                del self._planes[hex_id]
        self.updated = now

    def _record(self, data: dict, now: float):
        """Keeps every aircraft heard in the last RECENT_S, so a radio call can be matched to it later."""
        for plane in data.get("aircraft", []):
            hex_id = plane.get("hex")
            if not hex_id or not (plane.get("flight") or plane.get("r")):
                continue
            seen = self._recent.get(hex_id) or {"hex": hex_id, "track_points": []}
            seen.update({"flight": (plane.get("flight") or "").strip(), "reg": plane.get("r") or "",
                         "type": plane.get("t") or "", "desc": plane.get("desc") or "",
                         "operator": plane.get("ownOp") or "", "helicopter": is_helicopter(plane),
                         "seen": now - plane.get("seen", 0)})
            lat, lon = plane.get("lat"), plane.get("lon")
            if lat is not None and lon is not None and plane.get("seen_pos", 0) <= STALE_S:
                point = [round(now - plane.get("seen_pos", 0), 1), lat, lon, altitude_ft(plane), plane.get("gs")]
                if not seen["track_points"] or seen["track_points"][-1][1:3] != point[1:3]:
                    seen["track_points"].append(point)
            seen["track_points"] = [p for p in seen["track_points"] if now - p[0] <= RECENT_S]
            self._recent[hex_id] = seen
        for hex_id in [h for h, p in self._recent.items() if now - p["seen"] > RECENT_S]:
            del self._recent[hex_id]

    def recent_aircraft(self, when: float, window_s: float = 600) -> list[dict]:
        """Aircraft heard within window_s before a time (or up to two minutes after), with timed positions
        [time, lat, lon, alt_ft, speed_kt] and, for helicopters, whether they are medevac or police."""
        with self._lock:
            found = []
            for plane in self._recent.values():
                times = [p[0] for p in plane["track_points"]] + [plane["seen"]]
                if not any(when - window_s <= t <= when + 120 for t in times):
                    continue
                known = self._known.get(plane["hex"])
                kind = decide_kind(known, self.settings.learn_landings)[0] if known else None
                found.append(dict(plane, track_points=list(plane["track_points"]),
                                  kind=kind if kind != "other" else None))
            return found

    def aircraft(self) -> list[dict]:
        with self._lock:
            return [dict(p, trail=list(p["trail"])) for p in self._planes.values()]

    def set_kind(self, hex_id: str, kind: str | None) -> dict:
        """Ken's pick from the map: medevac, police or other; None goes back to learning."""
        hex_id = hex_id.strip().lower()
        with self._lock:
            known = self._known.setdefault(hex_id, {"hex": hex_id, "hospital_landings": 0, "incident_landings": 0})
            known["kind"] = kind if kind in KINDS else None
            self.db.save_helicopter(known)
            if hex_id in self._planes:
                self._classify(self._planes[hex_id])
            return dict(known)

    def _load_hospitals(self):
        mtime = self._geo_path.stat().st_mtime if self._geo_path and self._geo_path.exists() else None
        if mtime != self._geo_mtime or not self.hospitals:
            self._geo_mtime = mtime
            self.hospitals = load_hospitals(self._geo_path, self.settings.hospitals)

    def _remember(self, shown: dict, now: float) -> dict:
        """The helicopter's row in the helicopters table, updated with what the receiver says now."""
        known = self._known.get(shown["hex"])
        fresh = {"reg": shown["reg"], "flight": shown["flight"], "type": shown["type"]}
        if shown["operator"]:
            fresh["operator"] = shown["operator"]
        if known is None:
            known = self._known[shown["hex"]] = {"hex": shown["hex"], "hospital_landings": 0,
                                                 "incident_landings": 0, "first_seen": int(now)}
        changed = any(value and known.get(key) != value for key, value in fresh.items())
        known.update({key: value for key, value in fresh.items() if value})
        if changed or now - (known.get("last_seen") or 0) >= 300:
            known["last_seen"] = int(now)
            self.db.save_helicopter(known)
        return known

    def _set_down(self, shown: dict, known: dict, now: float):
        """Counts a landing when the helicopter is low and slow at a hospital or an incident."""
        alt, speed = shown["alt_ft"], shown["speed_kt"]
        if alt is None or alt > self.settings.pad_altitude_ft or (speed is not None and speed > self.settings.pad_speed_kt):
            return
        hex_id, changed = shown["hex"], False
        hospital = self._hospital_near(shown["lat"], shown["lon"])
        if hospital:
            name, miles = hospital
            last = self._on_pad.get(hex_id)
            if last is None or last[0] != name or now - last[1] > 3600:
                known["hospital_landings"] = (known.get("hospital_landings") or 0) + 1
                changed = True
                log.info("Helicopter %s set down at %s (%d hospital landings)", self._who(shown), name,
                         known["hospital_landings"])
                self.db.add_helicopter_landing({"hex": hex_id, "landing_time": int(now), "name": self._who(shown),
                                                "place": name, "miles": miles})
            self._on_pad[hex_id] = (name, now)
        near = shown["near"]
        if near and (hex_id, near["incident_id"]) not in self._set_down_at:
            self._set_down_at.add((hex_id, near["incident_id"]))
            known["incident_landings"] = (known.get("incident_landings") or 0) + 1
            changed = True
        if changed:
            self.db.save_helicopter(known)

    def _hospital_near(self, lat: float, lon: float) -> tuple[str, float] | None:
        best = None
        for hospital in self.hospitals:
            meters = distance_m(lat, lon, hospital["lat"], hospital["lon"])
            if meters <= self.settings.hospital_radius_miles * METERS_PER_MILE and (best is None or meters < best[0]):
                best = (meters, hospital["name"])
        return (best[1], round(best[0] / METERS_PER_MILE, 2)) if best else None

    def _classify(self, shown: dict):
        kind, how = decide_kind(self._known.get(shown["hex"]) or {}, self.settings.learn_landings)
        shown.update({"kind": kind, "kind_how": how, "medical": kind == "medevac", "police": kind == "police",
                      "hand_kind": (self._known.get(shown["hex"]) or {}).get("kind")})

    def _recent_incidents(self, now: float) -> list[dict]:
        return [i for i in self.db.incidents(since=int(now - self.settings.incident_hours * 3600), limit=200)
                if i.get("lat") is not None and i.get("lon") is not None]

    def _near(self, plane: dict, incidents: list[dict]) -> dict | None:
        """The closest recent incident within landing_radius_miles, and whether the aircraft is low and slow
        enough there to be landing."""
        best = None
        for incident in incidents:
            meters = distance_m(plane["lat"], plane["lon"], incident["lat"], incident["lon"])
            if meters <= self.settings.landing_radius_miles * METERS_PER_MILE and (best is None or meters < best[0]):
                best = (meters, incident)
        if best is None:
            return None
        meters, incident = best
        alt, speed = plane["alt_ft"], plane["speed_kt"]
        landing = alt is not None and alt <= self.settings.landing_altitude_ft and (speed is None or speed <= 80)
        return {"incident_id": incident["id"], "label": incident.get("label") or "",
                "nature": incident.get("nature") or "", "miles": round(meters / METERS_PER_MILE, 2),
                "landing": landing}

    @staticmethod
    def _who(plane: dict) -> str:
        return plane["name"] or plane["flight"] or plane["reg"] or plane["hex"]

    def _landing(self, plane: dict, near: dict, now: float):
        key = (plane["hex"], near["incident_id"])
        if key in self._landed:
            return
        self._landed.add(key)
        who = self._who(plane)
        self.db.add_helicopter_landing({"hex": plane["hex"], "landing_time": int(now), "name": who,
                                        "incident_id": near["incident_id"], "miles": near["miles"]})
        if plane["operator"] and not plane["name"]:
            who += f" ({plane['operator']})"
        log.info("Helicopter %s (%s) coming down %.1f mi from incident %d (%s)", who, plane["kind"], near["miles"],
                 near["incident_id"], near["label"])
        wanted = plane["kind"] == "medevac" or plane["watched"] or not self.settings.alert_only_medevac
        if self.alerter and self.settings.alert_landing and wanted:
            where = " ".join(filter(None, (near["nature"], near["label"])))
            title = "Medevac landing" if plane["kind"] == "medevac" else "Helicopter landing"
            self.alerter.send("aircraft", f"{title}: {who}",
                              f"{near['miles']} mi from {where or 'an incident'}", tags=["helicopter"],
                              priority=4, click=f"{self.alerter.base_url}/map" if self.alerter.base_url else None,
                              incident_id=near["incident_id"])


def incident_helicopters(db: Database, incident_ids: list[int], learn_landings: int = 2) -> dict[int, list[dict]]:
    """Medevac and police helicopters that came down near each incident, once per helicopter, for the
    incident list. Decided by what is known about the helicopter now, so a landing counts once it is learned."""
    notes: dict[int, list[dict]] = {}
    for incident_id, landings in db.helicopter_landings(incident_ids).items():
        seen = set()
        for landing in landings:
            known = dict(landing, kind=landing["hand_kind"])
            kind, _how = decide_kind(known, learn_landings)
            if kind == "other" or landing["hex"] in seen:
                continue
            seen.add(landing["hex"])
            clock = time.strftime("%I:%M %p", time.localtime(landing["landing_time"])).lstrip("0")
            verb = "landed" if kind == "medevac" else "overhead"
            label = "Medevac" if kind == "medevac" else "Police"
            notes.setdefault(incident_id, []).append({
                "hex": landing["hex"], "name": landing["name"] or landing["hex"], "kind": kind,
                "time": landing["landing_time"], "miles": landing["miles"],
                "text": f"{label} {landing['name'] or landing['hex']} {verb} {clock}"})
    return notes
