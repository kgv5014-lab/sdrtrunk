"""Live audio: new calls played to a phone as one endless MP3 stream, like an internet radio station.

Each call joins the stream a moment after SDRTrunk uploads it (when the call ends), so calls are never
cut off mid-sentence. Between calls the stream carries silence, which keeps the phone's player
connected and playing with the screen locked.

Two ways in:
- /listen on the main service, private to this PC and Tailscale (the map's Listen button).
- The same page on its own port (public_port, 3001), with every request needing the secret key in
  data/listen-key.txt. Only that port is put on the internet with Tailscale Funnel, so the phone can
  listen with another VPN on. It has nothing but the listen page, its stream and the talkgroup list.
"""

import asyncio
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import av
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import audio

log = logging.getLogger(__name__)

RATE = audio.WHISPER_RATE
BIT_RATE = 32000
# Audio is made in half-second pieces and sent up to LEAD_S ahead of real time, which the phone buffers.
CHUNK = RATE // 2
LEAD_S = 2.0
# Quiet between back-to-back calls, so they don't run together.
GAP = int(0.6 * RATE)
# Calls waiting for one phone. When it falls further behind than this, the oldest are skipped.
MAX_WAITING = 6
# Phones listening at once. A new one past this pushes out the one connected longest.
MAX_LISTENERS = 6
TALKGROUP_DAYS = 7
WEB_DIR = Path(__file__).parent / "web"


@dataclass
class Listener:
    id: str
    talkgroups: set[str] | None
    waiting: deque = field(default_factory=deque)
    recent: deque = field(default_factory=lambda: deque(maxlen=10))
    playing: dict | None = None
    closed: bool = False

    def wants(self, talkgroup: str) -> bool:
        return self.talkgroups is None or talkgroup in self.talkgroups


class LiveHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._listeners: dict[str, Listener] = {}

    def wants(self, talkgroup: str) -> bool:
        with self._lock:
            return any(l.wants(talkgroup) for l in self._listeners.values())

    def add_call(self, call: dict, samples: np.ndarray):
        """Queues a just-uploaded call for every phone listening to its talkgroup."""
        if not len(samples):
            return
        item = {"id": call.get("id"), "talkgroup": call["talkgroup"],
                "label": call.get("talkgroup_label") or call["talkgroup"],
                "call_time": call.get("call_time"), "seconds": round(len(samples) / RATE, 1),
                "samples": _level(samples)}
        with self._lock:
            for listener in self._listeners.values():
                if listener.wants(call["talkgroup"]):
                    listener.waiting.append(item)
                    while len(listener.waiting) > MAX_WAITING:
                        listener.waiting.popleft()

    def join(self, listener_id: str, talkgroups: set[str] | None) -> Listener:
        listener = Listener(listener_id, talkgroups)
        with self._lock:
            old = self._listeners.pop(listener_id, None)
            if old:
                old.closed = True
            while len(self._listeners) >= MAX_LISTENERS:
                self._listeners.pop(next(iter(self._listeners))).closed = True
            self._listeners[listener_id] = listener
        return listener

    def leave(self, listener: Listener):
        with self._lock:
            if self._listeners.get(listener.id) is listener:
                del self._listeners[listener.id]

    def now(self, listener_id: str) -> dict:
        with self._lock:
            listener = self._listeners.get(listener_id)
            if listener is None:
                return {"connected": False, "playing": None, "waiting": 0, "recent": []}
            return {"connected": True, "playing": _brief(listener.playing),
                    "waiting": len(listener.waiting), "recent": [_brief(c) for c in reversed(listener.recent)]}

    def _next(self, listener: Listener) -> dict | None:
        with self._lock:
            if listener.playing:
                listener.recent.append(listener.playing)
            listener.playing = listener.waiting.popleft() if listener.waiting else None
            return listener.playing

    async def stream(self, listener: Listener):
        """Yields MP3 bytes forever: the listener's calls, with silence when there are none."""
        encoder = _Encoder()
        started = time.monotonic()
        sent = 0
        pcm, pos = None, 0
        try:
            while not listener.closed:
                ahead = sent / RATE - (time.monotonic() - started)
                if ahead > LEAD_S:
                    await asyncio.sleep(min(0.25, ahead - LEAD_S + 0.05))
                    continue
                if pcm is None:
                    call = self._next(listener)
                    if call:
                        pcm, pos = np.concatenate([np.zeros(GAP, dtype=np.float32), call["samples"]]), 0
                if pcm is not None:
                    chunk = pcm[pos:pos + CHUNK]
                    pos += CHUNK
                    if pos >= len(pcm):
                        pcm = None
                else:
                    chunk = np.zeros(CHUNK, dtype=np.float32)
                sent += len(chunk)
                data = encoder.encode(chunk)
                if data:
                    yield data
        finally:
            self.leave(listener)


