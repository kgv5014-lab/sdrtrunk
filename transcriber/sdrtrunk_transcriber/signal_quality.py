"""Signal quality per dongle, sent by SDRTrunk at the top of every hour, and a phone alert when a dongle gets worse
than its own normal.

SDRTrunk posts JSON to /api/signal: key, hourStart (unix seconds) and rows, one per channel that ran on a dongle
(or one with channel null for a dongle with no channels): tuner, status, channel, decoder, frequency, minutes,
messagesGood, messagesBad, goodPercent, syncLostSeconds, signalDb, noiseDb, ppm, ppmChange, channelErrorHz,
usbErrors.

"Normal" is the median of the same dongle and channel over the last few days, so a dongle that always sits at
90% isn't flagged while one that drops from 98% to 80% is. Each problem alerts once, and again only after an
hour without it.
"""

import logging
import statistics
from dataclasses import dataclass

from .config import SignalConfig
from .db import Database

log = logging.getLogger(__name__)

# Hours where a channel ran less than this many minutes are too short to judge or to count as normal.
MIN_MINUTES = 30

# What to check for each kind of problem, in plain words.
ADVICE = {
    "decode": "Check the antenna, its cable and connectors.",
    "sync": "Check the antenna, its cable and connectors.",
    "signal": "Check the antenna, its cable and connectors.",
    "noise": "Something nearby may be causing interference, or the gain changed.",
    "drift": "The dongle's frequency is drifting; it may be overheating.",
    "usb": "Check the USB cable, hub and power.",
    "error": "SDRTrunk stopped the dongle with an error; check its USB connection.",
    "missing": "SDRTrunk no longer sees the dongle; check its USB connection.",
}


@dataclass
class Problem:
    tuner: str
    channel: str  # '' for the whole dongle
    kind: str
    text: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.tuner, self.channel, self.kind


