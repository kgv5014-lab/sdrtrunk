"""HTTP side: an Rdio Scanner compatible call-upload endpoint plus the call log page and API.

SDRTrunk's Rdio Scanner streaming option posts one multipart form per call:
key, system, audio (MP3), dateTime (unix seconds), talkgroup, source, frequency,
talkerAlias, talkgroupLabel, talkgroupGroup, systemLabel, patches.
When it (re)connects it posts key, system and test=1, and treats a reply starting with
"Incomplete call data: no talkgroup" as a working server. An upload counts as delivered
when the reply is HTTP 200 and contains "Call imported successfully."

SDRTrunk's two-tone paging decoder posts each tone-out page as JSON to /api/page:
key, dateTime (unix seconds), toneA, toneB (null for a long tone), toneASeconds, label (station
name from SDRTrunk's paging_tones.csv, or null), system, site, channel, frequency.
"""

import hmac
import ipaddress
import json
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

from .alerts import Alerter
from .backends import create_backend
from .codes import CodeBook
from .places import PlaceBook
from .addresses import Gazetteer
from .config import Config
from .db import Database
from .geocode import Geocoder
from .incidents import MapWorker
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


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _clip(value, length: int = 200) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value[:length] or None


def _is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return host == "localhost"


def _safe(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", value or "")[:24] or "x"


def create_app(config: Config, backend_factory=create_backend) -> FastAPI:
    data_path = config.data_path
    (data_path / "calls").mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(data_path / "calls.db")
        codebook = CodeBook(config.codes_path, config.codes.time_window_minutes)
        placebook = PlaceBook(config.places_path, config.places.prompt_chars, config.places.areas)
        worker = TranscriptionWorker(config, db, backend_factory, codebook, placebook)
        alerter = Alerter(config, db) if config.alerts.enabled else None
        map_worker = None
        if config.map.enabled:
            gazetteer = Gazetteer(config.geo_path, config.places_path)
            geocoder = Geocoder(db, config.map.area, contact=config.map.contact_email) \
                if config.map.online_lookup else None
            map_worker = MapWorker(config, db, gazetteer, geocoder)
            worker.on_done = map_worker.submit
            if alerter:
                map_worker.on_placed = lambda call_id, incident_id, call: alerter.incident_call(
                    call_id, incident_id, call["call_time"], call["transcript"])
            map_worker.start()
        worker.start()
        app.state.db = db
        app.state.worker = worker
        app.state.codebook = codebook
        app.state.map_worker = map_worker
        app.state.alerter = alerter
        log.info("Listening for SDRTrunk uploads at http://%s:%d/api/call-upload",
                 config.server.host, config.server.port)
        yield
        worker.stop()
        if map_worker:
            map_worker.stop()
        if alerter:
            alerter.stop()
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

    @app.post("/api/page")
    async def page_upload(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return PlainTextResponse("Expected a JSON body", status_code=400)
        if not isinstance(body, dict):
            return PlainTextResponse("Expected a JSON object", status_code=400)

        # SDRTrunk on the same computer needs no key; anything else must send the upload key.
        if config.server.api_key and not _is_loopback(request.client.host if request.client else None):
            key = str(body.get("key") or "")
            if not hmac.compare_digest(key.encode(), config.server.api_key.encode()):
                log.warning("Rejected a page with the wrong API key from %s", request.client.host)
                return PlainTextResponse("Invalid API key", status_code=401)

        tone_a = _number(body.get("toneA"))
        if tone_a is None or tone_a <= 0:
            return PlainTextResponse("Page needs toneA", status_code=400)
        tone_b = _number(body.get("toneB"))
        frequency = _number(body.get("frequency"))
        page = {
            "page_time": _number(body.get("dateTime")) or time.time(),
            "received_at": int(time.time()),
            "tone_a": tone_a,
            "tone_b": tone_b if tone_b and tone_b > 0 else None,
            "tone_a_seconds": _number(body.get("toneASeconds")),
            "label": _clip(body.get("label")),
            "system_label": _clip(body.get("system")),
            "site": _clip(body.get("site")),
            "channel": _clip(body.get("channel")),
            "frequency": int(frequency) if frequency else None,
        }
        page["id"] = request.app.state.db.insert_page(page)
        if request.app.state.alerter:
            request.app.state.alerter.page(page)
        return {"id": page["id"]}

    @app.get("/api/pages")
    def list_pages(request: Request, since: float | None = None, until: float | None = None, q: str = "",
                   limit: int = 200):
        return request.app.state.db.pages(since, until, q, limit)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/calls")
    def list_calls(request: Request, q: str = "", talkgroup: str = "", source: str = "",
                   before_id: int | None = None, limit: int = 100):
        calls = request.app.state.db.search(q, talkgroup, source, before_id, limit)
        codebook = request.app.state.codebook
        # Labels are worked out on every read, so edits to codes.csv apply to old calls too.
        for call in calls:
            call["codes"] = codebook.annotate(call["transcript"], call["talkgroup_label"],
                                              call["talkgroup_group"])
        return calls

    @app.get("/api/calls/{call_id}/audio")
    def call_audio(request: Request, call_id: int):
        call = request.app.state.db.get_call(call_id)
        if call is None:
            raise HTTPException(404)
        path = (data_path / call["audio_path"]).resolve()
        if not path.is_relative_to(data_path.resolve()) or not path.exists():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/map", response_class=HTMLResponse)
    def map_page():
        return (WEB_DIR / "map.html").read_text(encoding="utf-8")

    def _map_worker(request: Request) -> MapWorker:
        if request.app.state.map_worker is None:
            raise HTTPException(404, "The incident map is turned off in config.toml ([map] enabled)")
        return request.app.state.map_worker

    @app.get("/api/incidents")
    def list_incidents(request: Request, since: int | None = None, until: int | None = None,
                       hidden: bool = False, limit: int = 300):
        _map_worker(request)
        db = request.app.state.db
        codebook = request.app.state.codebook
        incidents = db.incidents(since, until, hidden, limit)
        for incident in incidents:
            for call in incident["calls"]:
                call["codes"] = codebook.annotate(call["transcript"], call["talkgroup_label"],
                                                  call["talkgroup_group"])
            # Tone-outs just before the first call (pages carry no address, so this is by time only).
            incident["pages"] = db.pages(incident["first_time"] - 600, incident["first_time"] + 60, limit=10)
        return incidents

    @app.delete("/api/incidents/{incident_id}")
    def hide_incident(request: Request, incident_id: int):
        _map_worker(request)
        if not request.app.state.db.update_incident(incident_id, hidden=1):
            raise HTTPException(404)
        return {"hidden": incident_id}

    @app.post("/api/incidents/{incident_id}/restore")
    def restore_incident(request: Request, incident_id: int):
        _map_worker(request)
        if not request.app.state.db.update_incident(incident_id, hidden=0):
            raise HTTPException(404)
        return {"restored": incident_id}

    @app.post("/api/map/rebuild")
    def rebuild_map(request: Request):
        """Places every call again, e.g. after places.csv or places-geo.json changed."""
        if not _is_loopback(request.client.host if request.client else None):
            raise HTTPException(403, "Only from this computer")
        worker = _map_worker(request)
        worker.gazetteer.reload_if_changed()
        worker.rebuild()
        return {"queued": worker.queue_size()}

    @app.get("/api/map/status")
    def map_status(request: Request):
        worker = request.app.state.map_worker
        if worker is None:
            return {"enabled": False}
        gazetteer = worker.gazetteer
        return {
            "enabled": True,
            "streets": len(gazetteer.streets),
            "towns": len(gazetteer.town_names),
            "has_shapes": gazetteer.has_shapes,
            "online_lookup": worker.geocoder is not None,
            "queue": worker.queue_size(),
            "area": config.map.area,
            "tiles_url": config.map.tiles_url,
        }

    if config.alerts.enabled:
        # The two addresses the ntfy phone app asks, at this PC's address plus the topic name.
        topic = config.alerts.topic
        hosts_seen = set()

        def _alerter(request: Request) -> Alerter:
            alerter = request.app.state.alerter
            host = request.headers.get("host")
            # Through `tailscale serve` the host may read localhost; only a real name tells anything.
            if host and host.split(":")[0] in ("localhost", "127.0.0.1", "testserver"):
                host = None
            if config.alerts.base_url and host and host not in config.alerts.base_url and host not in hosts_seen:
                hosts_seen.add(host)
                log.warning("Phone alerts: the phone used %s but [alerts] base_url is %s; iPhone wake-ups "
                            "need them to match", request.headers["host"], config.alerts.base_url)
            return alerter

        @app.get(f"/{topic}/auth")
        def ntfy_auth(request: Request):
            _alerter(request)
            return {"success": True}

        @app.get(f"/{topic}/json")
        def ntfy_poll(request: Request, since: str | None = None, id: str | None = None):
            lines = [json.dumps(m) for m in _alerter(request).messages(since, id)]
            return PlainTextResponse("".join(line + "\n" for line in lines), media_type="application/x-ndjson")

        @app.post("/api/alerts/test")
        def test_alert(request: Request):
            """Sends a test alert to the phone. From this computer only."""
            if not _is_loopback(request.client.host if request.client else None):
                raise HTTPException(403, "Only from this computer")
            return request.app.state.alerter.send(
                "test", "Test alert", "Phone alerts from the SDRTrunk transcriber are working.",
                tags=["white_check_mark"], click=f"{config.alerts.base_url.rstrip('/')}/map"
                if config.alerts.base_url else None)

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
