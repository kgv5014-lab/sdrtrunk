"""PennDOT crashes, closures, roadwork and traffic cameras from the public 511PA website, for the incident map.

511PA's map page reads the same data with no key: /map/mapIcons/{layer} lists every item's id and position
statewide, and /map/data/{layer}/{id} has the details. Only items inside [traffic] area are fetched in detail.
Camera stills are passed through this server. Live video is only handed out to 511pa.com's own pages, so the
map links to the camera's 511PA page for it. PennDOT allows showing cameras live but not recording them, so
nothing is saved.
"""

import logging
import threading
import time
from datetime import datetime

import requests

from .config import Config

log = logging.getLogger(__name__)

SITE = "https://www.511pa.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; sdrtrunk-transcriber)"}
# 511PA layer name -> what the map calls it.
EVENT_LAYERS = {
    "MajorRouteIncident": "incident", "OtherRouteIncident": "incident",
    "MajorRouteClosure": "closure", "OtherRouteClosure": "closure",
    "ActiveRoadwork": "roadwork",
}
# Event details are fetched again this often, since the icon list doesn't say what changed.
DETAIL_REFRESH_S = 15 * 60


def decode_polyline(text: str) -> list[list[float]]:
    """Google's encoded polyline format (5 decimal places)."""
    points, index, lat, lon = [], 0, 0, 0
    while index < len(text):
        values = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(text[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        lat += values[0]
        lon += values[1]
        points.append([lat / 1e5, lon / 1e5])
    return points


def _time(value) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except (TypeError, ValueError):
        return None


def _inside(location, area) -> bool:
    try:
        lat, lon = float(location[0]), float(location[1])
    except (TypeError, ValueError, IndexError):
        return False
    south, west, north, east = area
    return south <= lat <= north and west <= lon <= east


def event_from(layer: str, data: dict, location) -> dict:
    lat = data.get("latitude", location[0])
    lon = data.get("longitude", location[1])
    path = []
    if data.get("polyline"):
        try:
            path = decode_polyline(data["polyline"])
        except (IndexError, ValueError):
            path = []
    area = data.get("area") or {}
    county = ((area.get("areaName") or {}).get("text")) or ""
    kind = EVENT_LAYERS.get(layer, "incident")
    if data.get("isFullClosure"):
        kind = "closure"
    return {
        "id": str(data.get("id")), "layer": layer, "kind": kind,
        "major": layer.startswith("Major"),
        "type": data.get("eventType") or "", "roadway": data.get("roadway") or "",
        "direction": data.get("direction") or "", "description": (data.get("description") or "").strip(),
        "where": (data.get("locationDescription") or "").strip(), "lanes": data.get("laneDescription") or "",
        "full_closure": bool(data.get("isFullClosure")), "county": county,
        "start": _time(data.get("startDate")), "end": _time(data.get("endDate")),
        "updated": _time(data.get("lastUpdated")),
        "lat": float(lat), "lon": float(lon), "path": path,
    }


def camera_from(data: dict, location) -> dict:
    views = []
    for image in data.get("images") or []:
        if image.get("disabled") or image.get("blocked") or image.get("id") is None:
            continue
        views.append({
            "image_id": int(image["id"]), "description": image.get("description") or "",
            "live": bool(image.get("videoUrl")) and not image.get("videoDisabled"),
        })
    return {
        "id": str(data.get("id")), "roadway": data.get("roadway") or "", "name": data.get("location") or "",
        "lat": float(location[0]), "lon": float(location[1]), "views": views,
        # 511PA's map opened on this camera; its popup's Show Video plays the live stream.
        "page": f"{SITE}/map#camera-{data.get('id')}",
    }


class Traffic511:
    def __init__(self, config: Config, session=None):
        self.settings = config.traffic
        self.area = list(self.settings.area or config.map.area)
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self._lock = threading.Lock()
        self._events: dict[str, dict] = {}
        self._fetched: dict[str, float] = {}
        self._cameras: dict[str, dict] = {}
        self._cameras_at = 0.0
        self.error: str | None = None
        self.updated: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="traffic", daemon=True)

    def start(self):
        log.info("Watching 511PA traffic events%s", " and cameras" if self.settings.cameras else "")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll()
                if self.error:
                    log.info("511PA: reading traffic again")
                self.error = None
            except Exception as e:
                if self.error is None:
                    log.warning("511PA: could not read traffic (%s); retrying", e)
                self.error = str(e)[:200]
            self._stop.wait(max(30.0, self.settings.poll_s))

    def _get(self, path: str):
        reply = self.session.get(SITE + path, timeout=20)
        reply.raise_for_status()
        return reply.json()

    def _icons(self, layer: str) -> list[dict]:
        return [i for i in (self._get(f"/map/mapIcons/{layer}").get("item2") or [])
                if i.get("itemId") is not None and _inside(i.get("location"), self.area)]

    def poll(self, now: float | None = None):
        now = time.time() if now is None else now
        events = {}
        for layer in EVENT_LAYERS:
            for icon in self._icons(layer):
                item_id = str(icon["itemId"])
                key = f"{layer}/{item_id}"
                old = self._events.get(key)
                if old is None or now - self._fetched.get(key, 0) > DETAIL_REFRESH_S:
                    try:
                        old = event_from(layer, self._get(f"/map/data/{layer}/{item_id}"), icon["location"])
                        self._fetched[key] = now
                    except (requests.RequestException, ValueError, TypeError) as e:
                        log.debug("511PA: no details for %s (%s)", key, e)
                        if old is None:
                            continue
                events[key] = old
        with self._lock:
            self._events = events
            self._fetched = {k: v for k, v in self._fetched.items() if k in events}
        if self.settings.cameras and now - self._cameras_at > self.settings.cameras_refresh_hours * 3600:
            self._load_cameras()
            self._cameras_at = now
        self.updated = now

    def _load_cameras(self):
        cameras = {}
        for icon in self._icons("Cameras"):
            item_id = str(icon["itemId"])
            try:
                data = self._get(f"/map/data/Cameras/{item_id}")
            except (requests.RequestException, ValueError) as e:
                log.debug("511PA: no details for camera %s (%s)", item_id, e)
                if item_id in self._cameras:
                    cameras[item_id] = self._cameras[item_id]
                continue
            camera = camera_from(data, icon["location"])
            if camera["views"]:
                cameras[item_id] = camera
        with self._lock:
            self._cameras = cameras
        log.info("511PA: %d traffic cameras in the map area", len(cameras))

    def events(self) -> list[dict]:
        with self._lock:
            return sorted(self._events.values(), key=lambda e: (e["kind"] == "roadwork", -(e["updated"] or 0)))

    def cameras(self) -> list[dict]:
        with self._lock:
            return list(self._cameras.values())

    def known_image(self, image_id: int) -> bool:
        with self._lock:
            return any(v["image_id"] == image_id for c in self._cameras.values() for v in c["views"])

    def image(self, image_id: int) -> tuple[bytes, str]:
        """A camera's current still picture, passed through (not saved)."""
        reply = self.session.get(f"{SITE}/map/Cctv/{image_id}", timeout=15)
        reply.raise_for_status()
        return reply.content, reply.headers.get("content-type", "image/jpeg")
