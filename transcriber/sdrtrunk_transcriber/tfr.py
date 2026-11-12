"""FAA Temporary Flight Restrictions (TFRs) over a watch zone, so a zone alert only says "drone launch"
when no TFR covers the zone around that time.

The FAA's TFR site (tfr.faa.gov) publishes the shape of every current TFR as GeoJSON, and each TFR's
details (start and end times) as XML. The shapes are fetched at most every few minutes; the details only
for TFRs that reach the zone. Times in the details are UTC.

This is a convenience only, not a pre-flight airspace check: it doesn't know about controlled airspace
(ABE's is close by), NOTAMs other than TFRs, or anything the FAA site leaves out. Check B4UFLY or a
LAANC app before flying.
"""

import logging
import math
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

SHAPES_URL = ("https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0&request=GetFeature"
              "&typeName=TFR:V_TFR_LOC&maxFeatures=2000&outputFormat=application/json&srsname=EPSG:4326")
DETAIL_URL = "https://tfr.faa.gov/download/detail_{}.xml"
# How long fetched shapes and details are used before asking again.
SHAPES_KEEP_S = 300
DETAIL_KEEP_S = 1800
# After the FAA site couldn't be reached, don't try again for this long (an alert shouldn't wait on it).
RETRY_S = 60
TIMEOUT_S = 8
METRES_PER_DEGREE = 111_320.0


@dataclass
class Tfr:
    notam: str                 # "6/7276"
    title: str                 # "Bethlehem, PA, Tuesday, November 17, 2026 Local"
    kind: str                  # "SECURITY", "HAZARDS", "VIP", ...
    windows: list = field(default_factory=list)   # [(start, end)] as epoch seconds; end None = until further notice

    def window_near(self, when: float, margin_s: float):
        """The time window within margin_s of `when`, or None."""
        for start, end in self.windows:
            if (start is None or start - margin_s <= when) and (end is None or when <= end + margin_s):
                return start, end
        return None


@dataclass
class TfrResult:
    ok: bool                                      # False: the FAA site couldn't be read
    tfrs: list = field(default_factory=list)      # [(Tfr, (start, end))] covering the zone around that time

    def describe(self) -> str:
        parts = []
        for tfr, (start, end) in self.tfrs:
            span = f"{_clock(start) if start else 'now'} to {_clock(end) if end else 'until further notice'}"
            parts.append(f"TFR {tfr.notam} ({tfr.kind.title() or 'TFR'}), {span}")
        return "; ".join(parts)


def _clock(t: float) -> str:
    when = datetime.fromtimestamp(t)
    today = datetime.now().date()
    text = when.strftime("%I:%M %p").lstrip("0")
    return text if when.date() == today else when.strftime("%a %b %d ").replace(" 0", " ") + text