def _brief(call: dict | None) -> dict | None:
    if call is None:
        return None
    return {k: call[k] for k in ("id", "talkgroup", "label", "call_time", "seconds")}


def _level(samples: np.ndarray) -> np.ndarray:
    """Same voice band and loudness for every call. Paging tones are left in, so tone-outs are heard."""
    try:
        return audio.normalize(audio.band_pass(samples)).astype(np.float32)
    except Exception:
        log.exception("Live audio: could not level a call; playing it as recorded")
        return samples.astype(np.float32)


class _Encoder:
    def __init__(self):
        self.ctx = av.CodecContext.create("libmp3lame", "w")
        self.ctx.sample_rate = RATE
        self.ctx.layout = "mono"
        self.ctx.format = "s16p"
        self.ctx.bit_rate = BIT_RATE
        self.ctx.open()
        self.pts = 0

    def encode(self, samples: np.ndarray) -> bytes:
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(pcm, format="s16p", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.pts
        self.pts += pcm.shape[1]
        return b"".join(bytes(packet) for packet in self.ctx.encode(frame))


def key_path(data_path: Path) -> Path:
    return data_path / "listen-key.txt"


def read_key(data_path: Path) -> str:
    """The secret for the public listen link, made on first use."""
    path = key_path(data_path)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    return key or new_key(data_path)


def new_key(data_path: Path) -> str:
    key = secrets.token_urlsafe(24)
    path = key_path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    return key


async def add_call(hub: LiveHub, call: dict, path: Path):
    """Called after each upload. Decodes the call only when a phone is listening to its talkgroup."""
    if not hub.wants(call["talkgroup"]):
        return
    try:
        samples = await asyncio.to_thread(audio.decode, path)
    except Exception:
        log.exception("Live audio: could not read %s", path)
        return
    hub.add_call(call, samples)


def add_routes(app: FastAPI, main_app: FastAPI, data_path: Path, public: bool):
    """The listen page and its API. On the public port every request must carry ?key=."""

    def check(request: Request):
        if public:
            given = request.query_params.get("key", "")
            if not hmac.compare_digest(given.encode(), read_key(data_path).encode()):
                raise HTTPException(404)

    def state(name: str):
        value = getattr(main_app.state, name, None)
        if value is None:
            raise HTTPException(503, "Still starting up")
        return value

    @app.get("/listen", response_class=HTMLResponse)
    def listen_page(request: Request):
        check(request)
        return (WEB_DIR / "listen.html").read_text(encoding="utf-8")

    @app.get("/listen/talkgroups")
    def listen_talkgroups(request: Request):
        check(request)
        return state("db").recent_talkgroups(int(time.time()) - TALKGROUP_DAYS * 86400)

    @app.get("/listen/stream.mp3")
    def listen_stream(request: Request, id: str = "", tg: str = ""):
        check(request)
        hub = state("live")
        talkgroups = {t for t in tg.split(",") if t} or None
        listener = hub.join(id[:40] or secrets.token_hex(6), talkgroups)
        return StreamingResponse(hub.stream(listener), media_type="audio/mpeg",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/listen/now")
    def listen_now(request: Request, id: str = ""):
        check(request)
        return state("live").now(id)


def create_public_app(main_app: FastAPI, data_path: Path) -> FastAPI:
    """The listen-only app for the public port: no call log, map, edits or API docs."""
    app = FastAPI(title="Listen", docs_url=None, redoc_url=None, openapi_url=None)
    add_routes(app, main_app, data_path, public=True)
    return app


def start_public(main_app: FastAPI, data_path: Path, port: int):
    """Runs the public listen app on 127.0.0.1:port in the background (Tailscale Funnel points here).

    The read-only guest view for friends (share.py) is on the same port, once a password is set.
    """
    import uvicorn

    from .share import ShareGate

    read_key(data_path)
    app = ShareGate(create_public_app(main_app, data_path), main_app, data_path)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                           port=port, log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, name="listen-public", daemon=True).start()
    log.info("Live audio: listen-only page on http://127.0.0.1:%d/listen (needs the key in %s)",
             port, key_path(data_path))
    return server
