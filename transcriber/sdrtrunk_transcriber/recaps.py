"""12-hour summaries: twice a day (7 AM and 7 PM by default) a recap of everything the map saw since.

Counts by type and town, tone-outs and medevac flights are worked out here; the local Ollama model
from [summaries] then writes a few sentences about the most important incidents. Without Ollama the
recap still has the counts and the list. Recaps are kept in their own table with what they need to
show (place, time, summary) copied in, so they stay readable after the map is rebuilt.

A phone alert ([alerts] set up) lists only the important incidents and those close to home; a quiet
12 hours with nothing like that sends nothing.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import requests

from .aircraft import decide_kind, incident_helicopters
from .config import Config
from .db import Database
from .incidents import distance_m, services_of
from .summaries import SYSTEM, NotReady, OllamaClient, clip_words

log = logging.getLogger(__name__)

METRES_PER_MILE = 1609.344
TYPE_NAMES = {"fire": "fire", "ems": "EMS", "police": "police", "mva": "MVA", "other": "other"}
TYPE_ORDER = ("fire", "mva", "ems", "police", "other")
# Emoji in front of each line of the phone alert.
TYPE_ICONS = {"fire": "🔥", "mva": "🚗", "ems": "🚑", "police": "🚓", "other": "📻"}
# Make the scheduled recap this long after its time, so the last calls are transcribed and summarized.
SETTLE_S = 120
# After a restart, a recap missed by less than this is still made.
CATCH_UP_S = 6 * 3600
HIGHLIGHTS = 6
RECAP_WORDS = 90
# The same helicopter landing again this soon is the same flight.
FLIGHT_GAP_S = 1800

RECAP_SCHEMA = {"type": "object", "properties": {"recap": {"type": "string"}}, "required": ["recap"]}

RECAP_PROMPT = (
    "Radio traffic in Northampton and Lehigh County from {start} to {end} ({hours} hours).\n"
    "Counts: {counts}.\n{extra}\n"
    "Incidents, most important first (time, type, place, how many calls, latest status, summary, updates):\n"
    "{incidents}\n\n"
    "Write \"recap\": 3 to 5 short sentences, at most {words} words, for someone who missed these hours. Say how "
    "busy it was, describe the most important incidents and how they ended, and anything unusual. Use only "
    "what is listed above; leave out routine calls."
)


def type_of(incident: dict) -> str:
    """What the map colors and filters by (server.py does the same)."""
    return "mva" if incident.get("nature") == "MVA" else incident.get("category") or "other"


def town_of(label: str | None) -> str | None:
    """"1234 Northampton St, Easton" -> "Easton"."""
    if not label or "," not in label:
        return None
    town = label.rsplit(",", 1)[1].strip()
    return town if town and not town[:1].isdigit() else None


def clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%I:%M %p").lstrip("0")


def slot_name(t: float) -> str:
    """"7 AM", "7:30 PM"."""
    moment = datetime.fromtimestamp(t)
    return moment.strftime("%I:%M %p" if moment.minute else "%I %p").lstrip("0")


def parse_times(values) -> list[tuple[int, int]]:
    found = []
    for value in values:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError(f"[recaps] times: {value!r} is not a time like \"07:00\" or \"19:00\"")
        found.append((int(match[1]), int(match[2])))
    if not found:
        raise ValueError("[recaps] times: give at least one time, like [\"07:00\", \"19:00\"]")
    return sorted(set(found))


def last_slot(times: list[tuple[int, int]], now: float) -> float:
    """The latest scheduled time at or before now (local time)."""
    today = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    for days_back in range(0, 3):
        day = today - timedelta(days=days_back)
        for hour, minute in reversed(times):
            moment = day.replace(hour=hour, minute=minute).timestamp()
            if moment <= now:
                return moment
    raise AssertionError("no time in three days")


def next_slot(times: list[tuple[int, int]], now: float) -> float:
    today = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    for days_ahead in range(0, 3):
        day = today + timedelta(days=days_ahead)
        for hour, minute in times:
            moment = day.replace(hour=hour, minute=minute).timestamp()
            if moment > now:
                return moment
    raise AssertionError("no time in three days")


class RecapMaker:
    def __init__(self, config: Config, db: Database, alerter=None, session=None):
        self.config = config
        self.settings = config.recaps
        self.db = db
        self.alerter = alerter
        self.times = parse_times(self.settings.times)
        self.important = {n.strip().lower() for n in self.settings.important}
        self.important_tags = {t.strip().lower() for t in self.settings.important_tags}
        self.home = self._home()
        self.client = None
        if config.summaries.enabled:
            self.client = OllamaClient(config.summaries.url, config.summaries.model, config.summaries.timeout_s,
                                       config.summaries.keep_alive, session)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="recaps", daemon=True)

    def _home(self) -> tuple[float, float] | None:
        """Where "close" is measured from: [recaps] home_lat/home_lon, else the first watch zone."""
        if self.settings.home_lat or self.settings.home_lon:
            return self.settings.home_lat, self.settings.home_lon
        for zone in self.config.alerts.zones:
            if isinstance(zone, dict) and zone.get("lat") is not None and zone.get("lon") is not None:
                return float(zone["lat"]), float(zone["lon"])
        return None

    # ---- the schedule

    def start(self):
        self._thread.start()

    def stop(self, timeout: float = 10):
        self._stop.set()
        self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.make_due(time.time())
            except Exception:
                log.exception("12-hour summary failed")
            due = next_slot(self.times, time.time() - SETTLE_S) + SETTLE_S
            # Wake at least every minute, so a PC clock change or sleep doesn't skip one.
            self._stop.wait(max(1.0, min(60.0, due - time.time())))

    def make_due(self, now: float) -> dict | None:
        """Makes the scheduled recap for the latest time passed, unless it was made already."""
        slot = last_slot(self.times, now - SETTLE_S)
        if now - slot > CATCH_UP_S or self.db.recap_for_slot(slot):
            return None
        return self.make(slot - self.settings.hours * 3600, slot, kind="scheduled", alert=True)

    # ---- making one

    def make(self, start: float, end: float, kind: str = "manual", alert: bool = False) -> dict:
        with self._lock:
            if kind == "scheduled" and self.db.recap_for_slot(end):
                return self.db.recap_for_slot(end)
            facts = self.gather(start, end)
            text, model, ms, problem = self.write(facts)
            recap = {"kind": kind, "start": int(start), "end": int(end), "made_at": int(time.time()),
                     "text": text, "model": model, "ms": ms, "problem": problem,
                     "counts": json.dumps(facts["counts"]), "highlights": json.dumps(facts["highlights"]),
                     "alerted": 0}
            recap["id"] = self.db.add_recap(recap)
            shown = to_api(recap)
            log.info("12-hour summary for %s: %s", slot_name(end), plain_text(shown).replace("\n", " / "))
            if alert and self.alerter and self.settings.phone_alert:
                if self.send_alert(shown):
                    self.db.mark_recap_alerted(recap["id"])
            return shown

    def gather(self, start: float, end: float) -> dict:
        """The counts and the incidents worth a mention, from the map, summaries and helicopters."""
        db = self.db
        incidents = [i for i in db.incidents(int(start), int(end), limit=2000) if i["calls"]]
        ids = [i["id"] for i in incidents]
        summaries = db.summaries("incident", ids)
        updates = db.incident_updates(ids)
        helicopters = incident_helicopters(db, ids, self.config.aircraft.learn_landings)
        by_type: dict[str, int] = {}
        by_nature: dict[str, int] = {}
        by_town: dict[str, int] = {}
        rows = []
        for incident in incidents:
            kind = type_of(incident)
            by_type[kind] = by_type.get(kind, 0) + 1
            if incident["nature"]:
                by_nature[incident["nature"]] = by_nature.get(incident["nature"], 0) + 1
            town = town_of(incident["label"])
            if town:
                by_town[town] = by_town.get(town, 0) + 1
            stored = summaries.get(incident["id"]) or {}
            tags = [t for t in (stored.get("tags") or "").split(",") if t]
            medevac = [h for h in helicopters.get(incident["id"], []) if h["kind"] == "medevac"]
            calls = len({c["id"] for c in incident["calls"]})
            services = services_of(incident["calls"])
            miles = None
            if self.home:
                miles = distance_m(self.home[0], self.home[1], incident["lat"], incident["lon"]) / METRES_PER_MILE
            important = self.is_important(incident, tags, medevac, calls, services)
            close = miles is not None and miles <= self.settings.close_miles
            score = (calls + 4 * len(services) + (25 if important else 0) + (15 if close else 0)
                     + 10 * len(medevac))
            rows.append({
                "incident_id": incident["id"], "time": incident["first_time"], "last_time": incident["last_time"],
                "type": kind, "nature": incident["nature"], "label": incident["label"],
                "lat": incident["lat"], "lon": incident["lon"], "calls": calls, "services": services,
                "summary": stored.get("summary"), "status": stored.get("status"), "tags": tags,
                "updates": [u["note"] for u in updates.get(incident["id"], [])][-3:],
                "medevac": [h["text"] for h in medevac],
                "miles": None if miles is None else round(miles, 1),
                "important": important, "close": close, "score": score,
            })
        rows.sort(key=lambda r: (-r["score"], -r["time"]))
        flights = self.medevac_flights(start, end)
        pages = db.pages(start, end, limit=1000)
        counts = {
            "incidents": len(incidents),
            "types": {k: by_type[k] for k in TYPE_ORDER if k in by_type},
            "natures": dict(sorted(by_nature.items(), key=lambda kv: -kv[1])[:8]),
            "towns": dict(sorted(by_town.items(), key=lambda kv: -kv[1])[:5]),
            "pages": len(pages), "medevac_flights": flights,
            "calls": db.count_calls(start, end),
        }
        picked = [r for r in rows if r["important"] or r["close"]][:HIGHLIGHTS]
        # Fill up with the busiest others, so a quiet night still lists what happened.
        for row in rows:
            if len(picked) >= HIGHLIGHTS:
                break
            if row not in picked and row["calls"] >= 3:
                picked.append(row)
        picked.sort(key=lambda r: (-r["score"], -r["time"]))
        return {"start": start, "end": end, "counts": counts, "highlights": picked, "all": rows}

    def is_important(self, incident: dict, tags: list[str], medevac: list, calls: int, services: list) -> bool:
        nature = (incident["nature"] or "").lower()
        if nature in self.important:
            # A crash is only big news with injuries, entrapment, a closed road or a helicopter.
            if nature != "mva" or medevac or {"injuries", "entrapment", "fatality", "road blocked"} & set(tags):
                return True
        if self.important_tags & {t.lower() for t in tags}:
            return True
        if medevac:
            return True
        # A long incident that police, fire and EMS all worked.
        return calls >= self.settings.important_calls and len(services) >= 2

    def medevac_flights(self, start: float, end: float) -> int:
        flights = 0
        last_by_hex: dict[str, float] = {}
        for landing in self.db.landings_between(start, end):
            kind, _how = decide_kind(dict(landing, kind=landing["hand_kind"]), self.config.aircraft.learn_landings)
            if kind != "medevac":
                continue
            if landing["landing_time"] - last_by_hex.get(landing["hex"], -1e12) > FLIGHT_GAP_S:
                flights += 1
            last_by_hex[landing["hex"]] = landing["landing_time"]
        return flights

    def write(self, facts: dict) -> tuple[str, str | None, int | None, str | None]:
        """The model's few sentences, or "" when there is nothing to tell or no model."""
        if not facts["counts"]["incidents"] or self.client is None:
            return "", None, None, None
        lines = []
        for row in facts["all"][:15]:
            parts = [f"{clock(row['time'])} {row['nature'] or TYPE_NAMES[row['type']]}",
                     row["label"] or "place unknown", f"{row['calls']} calls"]
            if row["status"]:
                parts.append(row["status"])
            line = "- " + ", ".join(parts)
            if row["summary"]:
                line += f": {row['summary']}"
            if row["updates"]:
                line += " Updates: " + " ".join(row["updates"])
            if row["medevac"]:
                line += " " + "; ".join(row["medevac"]) + "."
            lines.append(line)
        counts = facts["counts"]
        extra = []
        if counts["pages"]:
            extra.append(f"{counts['pages']} tone-outs.")
        if counts["medevac_flights"]:
            extra.append(f"{counts['medevac_flights']} medevac helicopter landings.")
        if counts["towns"]:
            extra.append("Busiest places: " + ", ".join(f"{t} ({n})" for t, n in counts["towns"].items()) + ".")
        prompt = RECAP_PROMPT.format(
            start=datetime.fromtimestamp(facts["start"]).strftime("%a %I:%M %p"),
            end=datetime.fromtimestamp(facts["end"]).strftime("%a %I:%M %p"),
            hours=round((facts["end"] - facts["start"]) / 3600), counts=count_line(counts),
            extra=" ".join(extra), incidents="\n".join(lines), words=RECAP_WORDS)
        started = time.perf_counter()
        try:
            answer = self.client.ask(prompt, RECAP_SCHEMA, system=SYSTEM)
        except (requests.RequestException, NotReady, ValueError) as e:
            log.warning("12-hour summary: Ollama could not write it (%s); counts only", e)
            return "", None, None, "Ollama was not reachable, so this one has counts only."
        return (clip_words(answer.get("recap", ""), RECAP_WORDS), self.client.model,
                int((time.perf_counter() - started) * 1000), None)

    # ---- the phone

    def send_alert(self, recap: dict) -> bool:
        """Only the important and close incidents. Nothing like that: no alert."""
        lines = []
        for row in recap["highlights"]:
            if not (row["important"] or row["close"]):
                continue
            what = row["nature"] or TYPE_NAMES[row["type"]].capitalize()
            line = f"{TYPE_ICONS.get(row['type'], '📻')} {clock(row['time'])} {what}"
            if row["label"]:
                line += f", {row['label']}"
            if row["close"] and row["miles"] is not None:
                line += f" ({row['miles']:g} mi from home)"
            note = (row["updates"] or [None])[-1] or (row["status"] if row["status"] not in (None, "unknown") else None)
            if note:
                line += f" - {note}"
            lines.append(line)
        if not lines:
            log.info("12-hour summary: nothing important or close, so no phone alert")
            return False
        base = self.alerter.base_url
        self.alerter.send("recap", f"12-hour summary · {slot_name(recap['end'])}",
                          "\n".join([count_line(recap["counts"]), *lines]), tags=["clipboard"], priority=3,
                          click=f"{base}/map?recap={recap['id']}" if base else None)
        return True


