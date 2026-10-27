"""Finds two-tone fire and EMS pages in call recordings.

SDRTrunk's own paging decoder only hears analog channels. On a P25 talkgroup the tones come through as
digital voice, so they are only in the call recordings SDRTrunk uploads. This looks for them there, in
the audio as uploaded (before audio.clean() cuts tones out for Whisper), the same way SDRTrunk's
TwoToneDetector does: tone A for 0.3 to 5 s, at most 0.3 s gap, then a different tone B; or one
steady tone of 6 s and more (an all-call).

Names come from paging_tones.csv, the same format as SDRTrunk's list: tone_a,tone_b,name. Tones not
on the list are named by themselves when dispatch keeps naming the same station right after them
(ToneFinder.learn).
"""

import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# 64 ms frames, 32 ms apart, zero-padded to ~2 Hz bins.
BLOCK_S = 0.064
HOP_S = 0.032
FFT_FACTOR = 8
# Paging tones run from about 280 Hz to 3 kHz. The low edge stays clear of CTCSS tones (up to 254 Hz).
BAND_HZ = (270.0, 3200.0)
# A frame is a tone when this share of the band's energy is in the peak's main lobe.
MIN_PURITY = 0.5
QUIET_DB = -55.0
# A frame may differ from its segment by this fraction (or MIN_SAME_HZ if larger).
SAME_TONE_FRACTION = 0.012
MIN_SAME_HZ = 4.0
# Tone A and tone B must differ by at least this fraction.
DIFFERENT_TONE_FRACTION = 0.025
MAX_MISSED_FRAMES = 2
MIN_TONE_S = 0.3
MAX_TONE_A_S = 5.0
MAX_GAP_S = 0.3
LONG_TONE_S = 6.0
# Tones match a list entry when each is within this fraction, as in SDRTrunk.
MATCH_FRACTION = 0.015

# Learning names: what dispatch says from just before a tone-out until this long after it.
LEARN_BEFORE_S = 120
LEARN_AFTER_S = 90
# The same tones again this soon (dispatch often tones out twice) are the same tone-out.
SAME_PAGE_S = 120
LEARN_EVERY_S = 300
# "Station 20", "Fire 54", "EMS 27", "Medic 27", "Rescue 71"; Whisper writes EMS as "E-M-S" or "MS".
STATION_NAME = re.compile(r"\b(station|fire|company|ems|e-m-s|a?ms|medic|ambulance|rescue)[\s-]*#?\s*(\d{1,3})\b(?![\d:-])",
                          re.IGNORECASE)
# Four-digit call signs start with the station: "Engine 2011" and "Medic 2081" are Station 20.
CALL_SIGN = re.compile(r"\b(?:engine|medic|ladder|tanker|truck|rescue|squad|ambulance|tower|brush|utility|qrs|"
                       r"chief|ems)[\s-]*#?\s*(\d{2})-?(\d{2})\b(?![\d:-])", re.IGNORECASE)
STATION_WORDS = {"station": "Station", "fire": "Fire", "company": "Fire", "rescue": "Rescue"}


@dataclass
class Page:
    tone_a: float
    tone_b: float | None
    tone_a_seconds: float
    offset_s: float          # where tone A starts in the call


