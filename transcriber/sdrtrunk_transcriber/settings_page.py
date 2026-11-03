"""The Settings page (/settings): edit the vocabulary, unit codes, learned corrections, Fire Scan
department names and phone alert distances from the browser, and add codes dispatch says that aren't
in codes.csv yet.

Only on the main service (127.0.0.1, reached over Tailscale). The friends' share link passes on only the
addresses in share.GUEST_API, read-only, so guests can never open or change anything here.

Every save first copies the old file to data/settings-backups. The vocabulary, codes and corrections
apply at once. Department names and alert distances live in config.toml; the page keeps its values in
settings-from-page.json next to config.toml, which is read after config.toml at start, and changes them
in the running service too, so they also apply at once.
"""

import csv
import io
import json
import logging
import os
import re
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from .codes import SIGNAL, TEN_CODE, UNIT_NUMBER, _agency, _code_key
from .corrections import FIELDS as CORRECTION_FIELDS
from .worker import load_vocabulary
from .zones import METRES_PER_MILE

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
OVERRIDES_FILE = "settings-from-page.json"
CODE_FIELDS = ("type", "code", "meaning", "applies_to", "prompt", "source")
CODE_TYPES = ("ten-code", "signal", "station", "unit-suffix", "police-esn", "call-type", "acronym")
APPLIES_TO = ("all", "police", "fire", "ems", "fire-ems")
KEEP_BACKUPS = 20
MAX_ROWS = 5000
# A code needs to be heard this many times to be listed as new (one-offs are mostly mishearings).
NEW_CODE_MIN_COUNT = 2


# ---- values kept in settings-from-page.json (read at start by config.load_config)

def overrides_path(config) -> Path:
    return config.base_dir / OVERRIDES_FILE


def read_overrides(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("Could not read %s: %s", path, e)
        return {}


def apply_overrides(config, data: dict):
    """Puts the page's values over config.toml's."""
    names = data.get("frequency_names")
    if isinstance(names, dict):
        config.channels.frequency_names = {str(k): str(v) for k, v in names.items()}
    close = data.get("close_miles")
    if isinstance(close, (int, float)):
        config.recaps.close_miles = float(close)
    radius = data.get("zone_radius_miles")
    if isinstance(radius, (int, float)) and config.alerts.zones and isinstance(config.alerts.zones[0], dict):
        config.alerts.zones = [dict(config.alerts.zones[0], radius_miles=float(radius))] + list(config.alerts.zones[1:])


# ---- saving files safely

def _backup(path: Path, backup_dir: Path):
    if not path.exists():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, backup_dir / f"{path.name}.{stamp}")
    old = sorted(backup_dir.glob(f"{path.name}.*"))
    for extra in old[:-KEEP_BACKUPS]:
        try:
            extra.unlink()
        except OSError:
            pass


def _save(path: Path, text: str, backup_dir: Path, encoding: str = "utf-8"):
    _backup(path, backup_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding=encoding, newline="")
    os.replace(temp, path)


def _read_csv(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    raw = path.read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp1252", errors="replace")  # Excel's "CSV" on Windows
    return [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(io.StringIO(content, newline=""))]


def _csv_text(rows: list[dict], fields) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields), lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _cell(value, length: int = 300) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(value if value is not None else "")).strip()[:length]


def clean_code_rows(rows) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("Expected a list of rows")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Expected a list of rows")
        row = {f: _cell(row.get(f)) for f in CODE_FIELDS}
        if not row["code"] and not row["meaning"]:
            continue
        row["type"] = row["type"].lower()
        if row["type"] not in CODE_TYPES:
            raise ValueError(f"Unknown type \"{row['type']}\" for {row['code'] or row['meaning']}")
        if not row["code"] or not row["meaning"]:
            raise ValueError(f"{row['code'] or row['meaning']}: needs both a code and a meaning")
        row["applies_to"] = (row["applies_to"] or "all").lower()
        if row["applies_to"] not in APPLIES_TO:
            raise ValueError(f"{row['code']}: \"for\" must be one of {', '.join(APPLIES_TO)}")
        row["prompt"] = "yes" if row["prompt"].lower() in ("yes", "y", "true", "1") else ""
        out.append(row)
    return out


def clean_correction_rows(rows) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("Expected a list of rows")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Expected a list of rows")
        heard, correct = _cell(row.get("heard"), 100), _cell(row.get("correct"), 100)
        if not heard and not correct:
            continue
        if not heard or not correct:
            raise ValueError(f"{heard or correct}: needs both what Whisper wrote and the right words")
        if heard == correct:
            continue
        count = _cell(row.get("count"), 6)
        out.append({"heard": heard, "correct": correct, "talkgroup": _cell(row.get("talkgroup"), 100),
                    "hint": "no" if str(row.get("hint", "yes")).lower() in ("no", "n", "false", "0") else "yes",
                    "count": count if count.isdigit() else "1",
                    "source": _cell(row.get("source"), 40) or "hand-made"})
    return out