def parse_time(text: str | None) -> float | None:
    """'2026-11-14T07:00:00' (UTC) as epoch seconds."""
    if not text or not text.strip():
        return None
    try:
        return datetime.strptime(text.strip()[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_detail(xml_text: str) -> list[tuple]:
    """The time windows in a TFR's detail XML: each area's schedule, or else the whole TFR's dates.
    A TFR that can't be read is taken as in force (no window = always)."""
    root = ET.fromstring(xml_text)
    windows = []
    for element in root.iter():
        if _local(element.tag) == "ScheduleGroup":
            values = {_local(child.tag): child.text for child in element}
            if "dateEffective" in values or "dateExpire" in values:
                windows.append((parse_time(values.get("dateEffective")), parse_time(values.get("dateExpire"))))
    if not windows:
        for element in root.iter():
            if _local(element.tag) == "Not":
                values = {_local(child.tag): child.text for child in element}
                windows.append((parse_time(values.get("dateEffective")), parse_time(values.get("dateExpire"))))
                break
    return windows or [(None, None)]


def _rings(geometry: dict) -> list[list]:
    """Outer rings of a Polygon or MultiPolygon, as [(lon, lat), ...]."""
    kind, coords = (geometry or {}).get("type"), (geometry or {}).get("coordinates") or []
    if kind == "Polygon":
        return [coords[0]] if coords else []
    if kind == "MultiPolygon":
        return [polygon[0] for polygon in coords if polygon]
    return []


def ring_reaches_circle(ring: list, lat: float, lon: float, radius_m: float) -> bool:
    """Whether a polygon ([lon, lat] points) overlaps a circle: the centre is inside it, or an edge comes
    within the radius."""
    scale = math.cos(math.radians(lat))
    points = [((p[0] - lon) * METRES_PER_DEGREE * scale, (p[1] - lat) * METRES_PER_DEGREE) for p in ring]
    if len(points) < 3:
        return False
    inside = False
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        if (y1 > 0) != (y2 > 0) and 0 < x1 + (0 - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length))
        if math.hypot(x1 + t * dx, y1 + t * dy) <= radius_m:
            return True
    return inside


class TfrCheck:
    def __init__(self, session=None, margin_minutes: float = 30):
        self.session = session or requests.Session()
        self.margin_s = margin_minutes * 60
        self._lock = threading.Lock()
        self._shapes = None          # GeoJSON features
        self._shapes_at = 0.0
        self._failed_at = 0.0
        self._details = {}           # notam -> (fetched at, windows)

    def check(self, lat: float, lon: float, radius_m: float, when: float | None = None) -> TfrResult:
        """TFRs covering the circle within the margin of `when` (now by default)."""
        when = time.time() if when is None else when
        with self._lock:
            try:
                features = self._features()
                found = {}
                for feature in features:
                    props = feature.get("properties") or {}
                    notam = str(props.get("NOTAM_KEY") or feature.get("id", "").rsplit(".", 1)[-1]).split("-")[0]
                    if not notam or notam in found:
                        continue
                    if any(ring_reaches_circle(r, lat, lon, radius_m) for r in _rings(feature.get("geometry"))):
                        found[notam] = Tfr(notam, str(props.get("TITLE") or "").strip(),
                                           str(props.get("LEGAL") or "").strip())
                result = TfrResult(ok=True)
                for notam, tfr in found.items():
                    tfr.windows = self._windows(notam)
                    window = tfr.window_near(when, self.margin_s)
                    if window:
                        result.tfrs.append((tfr, window))
                return result
            except Exception as e:
                self._failed_at = time.time()
                log.warning("TFR check: couldn't read the FAA TFR list (%s)", e)
                return TfrResult(ok=False)

    def _features(self) -> list:
        now = time.time()
        if self._shapes is not None and now - self._shapes_at < SHAPES_KEEP_S:
            return self._shapes
        if now - self._failed_at < RETRY_S:
            if self._shapes is not None and now - self._shapes_at < 4 * SHAPES_KEEP_S:
                return self._shapes
            raise RuntimeError("tried less than a minute ago")
        reply = self.session.get(SHAPES_URL, timeout=TIMEOUT_S)
        reply.raise_for_status()
        features = reply.json().get("features")
        if not isinstance(features, list):
            raise ValueError("no features in the reply")
        self._shapes, self._shapes_at = features, now
        log.info("TFR check: %d TFR areas from the FAA", len(features))
        return features

    def _windows(self, notam: str) -> list:
        now = time.time()
        cached = self._details.get(notam)
        if cached and now - cached[0] < DETAIL_KEEP_S:
            return cached[1]
        try:
            reply = self.session.get(DETAIL_URL.format(notam.replace("/", "_")), timeout=TIMEOUT_S)
            reply.raise_for_status()
            windows = parse_detail(reply.text)
        except Exception as e:
            # The shape reaches the zone: without its times, count it as in force.
            log.warning("TFR check: couldn't read TFR %s's times (%s); counting it as active", notam, e)
            windows = [(None, None)]
        self._details[notam] = (now, windows)
        return windows