def _frames(samples: np.ndarray, rate: int) -> tuple[np.ndarray, float]:
    """Each frame's tone frequency (NaN when it isn't a clean tone), and the frame step in seconds."""
    block, hop = int(BLOCK_S * rate), int(HOP_S * rate)
    if len(samples) < block:
        return np.zeros(0), hop / rate
    size = block * FFT_FACTOR
    bin_hz = rate / size
    lobe = 2 * FFT_FACTOR
    count = 1 + (len(samples) - block) // hop
    found = np.full(count, np.nan)
    window = np.hanning(block)
    lo, hi = int(math.ceil(BAND_HZ[0] / bin_hz)), int(BAND_HZ[1] / bin_hz)
    # A few hundred frames at a time keeps memory small on long calls.
    for first in range(0, count, 256):
        index = np.arange(block)[None, :] + hop * np.arange(first, min(count, first + 256))[:, None]
        chunk = samples[index] * window
        power = np.abs(np.fft.rfft(chunk, n=size, axis=1)) ** 2
        band = power[:, lo:hi + 1]
        total = band.sum(axis=1)
        peak = band.argmax(axis=1) + lo
        rows = np.arange(len(chunk))
        cumulative = np.concatenate([np.zeros((len(chunk), 1)), power.cumsum(axis=1)], axis=1)
        tone = cumulative[rows, peak + lobe + 1] - cumulative[rows, np.maximum(peak - lobe, 0)]
        level_db = 10 * np.log10(np.maximum(total / (block * block * 0.375 / 4), 1e-20))
        ok = (tone / np.maximum(total, 1e-30) >= MIN_PURITY) & (level_db > QUIET_DB)
        # Parabolic interpolation on the log power of the peak and its neighbours.
        left = np.log(power[rows, peak - 1] + 1e-30)
        centre = np.log(power[rows, peak] + 1e-30)
        right = np.log(power[rows, peak + 1] + 1e-30)
        denominator = left - 2 * centre + right
        delta = np.where(denominator != 0, 0.5 * (left - right) / np.where(denominator != 0, denominator, 1), 0)
        found[first:first + len(chunk)] = np.where(ok, (peak + delta) * bin_hz, np.nan)
    return found, hop / rate


def find_pages(samples: np.ndarray, rate: int) -> list[Page]:
    """Two-tone pages and long tones in a recording."""
    frames, step = _frames(np.asarray(samples, dtype=np.float64), rate)
    pages: list[Page] = []
    segments = []        # [first frame, last frame, sum of frequency, frame count]
    current, missed = None, 0

    def frequency(segment):
        return segment[2] / segment[3]

    def seconds(segment):
        return (segment[1] - segment[0] + 1) * step

    for i, f in enumerate(np.append(frames, np.nan)):
        if not math.isnan(f) and current is not None:
            mean = frequency(current)
            if abs(f - mean) > max(MIN_SAME_HZ, mean * SAME_TONE_FRACTION):
                segments.append(current)
                current = None
        if math.isnan(f):
            if current is not None:
                missed += 1
                if missed > MAX_MISSED_FRAMES or i == len(frames):
                    segments.append(current)
                    current = None
            continue
        if current is None:
            current = [i, i, 0.0, 0]
        current[1], current[2], current[3] = i, current[2] + f, current[3] + 1
        missed = 0

    pending = None
    for segment in segments:
        if seconds(segment) < MIN_TONE_S:
            continue
        if pending is not None:
            gap = (segment[0] - pending[1] - 1) * step
            a, b = frequency(pending), frequency(segment)
            if gap <= MAX_GAP_S and abs(b - a) >= a * DIFFERENT_TONE_FRACTION:
                pages.append(Page(round(float(a), 1), round(float(b), 1), round(seconds(pending), 2),
                                  round(pending[0] * step, 2)))
                pending = None
                continue
        if seconds(segment) >= LONG_TONE_S:
            pages.append(Page(round(float(frequency(segment)), 1), None, round(seconds(segment), 2),
                              round(segment[0] * step, 2)))
            pending = None
        elif seconds(segment) <= MAX_TONE_A_S:
            pending = segment
        else:
            pending = None
    return pages


class ToneList:
    """paging_tones.csv: tone_a,tone_b,name. Re-read when the file changes."""

    def __init__(self, path: Path | None):
        self.path = path
        self._mtime = None
        self.entries: list[tuple[float, float | None, str]] = []
        self.reload_if_changed()

    def reload_if_changed(self):
        if self.path is None or not self.path.exists():
            self.entries = []
            return
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        self._mtime = mtime
        entries = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#") or line.lower().startswith("tone_a"):
                continue
            parts = [p.strip() for p in line.split(",", 2)]
            try:
                a = float(parts[0])
                b = float(parts[1]) if len(parts) > 1 and parts[1] else None
                if a <= 0 or (b is not None and b <= 0):
                    raise ValueError
            except ValueError:
                log.warning("%s line %d skipped, expected 'tone A, tone B, name': %s", self.path.name, number, line)
                continue
            name = parts[2].strip('"') if len(parts) > 2 and parts[2] else describe(a, b)
            entries.append((a, b, name))
        self.entries = entries
        log.info("Loaded %d stations from %s", len(entries), self.path)

    def name(self, tone_a: float, tone_b: float | None) -> str | None:
        self.reload_if_changed()
        best, best_error = None, None
        for a, b, name in self.entries:
            if (b is None) != (tone_b is None):
                continue
            error = abs(tone_a - a) / a + (abs(tone_b - b) / b if b else 0)
            if abs(tone_a - a) / a <= MATCH_FRACTION and (b is None or abs(tone_b - b) / b <= MATCH_FRACTION) \
                    and (best_error is None or error < best_error):
                best, best_error = name, error
        return best


