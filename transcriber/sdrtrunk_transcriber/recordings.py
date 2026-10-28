"""Deletes old call audio so the data folder doesn't fill the disk.

Audio older than [recordings] max_age_days goes first; then, while all call audio together is over
max_total_gb, the oldest goes. Calls on an incident and calls that sent a phone alert keep their audio.
Only the sound file is deleted: the call, its transcript and its details stay in the call log.
"""

import logging
import os
import threading
import time
from pathlib import Path

from .config import Config
from .db import Database

log = logging.getLogger(__name__)


def _folder_size(folder: Path) -> int:
    total = 0
    for root, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _remove_empty_folders(folder: Path):
    for root, dirs, files in os.walk(folder, topdown=False):
        if Path(root) != folder and not dirs and not files:
            try:
                os.rmdir(root)
            except OSError:
                pass


class RecordingCleanup:
    def __init__(self, config: Config, db: Database):
        self.settings = config.recordings
        self.data_path = config.data_path
        self.calls_path = self.data_path / "calls"
        # Calls the map hasn't looked at yet may still join an incident.
        self.keep_unplaced = config.map.enabled
        self.db = db
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="recordings", daemon=True)

    @property
    def enabled(self) -> bool:
        return self.settings.max_age_days > 0 or self.settings.max_total_gb > 0

    def start(self):
        parts = []
        if self.settings.max_age_days > 0:
            parts.append(f"older than {self.settings.max_age_days:g} days")
        if self.settings.max_total_gb > 0:
            parts.append(f"oldest first over {self.settings.max_total_gb:g} GB")
        log.info("Deleting call audio %s; incident and alert audio is kept", " and ".join(parts))
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("Recordings: cleanup failed")
            self._stop.wait(max(1.0, self.settings.check_minutes) * 60)

    def _delete(self, call: dict) -> int | None:
        """Deletes one call's audio; returns the bytes freed, or None if it couldn't be deleted."""
        path = (self.data_path / call["audio_path"]).resolve()
        if not path.is_relative_to(self.calls_path.resolve()):
            return None
        try:
            size = path.stat().st_size
            path.unlink()
            return size
        except FileNotFoundError:
            return 0
        except OSError as e:
            log.warning("Recordings: could not delete %s (%s)", path, e)
            return None

    def run_once(self, now: float | None = None) -> tuple[int, int]:
        """One cleanup pass. Returns how many calls lost their audio and the bytes freed."""
        now = time.time() if now is None else now
        deleted, freed = [], 0

        if self.settings.max_age_days > 0:
            cutoff = int(now - self.settings.max_age_days * 86400)
            for call in self.db.audio_to_delete(before=cutoff, keep_unplaced=self.keep_unplaced):
                size = self._delete(call)
                if size is not None:
                    deleted.append(call["id"])
                    freed += size

        if self.settings.max_total_gb > 0:
            limit = int(self.settings.max_total_gb * 1024 ** 3)
            total = _folder_size(self.calls_path)
            if total > limit:
                done = set(deleted)
                for call in self.db.audio_to_delete(keep_unplaced=self.keep_unplaced):
                    if total <= limit:
                        break
                    if call["id"] in done:
                        continue
                    size = self._delete(call)
                    if size is not None:
                        deleted.append(call["id"])
                        freed += size
                        total -= size
                if total > limit:
                    log.warning("Recordings: call audio is still %.2f GB; the rest is kept for incidents and "
                                "alerts", total / 1024 ** 3)

        if deleted:
            self.db.mark_audio_deleted(deleted, int(now))
            _remove_empty_folders(self.calls_path)
            log.info("Recordings: deleted audio of %d old calls (%.0f MB)", len(deleted), freed / 1024 ** 2)
        return len(deleted), freed
