"""Watch zones: any call within a set distance of a chosen spot alerts, whatever the call is about.

A zone is set in config.toml under [alerts] zones with a centre (lat and lon, or an address looked up
once online) and a radius. A call alerts when the map places it, or the incident it joins, inside
the circle. A call that names a road without placing it ("Concord Circle", with no house number or cross
street), or that joined an incident elsewhere, alerts when that road lies wholly inside the circle: a short local road there
can only mean somewhere in the zone, while a long road passing through could be anywhere along it.
"""

import logging
import math
from dataclasses import dataclass, field

from .addresses import Gazetteer, words
from .incidents import distance_m

log = logging.getLogger(__name__)

METRES_PER_MILE = 1609.344
# A road counts as inside when every bit of it is within the radius plus this much.
ROAD_SLACK_M = 40.0


@dataclass
class Zone:
    name: str
    radius_m: float
    prefix: str
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    looked_up: bool = False
    roads: list = field(default_factory=list)          # streets wholly inside, for a call naming only a road
    nearby: list[str] = field(default_factory=list)    # names of every road reaching into the circle


def read_zones(entries: list) -> list[Zone]:
    zones = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"[alerts] zones entry {number}: expected {{name = ..., lat = ..., lon = ...}}")
        unknown = set(entry) - {"name", "address", "lat", "lon", "radius_miles", "prefix"}
        if unknown:
            raise ValueError(f"[alerts] zones entry {number}: unknown setting {', '.join(sorted(unknown))}")
        lat, lon = entry.get("lat"), entry.get("lon")
        if (lat is None) != (lon is None) or (lat is None and not entry.get("address")):
            raise ValueError(f"[alerts] zones entry {number}: give lat and lon, or an address")
        zones.append(Zone(name=str(entry.get("name") or entry.get("address") or f"Zone {number}"),
                          radius_m=float(entry.get("radius_miles", 0.5)) * METRES_PER_MILE,
                          prefix=str(entry.get("prefix") or "").strip(),
                          address=str(entry.get("address") or ""),
                          lat=None if lat is None else float(lat), lon=None if lon is None else float(lon)))
    return zones


class ZoneWatch:
    def __init__(self, zones: list[Zone], gazetteer: Gazetteer, geocoder=None):
        self.zones = zones
        self.gazetteer = gazetteer
        self.geocoder = geocoder
        self._streets_seen = None

    def _centre(self, zone: Zone) -> bool:
        if zone.lat is None and not zone.looked_up and self.geocoder:
            zone.looked_up = True
            result = self.geocoder.lookup(f"{zone.address}, Pennsylvania")
            if result:
                zone.lat, zone.lon = result["lat"], result["lon"]
                log.info("Zone %s: %s is at %.5f, %.5f", zone.name, zone.address, zone.lat, zone.lon)
            else:
                log.warning("Zone %s: could not look up %s; set lat and lon in config.toml", zone.name,
                            zone.address)
            self._streets_seen = None
        return zone.lat is not None

    def _refresh_roads(self):
        """Works out each zone's roads again when the street list was reloaded."""
        streets = self.gazetteer.streets
        if streets is self._streets_seen:
            return
        self._streets_seen = streets
        proj = self.gazetteer.proj
        for zone in self.zones:
            zone.roads, zone.nearby = [], []
            if zone.lat is None:
                continue
            cx, cy = proj.xy(zone.lat, zone.lon)
            near = set()
            for street in streets:
                points = self.gazetteer.points(street)
                if not points:
                    continue
                far = max(math.hypot(x - cx, y - cy) for x, y in points)
                close = min(math.hypot(x - cx, y - cy) for x, y in points)
                if close <= zone.radius_m:
                    near.add(street.name)
                    if far <= zone.radius_m + ROAD_SLACK_M:
                        zone.roads.append(street)
            zone.nearby = sorted(near)
            log.info("Zone %s: %d roads within %.2f mi: %s", zone.name, len(zone.nearby),
                     zone.radius_m / METRES_PER_MILE, ", ".join(zone.nearby) or "none (no road shapes?)")

    def check(self, call: dict, incident: dict | None) -> tuple[Zone, str, dict | None] | None:
        """(zone, where, the incident when it is in the zone) for the first zone this call is in, or None.
        A call naming a road in a zone counts even when it joined an incident elsewhere."""
        for zone in self.zones:
            self._centre(zone)
        self._refresh_roads()
        for zone in self.zones:
            if zone.lat is None:
                continue
            if incident and incident.get("lat") is not None:
                d = distance_m(zone.lat, zone.lon, incident["lat"], incident["lon"])
                if d <= zone.radius_m:
                    where = incident.get("label") or "on the map"
                    return zone, f"{where} ({d / METRES_PER_MILE:.1f} mi away)", incident
            road = self._road_named(zone, call.get("transcript") or "")
            if road:
                return zone, road, None
        return None

    def _road_named(self, zone: Zone, text: str) -> str | None:
        if not zone.roads:
            return None
        w = words(text)
        towns = self.gazetteer.towns_in(w)
        for mention in self.gazetteer.mentions(w):
            # With its suffix ("Concord Circle", not just "Concord"), so a common word isn't a road.
            if not mention.strong:
                continue
            # "Concord Drive in Upper Saucon" is another Concord Drive than the one in the zone.
            for street in mention.streets:
                if towns and street.town_key not in towns:
                    continue
                if street in zone.roads:
                    return street.name + (f", {street.town}" if street.town else "")
        return None