def count_line(counts: dict) -> str:
    """"31 incidents: 9 EMS, 6 MVA, 4 fire, 3 police"."""
    total = counts.get("incidents", 0)
    if not total:
        return "No incidents"
    parts = [f"{n} {TYPE_NAMES.get(k, k)}" for k, n in counts.get("types", {}).items()]
    return f"{total} incident{'s' if total != 1 else ''}: " + ", ".join(parts)


def to_api(row: dict) -> dict:
    """A stored recap as the map page and SDRTrunk read it."""
    recap = dict(row)
    for name in ("counts", "highlights"):
        if isinstance(recap.get(name), str):
            recap[name] = json.loads(recap[name] or ("{}" if name == "counts" else "[]"))
    recap["title"] = (f"{datetime.fromtimestamp(recap['end']).strftime('%a %b')} "
                      f"{datetime.fromtimestamp(recap['end']).day}, {slot_name(recap['start'])} to "
                      f"{slot_name(recap['end'])}")
    recap["alerted"] = bool(recap.get("alerted"))
    recap["plain"] = plain_text(recap)
    return recap


def plain_text(recap: dict) -> str:
    """The whole recap as text, for SDRTrunk's Summaries tab and the log."""
    counts = recap["counts"]
    lines = [count_line(counts) + "."]
    if counts.get("calls"):
        lines[0] = lines[0][:-1] + f" ({counts['calls']} calls)."
    more = []
    if counts.get("pages"):
        more.append(f"{counts['pages']} tone-outs")
    if counts.get("medevac_flights"):
        more.append(f"{counts['medevac_flights']} medevac flight{'s' if counts['medevac_flights'] != 1 else ''}")
    if counts.get("towns"):
        more.append("busiest: " + ", ".join(f"{t} {n}" for t, n in counts["towns"].items()))
    if more:
        lines.append("; ".join(more).capitalize() + ".")
    if recap.get("text"):
        lines.append("")
        lines.append(recap["text"])
    if recap.get("highlights"):
        lines.append("")
        for row in recap["highlights"]:
            flag = "!" if row["important"] else "*" if row["close"] else "-"
            line = f"{flag} {clock(row['time'])} {row['nature'] or TYPE_NAMES[row['type']].capitalize()}"
            if row["label"]:
                line += f", {row['label']}"
            line += f" ({row['calls']} calls"
            if row["close"] and row["miles"] is not None:
                line += f", {row['miles']:g} mi from home"
            line += ")"
            if row.get("summary"):
                line += f": {row['summary']}"
            lines.append(line)
    if recap.get("problem"):
        lines.append("")
        lines.append(recap["problem"])
    return "\n".join(lines)