def stations_named(text: str) -> tuple[set[str], set[int]]:
    """Station names ("EMS 27") and station numbers (27, also from call signs like "Medic 2781") in a
    transcript."""
    names, numbers = set(), set()
    for m in STATION_NAME.finditer(text or ""):
        number = int(m.group(2))
        if number == 0:
            continue
        names.add(f"{STATION_WORDS.get(m.group(1).lower(), 'EMS')} {number}")
        numbers.add(number)
    for m in CALL_SIGN.finditer(text or ""):
        if int(m.group(1)):
            numbers.add(int(m.group(1)))
    return names, numbers


def pick_name(heard: list[tuple[set[str], set[int]]], min_seen: int, share: float) -> tuple[str, int] | None:
    """The station dispatch names after most of a tone pair's tone-outs, and after how many: by name
    ("EMS 27"), or else by number when the service varies ("Fire 20" and "EMS 20" make "Station 20").
    None while it is unclear. Tones every fire call starts or ends with name a different station each
    time, so they stay unnamed."""
    if len(heard) < min_seen:
        return None
    for counts, name_of in ((Counter(n for names, _ in heard for n in names), str),
                            (Counter(n for _, numbers in heard for n in numbers), lambda n: f"Station {n}")):
        top = counts.most_common(2)
        if top and top[0][1] >= max(min_seen, share * len(heard)) and (len(top) == 1 or top[0][1] > top[1][1]):
            return name_of(top[0][0]), top[0][1]
    return None


def describe(tone_a: float, tone_b: float | None) -> str:
    return f"{tone_a:.1f} Hz long tone" if tone_b is None else f"{tone_a:.1f} Hz + {tone_b:.1f} Hz"