def parse_rows(body: dict) -> tuple[int, list[dict]]:
    """Reads SDRTrunk's JSON into signal_hours rows. Raises ValueError on a body that can't be used."""
    try:
        hour_start = int(body.get("hourStart"))
    except (TypeError, ValueError):
        raise ValueError("Report needs hourStart")
    rows = []
    for item in body.get("rows") or []:
        if not isinstance(item, dict) or not str(item.get("tuner") or "").strip():
            continue

        def number(name, kind=float):
            value = item.get(name)
            try:
                return kind(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        rows.append({
            "hour_start": hour_start,
            "tuner": str(item["tuner"]).strip()[:100],
            "status": str(item.get("status") or "")[:20],
            "channel": str(item.get("channel") or "").strip()[:100],
            "decoder": str(item.get("decoder") or "")[:20],
            "frequency": number("frequency", int),
            "minutes": number("minutes", int) or 0,
            "messages_good": number("messagesGood", int) or 0,
            "messages_bad": number("messagesBad", int) or 0,
            "good_percent": number("goodPercent"),
            "sync_lost_s": number("syncLostSeconds") or 0.0,
            "signal_db": number("signalDb"),
            "noise_db": number("noiseDb"),
            "ppm": number("ppm"),
            "ppm_change": number("ppmChange") or 0.0,
            "channel_error_hz": number("channelErrorHz", int),
            "usb_errors": number("usbErrors", int) or 0,
        })
    return hour_start, rows


def _median(rows: list[dict], name: str, minimum: int) -> float | None:
    values = [row[name] for row in rows if row.get(name) is not None]
    return statistics.median(values) if len(values) >= minimum else None


def _where(row: dict) -> str:
    return row["channel"] or row["tuner"]


def find_problems(rows: list[dict], history: list[dict], previous: list[dict], settings: SignalConfig) -> list[Problem]:
    """Compares one hour's rows with the dongle's own past.
    history: earlier hours to take "normal" from. previous: the report just before this one, to see dongles that
    went missing."""
    problems = []
    need = max(settings.min_history_hours, 1)
    past: dict[tuple[str, str], list[dict]] = {}
    for row in history:
        if row["minutes"] >= MIN_MINUTES or not row["channel"]:
            past.setdefault((row["tuner"], row["channel"]), []).append(row)

    tuner_rows: dict[str, list[dict]] = {}
    for row in rows:
        tuner_rows.setdefault(row["tuner"], []).append(row)

    for tuner, mine in tuner_rows.items():
        first = mine[0]
        if first["status"].lower() == "error":
            problems.append(Problem(tuner, "", "error", "SDRTrunk shows this dongle in an error state."))
            continue

        # The dongle as a whole: drift and USB errors repeat on each of its channels.
        tuner_past = [r for r in history if r["tuner"] == tuner]
        hours = {r["hour_start"]: r for r in tuner_past}.values()
        if first["ppm"] is not None:
            normal_ppm = _median(list(hours), "ppm", need)
            if abs(first["ppm_change"]) >= settings.ppm_jump:
                problems.append(Problem(tuner, "", "drift", f"Frequency correction moved {first['ppm_change']:+.2f} "
                                                            f"ppm this hour, to {first['ppm']:+.2f} ppm."))
            elif normal_ppm is not None and abs(first["ppm"] - normal_ppm) >= settings.ppm_drift:
                problems.append(Problem(tuner, "", "drift", f"Frequency correction is {first['ppm']:+.2f} ppm, "
                                                            f"normally {normal_ppm:+.2f} ppm."))
        if settings.usb_errors > 0 and first["usb_errors"] >= settings.usb_errors:
            problems.append(Problem(tuner, "", "usb", f"{first['usb_errors']} USB errors this hour."))

        for row in mine:
            if not row["channel"] or row["minutes"] < MIN_MINUTES:
                continue
            normal = past.get((tuner, row["channel"]), [])
            where = _where(row)

            base = _median(normal, "good_percent", need)
            if row["good_percent"] is not None and base is not None \
                    and base - row["good_percent"] >= settings.decode_drop_points:
                problems.append(Problem(tuner, row["channel"], "decode",
                                        f"{where}: decoded {row['good_percent']:.0f}% of messages, normally "
                                        f"{base:.0f}%."))

            lost = row["sync_lost_s"] or 0.0
            if lost >= settings.sync_lost_minutes * 60 and len(normal) >= need:
                base = statistics.median(r["sync_lost_s"] or 0.0 for r in normal)
                if lost >= 3 * base:
                    problems.append(Problem(tuner, row["channel"], "sync",
                                            f"{where}: lost the control channel for {lost / 60:.0f} min this hour, "
                                            f"normally {base / 60:.1f} min."))

            base = _median(normal, "signal_db", need)
            if row["signal_db"] is not None and base is not None and base - row["signal_db"] >= settings.signal_drop_db:
                problems.append(Problem(tuner, row["channel"], "signal",
                                        f"{where}: signal {base - row['signal_db']:.0f} dB weaker than normal "
                                        f"({row['signal_db']:.0f} dB, normally {base:.0f} dB)."))

            base = _median(normal, "noise_db", need)
            if row["noise_db"] is not None and base is not None and row["noise_db"] - base >= settings.noise_rise_db:
                problems.append(Problem(tuner, row["channel"], "noise",
                                        f"{where}: noise {row['noise_db'] - base:.0f} dB higher than normal "
                                        f"({row['noise_db']:.0f} dB, normally {base:.0f} dB)."))

    # Dongles that ran channels in the report before and are gone from this one.
    for row in previous:
        if row["channel"] and row["minutes"] >= MIN_MINUTES and row["tuner"] not in tuner_rows \
                and not any(p.key == (row["tuner"], "", "missing") for p in problems):
            problems.append(Problem(row["tuner"], "", "missing", "The dongle is gone from SDRTrunk's tuner list."))
    return problems


class SignalWatch:
    def __init__(self, settings: SignalConfig, db: Database, alerter=None):
        self.settings = settings
        self.db = db
        self.alerter = alerter if settings.alert else None

    def report(self, body: dict) -> dict:
        hour_start, rows = parse_rows(body)
        if not rows:
            return {"stored": 0, "problems": []}
        history = self.db.signal_hours(int(hour_start - self.settings.baseline_days * 86400), hour_start)
        last = self.db.previous_signal_hour(hour_start)
        previous = [r for r in history if r["hour_start"] == last] if last and hour_start - last <= 2 * 3600 else []
        self.db.store_signal_hours(rows, hour_start - self.settings.keep_days * 86400)

        problems = find_problems(rows, history, previous, self.settings)
        known = self.db.signal_problems()
        new = [p for p in problems if p.key not in known]
        self.db.set_signal_problems({p.key for p in problems}, {r["tuner"] for r in rows}, hour_start)
        for problem in problems:
            log.warning("Signal quality: %s: %s", problem.tuner, problem.text)

        by_tuner: dict[str, list[Problem]] = {}
        for problem in new:
            by_tuner.setdefault(problem.tuner, []).append(problem)
        for tuner, found in by_tuner.items():
            advice = list(dict.fromkeys(ADVICE[p.kind] for p in found))
            message = "\n".join([p.text for p in found] + advice)
            if self.alerter:
                self.alerter.send("signal", f"Dongle problem: {tuner}", message, tags=["satellite_antenna"],
                                  priority=3, page_label=tuner)
        return {"stored": len(rows), "problems": [p.text for p in problems], "alerted": len(by_tuner)}
