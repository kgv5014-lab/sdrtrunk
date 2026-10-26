"""Helicopters from an ADS-B receiver (readsb / tar1090 / ultrafeeder), for the incident map.

Reads the receiver's aircraft.json every few seconds and keeps the helicopters, plus any aircraft on the
watch list, with a short trail each. A helicopter that comes down low near a recent incident is marked
as landing there, and can send a phone alert.
"""

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


# Operators of air ambulances in and around the Lehigh Valley, matched in the receiver's ownOp field.
MEDICAL_OPERATORS = ("GEISINGER", "LIFE FLIGHT", "LIFEFLIGHT", "MEDEVAC", "MED EVAC", "AIR METHODS", "PHI ",
                     "PHI,", "LIFENET", "JEFFSTAT", "PENNSTAR", "LIFE LION", "HEALTH NETWORK", "ST LUKE",
                     "ST. LUKE", "HOSPITAL", "MEDICAL")


def is_helicopter(plane: dict) -> bool:
    return (plane.get("category") == "A7" or (plane.get("t") or "").upper() in HELICOPTER_TYPES
            or "HELICOPTER" in (plane.get("desc") or "").upper())


def is_medical(plane: dict) -> bool:
    operator = (plane.get("ownOp") or "").upper()
    return any(word in operator for word in MEDICAL_OPERATORS)


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
        self._landed: set[tuple[str, int]] = set()
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
        with self._lock:
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
                    "operator": plane.get("ownOp") or "", "medical": is_medical(plane),
                    "name": name or "", "watched": name is not None,
                    "lat": lat, "lon": lon, "alt_ft": altitude_ft(plane), "speed_kt": plane.get("gs"),
                    "track": plane.get("track"), "seen": now - plane.get("seen_pos", 0),
                })
                if not shown["trail"] or shown["trail"][-1] != [lat, lon]:
                    shown["trail"] = (shown["trail"] + [[lat, lon]])[-TRAIL_POINTS:]
                shown["near"] = self._near(shown, incidents)
                self._planes[hex_id] = shown
                if shown["near"] and shown["near"]["landing"]:
                    self._landing(shown, shown["near"])
            for hex_id in [h for h, p in self._planes.items() if now - p["seen"] > STALE_S]:
                del self._planes[hex_id]
        self.updated = now

    def aircraft(self) -> list[dict]:
        with self._lock:
            return [dict(p, trail=list(p["trail"])) for p in self._planes.values()]

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

    def _landing(self, plane: dict, near: dict):
        key = (plane["hex"], near["incident_id"])
        if key in self._landed:
            return
        self._landed.add(key)
        who = plane["name"] or plane["flight"] or plane["reg"] or plane["hex"]
        if plane["operator"] and not plane["name"]:
            who += f" ({plane['operator']})"
        log.info("Helicopter %s coming down %.1f mi from incident %d (%s)", who, near["miles"],
                 near["incident_id"], near["label"])
        if self.alerter and self.settings.alert_landing:
            where = " ".join(filter(None, (near["nature"], near["label"])))
            self.alerter.send("aircraft", f"Helicopter landing: {who}",
                              f"{near['miles']} mi from {where or 'an incident'}", tags=["helicopter"],
                              priority=4, click=f"{self.alerter.base_url}/map" if self.alerter.base_url else None,
                              incident_id=near["incident_id"])
