"""HTTP side: an Rdio Scanner compatible call-upload endpoint plus the call log page and API.

SDRTrunk's Rdio Scanner streaming option posts one multipart form per call:
key, system, audio (MP3), dateTime (unix seconds), talkgroup, source, frequency,
talkerAlias, talkgroupLabel, talkgroupGroup, systemLabel, patches.
When it (re)connects it posts key, system and test=1, and treats a reply starting with
"Incomplete call data: no talkgroup" as a working server. An upload counts as delivered
when the reply is HTTP 200 and contains "Call imported successfully."
"""

import hmac
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from starlette.datastructures import UploadFile

from .backends import create_backend
from .config import Config
from .db import Database
from .worker import TranscriptionWorker

log = logging.getLogger(__name__)

TEST_REPLY = "Incomplete call data: no talkgroup"
SUCCESS_REPLY = "Call imported successfully."
WEB_DIR = Path(__file__).parent / "web"


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _text(form, name: str) -> str | None:
    value = form.get(name)
    if value is None or isinstance(value, UploadFile):
        return None
    value = str(value).strip()
    return value or None


def _safe(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", value or "")[:24] or "x"


def create_app(config: Config, backend_factory=create_backend) -> FastAPI:
    data_path = config.data_path
    (data_path / "calls").mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(data_path / "calls.db")
        worker = TranscriptionWorker(config, db, backend_factory)
        worker.start()
        app.state.db = db
        app.state.worker = worker
        log.info("Listening for SDRTrunk uploads at http://%s:%d/api/call-upload",
                 config.server.host, config.server.port)
        yield
        worker.stop()
        db.close()

    app = FastAPI(title="SDRTrunk transcriber", lifespan=lifespan)

    async def call_upload(request: Request):
        form = await request.form()

        if config.server.api_key:
            key = _text(form, "key") or ""
            if not hmac.compare_digest(key.encode(), config.server.api_key.encode()):
                log.warning("Rejected an upload with the wrong API key from %s", request.client.host)
                return PlainTextResponse("Invalid API key", status_code=401)

        upload = form.get("audio")
        talkgroup = _text(form, "talkgroup")

        # The connection test and any malformed call get Rdio Scanner's "incomplete" reply.
        if _text(form, "test") or not talkgroup or not isinstance(upload, UploadFile):
            return PlainTextResponse(TEST_REPLY, status_code=417)

        audio_bytes = await upload.read()
        if not audio_bytes:
            return PlainTextResponse("Incomplete call data: no audio", status_code=417)

        call_time = _int(_text(form, "dateTime"), int(time.time()))
        source = _text(form, "source")
        local = datetime.fromtimestamp(call_time)
        suffix = Path(upload.filename or "").suffix.lower() or ".mp3"
        if suffix not in (".mp3", ".wav", ".m4a"):
            suffix = ".mp3"
        relative = Path("calls", local.strftime("%Y"), local.strftime("%m"), local.strftime("%d"),
                        f"{local.strftime('%H%M%S')}_TG{_safe(talkgroup)}_R{_safe(source)}_"
                        f"{secrets.token_hex(3)}{suffix}")
        target = data_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio_bytes)

        call_id = request.app.state.db.insert_call({
            "call_time": call_time,
            "received_at": int(time.time()),
            "system_id": _text(form, "system"),
            "system_label": _text(form, "systemLabel"),
            "talkgroup": talkgroup,
            "talkgroup_label": _text(form, "talkgroupLabel"),
            "talkgroup_group": _text(form, "talkgroupGroup"),
            "source": source,
            "talker_alias": _text(form, "talkerAlias"),
            "frequency": _int(_text(form, "frequency")),
            "patches": _text(form, "patches"),
            "audio_path": relative.as_posix(),
        })
        request.app.state.worker.submit(call_id)
        return PlainTextResponse(SUCCESS_REPLY)

    # Rdio Scanner's own path, plus the bare host SDRTrunk defaults to.
    app.post("/api/call-upload")(call_upload)
    app.post("/")(call_upload)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/calls")
    def list_calls(request: Request, q: str = "", talkgroup: str = "", source: str = "",
                   before_id: int | None = None, limit: int = 100):
        return request.app.state.db.search(q, talkgroup, source, before_id, limit)

    @app.get("/api/calls/{call_id}/audio")
    def call_audio(request: Request, call_id: int):
        call = request.app.state.db.get_call(call_id)
        if call is None:
            raise HTTPException(404)
        path = (data_path / call["audio_path"]).resolve()
        if not path.is_relative_to(data_path.resolve()) or not path.exists():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/api/status")
    def status(request: Request):
        worker = request.app.state.worker
        return {
            "backend": config.transcription.backend,
            "backend_ready": worker.ready.is_set(),
            "queue": worker.queue_size(),
            "calls": request.app.state.db.status_counts(),
        }

    return app
