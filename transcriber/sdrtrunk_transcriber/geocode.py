"""Online address lookup with OpenStreetMap's free Nominatim search, used for house-number addresses.

Nominatim's rules for free use: at most one request a second, a User-Agent naming the program, and
cache results. Every answer (including "not found") is kept in the call log database, so an address
is looked up once. Results are limited to the map area.
"""

import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


class Geocoder:
    def __init__(self, db, area: list[float], url: str = NOMINATIM_URL, contact: str = "", session=None,
                 min_interval_s: float = 1.1):
        self.db = db
        self.area = area
        self.url = url
        self.contact = contact
        self.session = session or requests.Session()
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()
        self.failures = 0

    def lookup(self, query: str) -> dict | None:
        """{"lat", "lon", "kind", "display"} for a free-text address, or None. kind is "address" when
        Nominatim found the house, "street" when it only found the street."""
        key = query.strip().lower()
        cached = self.db.geocode_cached(key)
        if cached is not None:
            return cached or None
        result = self._ask(query)
        if result is False:
            return None              # network trouble: don't cache, try again next time
        self.db.geocode_store(key, result)
        return result or None

    def _ask(self, query: str):
        south, west, north, east = self.area
        with self._lock:
            wait = self._last + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                agent = "sdrtrunk-transcriber/1.0 (local incident map" + (f"; {self.contact}" if self.contact else "") + ")"
                response = self.session.get(self.url, timeout=15, headers={"User-Agent": agent}, params={
                    "q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us",
                    "viewbox": f"{west},{north},{east},{south}", "bounded": 1, "addressdetails": 0,
                })
                response.raise_for_status()
                rows = response.json()
            except Exception as e:
                self.failures += 1
                log.warning("Address lookup for %r failed: %s", query, e)
                return False
        self.failures = 0
        if not rows:
            return {}
        row = rows[0]
        house = row.get("addresstype") in ("building", "house", "place") or row.get("category") == "building" \
            or row.get("type") in ("house", "building", "residential", "yes")
        if row.get("category") == "highway" or row.get("class") == "highway":
            house = False
        return {"lat": float(row["lat"]), "lon": float(row["lon"]), "kind": "address" if house else "street",
                "display": row.get("display_name", "")[:200]}