def clean_departments(items) -> dict:
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("Expected a list of departments")
    names = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Expected a list of departments")
        mhz, name = _cell(item.get("mhz"), 20), _cell(item.get("name"), 60)
        if not mhz and not name:
            continue
        try:
            value = float(mhz)
        except ValueError:
            raise ValueError(f"\"{mhz}\" is not a frequency in MHz")
        if not 1 <= value <= 3000 or not name:
            raise ValueError(f"{mhz}: give a frequency in MHz (like 453.300) and a name")
        key = f"{value:.4f}"
        names[key[:-1] if key.endswith("0") else key] = name
    return names


def _miles(value, what: str) -> float:
    try:
        miles = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: give a number of miles")
    if not 0.05 <= miles <= 50:
        raise ValueError(f"{what}: give between 0.05 and 50 miles")
    return round(miles, 2)


# ---- codes heard on the radio that aren't in codes.csv

def new_codes(db, codebook, since: float, ignored: set[str], window_minutes: int = 3) -> list[dict]:
    """10-codes, signals and fire/EMS station numbers in recent transcripts that codes.csv doesn't
    have, most heard first, each with a few example calls."""
    codebook.reload_if_changed()
    known_codes, known_stations = set(codebook._codes), set(codebook._stations)
    counts: Counter = Counter()
    examples: dict[str, list[dict]] = {}
    kinds: dict[str, str] = {}
    for call in db.transcripts_since(since):
        if call.get("air"):
            continue
        text = call["transcript"] or ""
        when = datetime.fromtimestamp(call["call_time"])
        found = []
        for match in TEN_CODE.finditer(text):
            key = _code_key(match.group(0))
            # "10-28" said at 10:28 is the dispatch time, not a code.
            minute = int(match.group(1))
            if when.hour == 10 and abs(when.minute - minute) <= window_minutes:
                continue
            if key and key not in known_codes:
                found.append((key, "ten-code"))
        for match in SIGNAL.finditer(text):
            key = _code_key(match.group(0))
            if key and key not in known_codes:
                found.append((key, "signal"))
        if known_stations and _agency(call.get("talkgroup_label") or "") != "police":
            for match in UNIT_NUMBER.finditer(text):
                if match.group(2) not in known_stations:
                    found.append((f"Station {match.group(2)}", "station"))
        for key, kind in dict(found).items():
            if key.lower() in ignored:
                continue
            counts[key] += 1
            kinds[key] = kind
            calls = examples.setdefault(key, [])
            if len(calls) < 3:
                calls.append({"id": call["id"], "time": call["call_time"],
                              "talkgroup": call.get("talkgroup_label") or call.get("talkgroup") or "",
                              "text": text[:300]})
    listed = [{"code": key, "type": kinds[key], "count": n, "examples": examples[key][::-1]}
              for key, n in counts.most_common() if n >= NEW_CODE_MIN_COUNT]
    return listed[:100]


def _ignored_path(data_path: Path) -> Path:
    return data_path / "ignored-codes.txt"


def read_ignored(data_path: Path) -> set[str]:
    try:
        return {line.strip().lower() for line in _ignored_path(data_path).read_text(encoding="utf-8").splitlines()
                if line.strip()}
    except FileNotFoundError:
        return set()


# ---- the page and its addresses

