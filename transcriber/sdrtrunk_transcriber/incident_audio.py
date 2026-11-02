"""Play all: an incident's calls joined into one MP3, oldest first, with a short pause between calls.

One file (not a list of clips) so an iPhone keeps playing with the screen locked. The file is kept in
data/incident-audio and made again only when the incident's calls change. The map gets each call's
start time in the file, so it can show which call is playing and skip to the next one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

import av
import numpy as np

from . import audio, live

log = logging.getLogger(__name__)

RATE = audio.WHISPER_RATE
BIT_RATE = 32000
GAP_S = 1.0          # silence between calls
KEEP_FILES = 40      # joined files kept; older ones are deleted
FOLDER = "incident-audio"

_lock = threading.Lock()


def _folder(data_path: Path) -> Path:
    return data_path / FOLDER


def _version(calls: list[dict]) -> str:
    """Changes when a call joins or leaves the incident, or its audio is deleted."""
    text = ",".join(f"{c['id']}{'d' if c.get('audio_deleted_at') else ''}" for c in calls)
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def _audio_file(data_path: Path, call: dict) -> Path | None:
    if call.get("audio_deleted_at") or not call.get("audio_path"):
        return None
    path = (data_path / call["audio_path"]).resolve()
    if not path.is_relative_to(data_path.resolve()) or not path.exists():
        return None
    return path


def _write_mp3(path: Path, pieces: list[np.ndarray]):
    tmp = path.with_suffix(".part")
    with av.open(str(tmp), "w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=RATE, layout="mono")
        stream.bit_rate = BIT_RATE
        pts = 0
        for samples in pieces:
            for start in range(0, len(samples), RATE):
                pcm = (np.clip(samples[start:start + RATE], -1.0, 1.0) * 32767).astype(np.int16).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
                frame.sample_rate = RATE
                frame.pts = pts
                pts += pcm.shape[1]
                for packet in stream.encode(frame):
                    container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    tmp.replace(path)


def _tidy(folder: Path, incident_id: int, keep: Path):
    """Old versions of this incident go, and only the newest KEEP_FILES files are kept."""
    for old in folder.glob(f"{incident_id}-*"):
        if old.stem != keep.stem:
            old.unlink(missing_ok=True)
    files = sorted(folder.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_FILES:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def build(db, data_path: Path, incident_id: int) -> dict | None:
    """The joined file for an incident, made if needed. None when the incident has no calls.

    Returns {"file": Path or None, "version", "seconds", "calls": [{"id", "start", "seconds"}],
    "missing": [call ids with no audio]}. "file" is None when no call has audio.
    """
    calls = db.incident_audio_calls(incident_id)
    if not calls:
        return None
    version = _version(calls)
    folder = _folder(data_path)
    mp3 = folder / f"{incident_id}-{version}.mp3"
    meta = mp3.with_suffix(".json")
    with _lock:
        if mp3.exists() and meta.exists():
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
                return {**info, "file": mp3}
            except (OSError, ValueError):
                pass
        pieces, placed, missing, at = [], [], [], 0
        gap = np.zeros(int(GAP_S * RATE), dtype=np.float32)
        for call in calls:
            path = _audio_file(data_path, call)
            samples = None
            if path is not None:
                try:
                    samples = audio.decode(path)
                except Exception:
                    log.exception("Play all: could not read %s", path)
            if samples is None or not len(samples):
                missing.append(call["id"])
                continue
            samples = live._level(samples)
            if pieces:
                pieces.append(gap)
                at += len(gap)
            placed.append({"id": call["id"], "start": round(at / RATE, 2), "seconds": round(len(samples) / RATE, 2)})
            pieces.append(samples)
            at += len(samples)
        info = {"version": version, "seconds": round(at / RATE, 2), "calls": placed, "missing": missing}
        if not placed:
            return {**info, "file": None}
        folder.mkdir(parents=True, exist_ok=True)
        _write_mp3(mp3, pieces)
        meta.write_text(json.dumps(info), encoding="utf-8")
        _tidy(folder, incident_id, mp3)
        return {**info, "file": mp3}
