"""Smart Fire Scan: which Fire Scan departments were toned out lately, so SDRTrunk can stay on them.

SDRTrunk's Fire Scan rotates one dongle over several fire departments. When dispatch tones one of them
out on Countywide, SDRTrunk asks /api/scan-focus and holds on that department's frequency for
[channels] toned_hold_minutes (rotating among only those when several were toned out).

A department counts as toned out when a tone-out in the window is named for its station ("Fire 25",
"Station 25"), or when dispatch says its station ("Station 25", "Forks Fire") on the tone-out's
channel right after the tones. EMS tones and EMS names don't count: EMS 27 is Suburban EMS, not
Palmer Fire.
"""

from __future__ import annotations

import re
import time

from .tones import STATION_NAME

# A tone-out's station name, as paging_tones.csv names fire stations.
FIRE_LABEL = re.compile(r"^\s*(?:fire|station|company|rescue)\s*#?\s*(\d{1,3})\b", re.IGNORECASE)
FIRE_WORDS = {"station", "fire", "company", "rescue"}
# What dispatch says after the tones that names the station (the tone-out call itself counts too).
SAID_BEFORE_S = 5
SAID_AFTER_S = 90


def _departments(channels) -> list[tuple[int, int, str]]:
    """(frequency Hz, station number, name) for each scan frequency with a station number."""
    found = []
    for mhz, number in (channels.toned_stations or {}).items():
        try:
            hz = int(round(float(mhz) * 1_000_000))
            station = int(number)
        except (TypeError, ValueError):
            continue
        found.append((hz, station, channels.name_for(hz) or f"Station {station}"))
    return found


def fire_stations_said(text: str, words: dict[str, int]) -> set[int]:
    """Fire station numbers named in a dispatch: "Station 25", "Fire 25", or a department word followed
    by "fire" ("Forks Fire", "Tatamy fire company")."""
    numbers = set()
    for m in STATION_NAME.finditer(text or ""):
        if m.group(1).lower() in FIRE_WORDS:
            numbers.add(int(m.group(2)))
    lowered = (text or "").lower()
    for word, number in words.items():
        if re.search(rf"\b{re.escape(word)}\s+(?:township\s+)?fire\b", lowered):
            numbers.add(number)
    return numbers


def toned_out(db, channels, now: float | None = None) -> dict:
    """The scan frequencies to stay on now, each with when its hold ends."""
    now = time.time() if now is None else now
    hold_s = max(0.0, float(channels.toned_hold_minutes)) * 60
    departments = _departments(channels)
    result = {"hold_minutes": hold_s / 60, "frequencies": []}
    if not departments or hold_s <= 0:
        return result

    by_station = {}
    for hz, station, name in departments:
        by_station.setdefault(station, []).append((hz, name))
    # "Forks Fire" -> "forks": the department's own word, for dispatch saying it by name.
    words = {}
    for hz, station, name in departments:
        word = name.split()[0].lower() if name and not FIRE_LABEL.match(name) else ""
        if word:
            words[word] = station

    since = now - hold_s
    pages = db.pages(since=since - SAID_AFTER_S, limit=1000)
    said = db.transcripts_between(since - SAID_AFTER_S - SAID_BEFORE_S, now + 1) if pages else []

    latest: dict[int, tuple[float, str]] = {}

    def toned(station: int, at: float, why: str):
        if station in by_station and at >= since and (station not in latest or at > latest[station][0]):
            latest[station] = (at, why)

    for page in pages:
        t = page["page_time"]
        m = FIRE_LABEL.match(page.get("label") or "")
        if m:
            toned(int(m.group(1)), t, page["label"])
        for call in said:
            if not (t - SAID_BEFORE_S <= call["call_time"] < t + SAID_AFTER_S):
                continue
            if page.get("channel") and (call["talkgroup_label"] or call["talkgroup"]) != page["channel"]:
                continue
            for station in fire_stations_said(call["transcript"], words):
                # The hold counts from when dispatch said it, or the tones if that is later.
                toned(station, max(t, call["call_time"]), "dispatch said Station %d" % station)

    for station, (at, why) in sorted(latest.items(), key=lambda kv: kv[1][0]):
        for hz, name in by_station[station]:
            result["frequencies"].append({
                "frequency": hz, "name": name, "station": station, "toned_at": at,
                "until": at + hold_s, "why": why})
    return result
