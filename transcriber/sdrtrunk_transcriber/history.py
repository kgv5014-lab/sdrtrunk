"""Incident history: a lasting copy of every incident on the map, for the heat map page (/history)
and the CSV download.

The incidents table is cleared whenever the map is rebuilt (after the street list changes or an update
changes how calls are placed), then filled again from the transcripts. The history table is not cleared.
Each row is keyed by the incident's first call, which is the same before and after a rebuild, so a
rebuild updates rows instead of adding them twice. A row goes away only when its calls became part of
another incident, or when its first call was taken off the map by hand.
"""

import csv
import io
import logging
import threading
import time

from .db import Database
from .incidents import services_of

log = logging.getLogger(__name__)

SYNC_SECONDS = 60
TYPES = ("fire", "ems", "police", "mva", "other")


def incident_type(nature: str | None, category: str | None) -> str:
    """What the map colors and filters by: MVA first, since crashes go to every agency."""
    return "mva" if nature == "MVA" else category or "other"


def _fingerprint(row: dict) -> tuple:
    return (row["incident_id"], row["first_time"], row["last_time"], round(row["lat"], 6), round(row["lon"], 6),
            row["label"], row["category"], row["nature"], int(row["hidden"] or 0), row["calls"])


class HistoryKeeper:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_sync = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="incident-history", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception:
                log.exception("Incident history: could not save")
            self._stop.wait(SYNC_SECONDS)

    def sync_if_stale(self, seconds: float = 10):
        if time.time() - self.last_sync > seconds:
            self.sync()

    def sync(self) -> dict:
        """Copies new and changed incidents into the history. Returns counts, for the log and tests."""
        with self._lock:
            now = int(time.time())
            snapshots = self.db.incident_snapshots()
            index = self.db.history_index()
            owner: dict[int, int] = {}          # call id -> first call of the incident it is in now
            changed = []
            for snap in snapshots:
                ids = snap["call_ids"]
                first = ids[0]
                for call_id in ids:
                    owner[call_id] = first
                row = {"first_call_id": first, "incident_id": snap["id"], "first_time": snap["first_time"],
                       "last_time": snap["last_time"], "lat": snap["lat"], "lon": snap["lon"],
                       "label": snap["label"], "type": incident_type(snap["nature"], snap["category"]),
                       "category": snap["category"], "nature": snap["nature"], "calls": len(ids),
                       "call_ids": ",".join(map(str, ids)), "hidden": int(snap["hidden"] or 0), "updated_at": now}
                old = index.get(first)
                if old is None or _fingerprint(old) != _fingerprint(row):
                    changed.append(row)
            if changed:
                calls = {c["id"]: c for c in self.db.calls_for_services(
                    [int(c) for row in changed for c in row["call_ids"].split(",")])}
                for row in changed:
                    row["services"] = ",".join(services_of(
                        [calls[int(c)] for c in row["call_ids"].split(",") if int(c) in calls]))
            live = {snap["call_ids"][0] for snap in snapshots}
            # Rows whose first call is now in another incident: that incident's row covers them.
            gone = [first for first in index if first not in live and first in owner]
            orphans = [first for first in index if first not in live and first not in owner]
            statuses = self.db.map_statuses(orphans) if orphans else {}
            gone += [first for first in orphans if statuses.get(first) == "removed"]
            # The rest stay: the map is being rebuilt, or the incident fell off the map some other way.
            detached = [first for first in orphans
                        if statuses.get(first) != "removed" and index[first]["incident_id"] is not None]
            if changed or gone or detached:
                self.db.save_history(changed, gone, detached)
            self.last_sync = time.time()
        if changed or gone:
            log.info("Incident history: %d saved, %d merged or removed", len(changed), len(gone))
        return {"saved": len(changed), "gone": len(gone), "detached": len(detached)}


CSV_COLUMNS = ("start", "end", "type", "nature", "address", "latitude", "longitude", "services", "calls",
               "removed_from_map", "first_call_id")


def _local(t: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")     # Excel on Windows
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_local(row["first_time"]), _local(row["last_time"]), row["type"], row["nature"] or "",
                         row["label"] or "", f"{row['lat']:.6f}", f"{row['lon']:.6f}",
                         (row["services"] or "").replace(",", " "), row["calls"], "yes" if row["hidden"] else "",
                         row["first_call_id"]])
    return out.getvalue()