def add_routes(app: FastAPI, config):
    data_path = config.data_path
    backup_dir = data_path / "settings-backups"

    async def _body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected JSON")
        return body

    def _alerts():
        zone = config.alerts.zones[0] if config.alerts.zones and isinstance(config.alerts.zones[0], dict) else None
        return {"alerts_on": config.alerts.enabled,
                "zone_name": (zone.get("name") or zone.get("address") or "Watch zone") if zone else None,
                "zone_radius_miles": float(zone.get("radius_miles", 0.5)) if zone else None,
                "zone_prefix": (zone.get("prefix") or "") if zone else "",
                "recaps_on": config.recaps.enabled, "close_miles": config.recaps.close_miles}

    def _save_overrides(**values):
        path = overrides_path(config)
        data = read_overrides(path)
        data.update(values)
        _save(path, json.dumps(data, indent=2) + "\n", backup_dir)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page():
        return (WEB_DIR / "settings.html").read_text(encoding="utf-8")

    @app.get("/api/settings")
    def get_settings(request: Request):
        vocabulary = config.vocabulary_path
        return {
            "vocabulary": vocabulary.read_text(encoding="utf-8") if vocabulary.exists() else "",
            "codes": _read_csv(config.codes_path) if config.codes_path else None,
            "corrections": [c.row() for c in request.app.state.corrections.items()] if config.corrections_path else None,
            "departments": [{"mhz": k, "name": v} for k, v in config.channels.frequency_names.items()],
            "alerts": _alerts(),
            "types": CODE_TYPES, "applies_to": APPLIES_TO,
        }

    @app.put("/api/settings/vocabulary")
    async def save_vocabulary(request: Request):
        text = (await _body(request)).get("text")
        if not isinstance(text, str) or len(text) > 100_000:
            raise HTTPException(400, "Expected {\"text\": ...}")
        lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
        while lines and not lines[-1]:
            lines.pop()
        _save(config.vocabulary_path, "\n".join(lines) + "\n", backup_dir)
        worker = getattr(request.app.state, "worker", None)
        terms = load_vocabulary(config.vocabulary_path)
        if worker is not None:
            worker.vocabulary = terms
        return {"saved": True, "terms": len(terms)}

    @app.put("/api/settings/codes")
    async def save_codes(request: Request):
        if config.codes_path is None:
            raise HTTPException(409, "Codes are turned off in config.toml")
        try:
            rows = clean_code_rows((await _body(request)).get("rows"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        _save(config.codes_path, _csv_text(rows, CODE_FIELDS), backup_dir, "utf-8-sig")
        request.app.state.codebook.reload_if_changed()
        return {"saved": True, "rows": len(rows)}

    @app.post("/api/settings/codes/add")
    async def add_code(request: Request):
        """Adds one code from the "New codes heard" list."""
        if config.codes_path is None:
            raise HTTPException(409, "Codes are turned off in config.toml")
        body = await _body(request)
        code = _cell(body.get("code"), 40)
        if body.get("type") == "station":
            code = re.sub(r"^\s*station\s*", "", code, flags=re.IGNORECASE)
        rows = _read_csv(config.codes_path)
        new = {"type": body.get("type"), "code": code, "meaning": body.get("meaning"),
               "applies_to": body.get("applies_to") or "all", "prompt": "", "source": "added on Settings page"}
        try:
            rows = clean_code_rows(rows + [new])
        except ValueError as e:
            raise HTTPException(400, str(e))
        _save(config.codes_path, _csv_text(rows, CODE_FIELDS), backup_dir, "utf-8-sig")
        request.app.state.codebook.reload_if_changed()
        return {"added": rows[-1]}

    @app.put("/api/settings/corrections")
    async def save_corrections(request: Request):
        if config.corrections_path is None:
            raise HTTPException(409, "Corrections are turned off in config.toml")
        try:
            rows = clean_correction_rows((await _body(request)).get("rows"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        book = request.app.state.corrections
        with book._lock:
            _save(config.corrections_path, _csv_text(rows, CORRECTION_FIELDS), backup_dir, "utf-8-sig")
        book.reload_if_changed()
        return {"saved": True, "rows": len(book.items())}

    @app.put("/api/settings/departments")
    async def save_departments(request: Request):
        try:
            names = clean_departments((await _body(request)).get("departments"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        _save_overrides(frequency_names=names)
        config.channels.frequency_names = names
        return {"saved": True, "departments": [{"mhz": k, "name": v} for k, v in names.items()]}

    @app.put("/api/settings/alerts")
    async def save_alerts(request: Request):
        body = await _body(request)
        values = {}
        try:
            if body.get("close_miles") is not None:
                values["close_miles"] = _miles(body["close_miles"], "12-hour summary distance")
            if body.get("zone_radius_miles") is not None:
                if not config.alerts.zones:
                    raise ValueError("There is no watch zone in config.toml to change")
                values["zone_radius_miles"] = _miles(body["zone_radius_miles"], "Alert distance")
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not values:
            raise HTTPException(400, "Nothing to save")
        _save_overrides(**values)
        apply_overrides(config, values)
        watch = getattr(request.app.state, "zone_watch", None)
        if watch is not None and "zone_radius_miles" in values and watch.zones:
            watch.zones[0].radius_m = values["zone_radius_miles"] * METRES_PER_MILE
        return {"saved": True, "alerts": _alerts()}

    @app.get("/api/settings/new-codes")
    def list_new_codes(request: Request, days: float = 30):
        days = min(max(days, 1), 365)
        return {"days": days, "codes": new_codes(request.app.state.db, request.app.state.codebook,
                                                 time.time() - days * 86400, read_ignored(data_path),
                                                 config.codes.time_window_minutes)}

    @app.post("/api/settings/new-codes/ignore")
    async def ignore_code(request: Request):
        code = _cell((await _body(request)).get("code"), 40)
        if not code:
            raise HTTPException(400, "Expected {\"code\": ...}")
        path = _ignored_path(data_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(code + "\n")
        return {"ignored": code}