class ToneFinder:
    """Checks each call's audio for pages, logs them with the tone-outs SDRTrunk posts, and alerts."""

    def __init__(self, config, db, alerter=None, rate: int = 16000):
        self.settings = config.tones
        self.db = db
        self.alerter = alerter
        self.rate = rate
        self.tone_list = ToneList(config.tones_path)
        self.path = config.tones_path
        self._learned_at = 0.0

    def wanted(self, call: dict) -> bool:
        if not self.settings.talkgroups:
            return True
        names = " ".join(filter(None, (call.get("talkgroup"), call.get("talkgroup_label"),
                                       call.get("talkgroup_group")))).lower()
        return any(str(t).strip().lower() in names for t in self.settings.talkgroups if str(t).strip())

    def __call__(self, call: dict, samples: np.ndarray) -> list[dict]:
        if not self.wanted(call):
            return []
        logged = []
        for page in find_pages(samples, self.rate):
            name = self.tone_list.name(page.tone_a, page.tone_b)
            record = {
                "page_time": call["call_time"] + page.offset_s,
                "received_at": call["received_at"],
                "tone_a": page.tone_a,
                "tone_b": page.tone_b,
                "tone_a_seconds": page.tone_a_seconds,
                "label": name,
                "system_label": call.get("system_label"),
                "site": None,
                "channel": call.get("talkgroup_label") or call.get("talkgroup"),
                "frequency": call.get("frequency"),
            }
            record["id"] = self.db.insert_page(record)
            log.info("Tone out in call %d: %s (%s)", call["id"], name or "not on the station list",
                     describe(page.tone_a, page.tone_b))
            if self.alerter:
                self.alerter.page(record)
            logged.append(record)
        return logged

    # ---- learning names

    def learn_if_due(self, now: float | None = None):
        now = time.time() if now is None else now
        if not self.settings.learn or now - self._learned_at < LEARN_EVERY_S:
            return
        self._learned_at = now
        try:
            self.learn(now)
        except Exception:
            log.exception("Learning tone names failed")

    def learn(self, now: float) -> list[tuple[float, float | None, str]]:
        """Names the unlisted tone pairs dispatch keeps naming the same station after, adds them to the
        station list, and labels their tone-outs already logged. Returns what it added."""
        pages = self.db.unnamed_pages(now - self.settings.learn_days * 86400, now - LEARN_AFTER_S)
        if not pages:
            return []
        # Tone pairs, each with its tone-outs: [[page, ...], ...].
        pairs: list[dict] = []
        for page in pages:
            pair = next((p for p in pairs if _same_tones(p, page)), None)
            if pair is None:
                pair = {"tone_a": page["tone_a"], "tone_b": page["tone_b"], "pages": [], "outs": []}
                pairs.append(pair)
            pair["pages"].append(page)
            if pair["outs"] and page["page_time"] - pair["outs"][-1][-1]["page_time"] <= SAME_PAGE_S:
                pair["outs"][-1].append(page)
            else:
                pair["outs"].append([page])
        self.tone_list.reload_if_changed()
        calls = self.db.transcripts_between(pages[0]["page_time"] - LEARN_BEFORE_S, now)
        added = []
        for pair in pairs:
            ids = [p["id"] for p in pair["pages"]]
            tone_a = sum(p["tone_a"] for p in pair["pages"]) / len(ids)
            tone_b = None if pair["tone_b"] is None else sum(p["tone_b"] for p in pair["pages"]) / len(ids)
            listed = self.tone_list.name(tone_a, tone_b)
            if listed:
                self.db.name_pages(ids, listed)      # logged before the name was on the list
                continue
            heard = [stations_named(" ".join(self._said_after(out[0], calls))) for out in pair["outs"]]
            picked = pick_name(heard, self.settings.learn_min_seen, self.settings.learn_share)
            if not picked:
                continue
            name, seen = picked
            self._add_to_list(tone_a, tone_b, name, seen, len(heard))
            self.db.name_pages(ids, name)
            added.append((round(tone_a, 1), None if tone_b is None else round(tone_b, 1), name))
            log.info("Learned tone-out %s: %s (named after %d of %d tone-outs)", describe(tone_a, tone_b), name,
                     seen, len(heard))
        return added

    @staticmethod
    def _said_after(page: dict, calls: list[dict]) -> list[str]:
        """Transcripts on the page's channel from the call holding the tones until LEARN_AFTER_S later."""
        t = page["page_time"]
        return [c["transcript"] for c in calls
                if t - LEARN_BEFORE_S <= c["call_time"] < t + LEARN_AFTER_S
                and c["call_time"] + (c["duration_s"] or 0) >= t - 1
                and (c["talkgroup_label"] or c["talkgroup"]) == page["channel"]]

    def _add_to_list(self, tone_a: float, tone_b: float | None, name: str, seen: int, total: int):
        if self.path is None:
            return
        newline = "\n"
        text = ""
        if self.path.exists():
            raw = self.path.read_bytes()
            newline = "\r\n" if b"\r\n" in raw else "\n"
            text = raw.decode("utf-8-sig")
        else:
            text = "tone_a,tone_b,name" + newline
        if text and not text.endswith(("\n", "\r")):
            text += newline
        b = "" if tone_b is None else f"{tone_b:.1f}"
        text += (f"# Learned {datetime.now():%Y-%m-%d}: dispatch named {name} after {seen} of {total} tone-outs"
                 f"{newline}{tone_a:.1f},{b},{name}{newline}")
        self.path.write_text(text, encoding="utf-8", newline="")
        self.tone_list.reload_if_changed()


def _same_tones(pair: dict, page: dict) -> bool:
    if (pair["tone_b"] is None) != (page["tone_b"] is None):
        return False
    if abs(page["tone_a"] - pair["tone_a"]) > pair["tone_a"] * MATCH_FRACTION:
        return False
    return pair["tone_b"] is None or abs(page["tone_b"] - pair["tone_b"]) <= pair["tone_b"] * MATCH_FRACTION
