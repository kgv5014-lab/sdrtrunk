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

At the top of each hour SDRTrunk posts each dongle's signal quality as JSON to /api/signal
(see signal_quality.py).
"""

import hashlib
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
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.datastructures import UploadFile
from starlette.middleware.gzip import GZipMiddleware

from . import audio
from .alerts import Alerter
from .backends import create_backend
from .codes import CodeBook
from .corrections import CorrectionBook
from .hints import LearnedHints
from .places import PlaceBook
from .address_fix import AddressBook, AddressFixer
from .addresses import Gazetteer
from .aircraft import AircraftWatch
from .traffic import Traffic511
from .config import Config
from .db import Database
from .geocode import Geocoder
from .incidents import MapWorker, highlights, services_of
from .zones import ZoneWatch, read_zones
from .speakers import SpeakerBook
from .summaries import Summarizer, attach as attach_summary, incident_basis
from .signal_quality import SignalWatch
from .recordings import RecordingCleanup
from .tones import ToneFinder
from .units import UnitBoard
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


def _json_unless_unchanged(request: Request, data) -> Response:
    """JSON with an ETag, or an empty 304 when the page already has this exact reply.

    The map and calls pages poll every few seconds; over a phone's data plan, sending the same
    hundreds of kilobytes again each time is what makes them slow.
    """
    body = json.dumps(jsonable_encoder(data), separators=(",", ":"), ensure_ascii=False).encode()
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


class _CompressText:
    """Gzips the pages and JSON replies, but not call audio or camera pictures.

    Those are already compressed, and Safari plays audio with range requests that gzip would break.
    """

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1000)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if scope["type"] == "http" and not path.endswith(("/audio", "/image")):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


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
        corrections = CorrectionBook(config.corrections_path)
        hints = LearnedHints(data_path / "calls.db", config.hints) if config.hints.enabled else None
        gazetteer = Gazetteer(config.geo_path, config.places_path) \
            if config.map.enabled or config.places.fix_addresses else None
        address_book = AddressBook(config.ranges_path)
        fixer = None
        if gazetteer and config.places.fix_addresses:
            window = config.map.incident_window_minutes * 60

            def near_incidents(call_time):
                return [(i["lat"], i["lon"]) for i in db.nearby_incidents(call_time - window, call_time)]

            fixer = AddressFixer(gazetteer, address_book, near_incidents)
        worker = TranscriptionWorker(config, db, backend_factory, codebook, placebook, corrections, hints, fixer)
        alerter = Alerter(config, db) if config.alerts.enabled else None
        speakers = None
        if config.speakers.enabled:
            speakers = SpeakerBook(db, config.speakers.dispatch_talkgroups, config.speakers.police_talkgroups)
            try:
                # Before the map starts, so incidents rebuilt from older calls know who was talking.
                labelled = speakers.backfill()
                if labelled:
                    log.info("Speakers: labelled %d older calls", labelled)
            except Exception:
                log.exception("Speakers: could not label older calls")
        summarizer = Summarizer(config, db) if config.summaries.enabled else None
        if config.tones.enabled:
            tone_finder = worker.on_audio = ToneFinder(config, db, alerter, audio.WHISPER_RATE)
        map_worker = None
        if config.map.enabled:
            geocoder = Geocoder(db, config.map.area, contact=config.map.contact_email) \
                if config.map.online_lookup else None
            map_worker = MapWorker(config, db, gazetteer, geocoder, address_book)

            def placed(call_id, incident_id, call):
                if alerter:
                    alerter.incident_call(call_id, incident_id, call["call_time"], call["transcript"])
                if summarizer:
                    summarizer.incident_changed(incident_id)

            map_worker.on_placed = placed
            zones = read_zones(config.alerts.zones) if alerter else []
            if zones:
                watch = ZoneWatch(zones, gazetteer, geocoder)

                def looked_at(call, incident_id):
                    found = watch.check(call, db.get_incident(incident_id) if incident_id else None)
                    if found:
                        zone, where, incident = found
                        alerter.zone_call(zone, where, call, incident)

                map_worker.on_done = looked_at
            if summarizer:
                # Calls moved between incidents by hand on the map.
                map_worker.on_changed = lambda ids: [summarizer.incident_changed(i) for i in ids]
            map_worker.start()

        units = UnitBoard(db, codebook)
        try:
            units.rebuild()
        except Exception:
            log.exception("Unit board: could not read recent calls")

        def done(call_id):
            if speakers:
                # First: the unit board, the map and the summaries read the label.
                try:
                    speakers.apply(call_id)
                except Exception:
                    log.exception("Speakers: call %s failed", call_id)
            if config.tones.enabled:
                tone_finder.learn_if_due()
            try:
                units.call_done(call_id)
            except Exception:
                log.exception("Unit board: call %s failed", call_id)
            if map_worker:
                map_worker.submit(call_id)
            if summarizer:
                summarizer.call_done(call_id)

        worker.on_done = done
        aircraft = AircraftWatch(config, db, alerter) if config.aircraft.enabled else None
        if aircraft:
            aircraft.start()
        traffic = Traffic511(config) if config.traffic.enabled else None
        if traffic:
            traffic.start()
        if summarizer:
            summarizer.start()
        recordings = RecordingCleanup(config, db)
        if recordings.enabled:
            recordings.start()
        worker.start()
        app.state.db = db
        app.state.worker = worker
        app.state.codebook = codebook
        app.state.corrections = corrections
        app.state.map_worker = map_worker
        app.state.alerter = alerter
        app.state.summarizer = summarizer
        app.state.aircraft = aircraft
        app.state.traffic = traffic
        app.state.units = units
        app.state.signal = SignalWatch(config.signal, db, alerter) if config.signal.enabled else None
        app.state.speakers = speakers
        log.info("Listening for SDRTrunk uploads at http://%s:%d/api/call-upload",
                 config.server.host, config.server.port)
        yield
        worker.stop()
        recordings.stop()
        if aircraft:
            aircraft.stop()
        if traffic:
            traffic.stop()
        if map_worker:
            map_worker.stop()
        if alerter:
            alerter.stop()
        if summarizer:
            summarizer.stop()
        db.close()

    app = FastAPI(title="SDRTrunk transcriber", lifespan=lifespan)
    app.add_middleware(_CompressText)

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

    def _key_ok(request: Request, body: dict, what: str) -> bool:
        # SDRTrunk on the same computer needs no key; anything else must send the upload key.
        if config.server.api_key and not _is_loopback(request.client.host if request.client else None):
            key = str(body.get("key") or "")
            if not hmac.compare_digest(key.encode(), config.server.api_key.encode()):
                log.warning("Rejected a %s with the wrong API key from %s", what, request.client.host)
                return False
        return True

    @app.post("/api/signal")
    async def signal_upload(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return PlainTextResponse("Expected a JSON body", status_code=400)
        if not isinstance(body, dict):
            return PlainTextResponse("Expected a JSON object", status_code=400)
        if not _key_ok(request, body, "signal report"):
            return PlainTextResponse("Invalid API key", status_code=401)
        watch = request.app.state.signal
        if watch is None:
            return {"stored": 0, "problems": [], "note": "[signal] enabled = false"}
        try:
            return watch.report(body)
        except ValueError as e:
            return PlainTextResponse(str(e), status_code=400)

    @app.get("/api/signal")
    def list_signal(request: Request, hours: float = 24):
        """The hourly rows SDRTrunk sent, newest hours last."""
        return request.app.state.db.signal_hours(int(time.time() - hours * 3600))

    @app.post("/api/page")
    async def page_upload(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return PlainTextResponse("Expected a JSON body", status_code=400)
        if not isinstance(body, dict):
            return PlainTextResponse("Expected a JSON object", status_code=400)

        if not _key_ok(request, body, "page"):
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
                                              call["talkgroup_group"], call_time=call.get("call_time"))
        if request.app.state.map_worker:
            # Which incident on the map each call is in, for the dashboard's "Show on map".
            for call in calls:
                call["incident_id"] = request.app.state.db.incident_of_call(call["id"])
        if request.app.state.summarizer:
            summaries = request.app.state.db.summaries("call", [call["id"] for call in calls])
            for call in calls:
                attach_summary(summaries, call)
        return _json_unless_unchanged(request, calls)

    @app.post("/api/calls/{call_id}/transcript")
    async def correct_transcript(request: Request, call_id: int):
        """Saves Ken's fix of a transcript and learns the replaced words for later calls."""
        db = request.app.state.db
        call = db.get_call(call_id)
        if call is None:
            raise HTTPException(404)
        if call["status"] not in ("done", "skipped", "failed"):
            raise HTTPException(409, "This call is still being transcribed")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected JSON")
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise HTTPException(400, "Expected {\"text\": ...}")
        text = re.sub(r"\s+", " ", text).strip()[:4000]
        original = call["transcript"] or ""
        learned = request.app.state.corrections.learn(original, text) if body.get("learn", True) else []
        db.update_call(call_id, transcript=text, corrected_at=int(time.time()), status="done", error=None)
        if request.app.state.speakers:
            request.app.state.speakers.apply(call_id, learn=False)
        # A fixed street name may put a call on the map that wasn't placed before.
        map_worker = request.app.state.map_worker
        if map_worker and call["map_status"] in ("none", "follow-up"):
            db.set_map_status(call_id, None)
            map_worker.submit(call_id)
        return {"id": call_id, "transcript": text, "learned": learned}

    @app.get("/api/corrections")
    def list_corrections(request: Request):
        return [c.row() for c in request.app.state.corrections.items()]

    @app.post("/api/corrections/forget")
    async def forget_correction(request: Request):
        """Removes a learned correction (the page's Undo)."""
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected JSON")
        heard, correct = (body.get("heard"), body.get("correct")) if isinstance(body, dict) else (None, None)
        if not isinstance(heard, str) or not isinstance(correct, str):
            raise HTTPException(400, "Expected {\"heard\": ..., \"correct\": ...}")
        if not request.app.state.corrections.forget(heard, correct):
            raise HTTPException(404)
        return {"forgot": {"heard": heard, "correct": correct}}

    @app.get("/api/calls/{call_id}/audio")
    def call_audio(request: Request, call_id: int):
        call = request.app.state.db.get_call(call_id)
        if call is None:
            raise HTTPException(404)
        if call.get("audio_deleted_at"):
            raise HTTPException(410, "Audio deleted to save space")
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

    @app.get("/units", response_class=HTMLResponse)
    def units_page():
        return (WEB_DIR / "units.html").read_text(encoding="utf-8")

    @app.get("/api/units")
    def list_units(request: Request, hours: float = 12):
        return request.app.state.units.board(hours)

    @app.post("/api/units/rebuild")
    def rebuild_units(request: Request, hours: float = 12):
        return {"calls": request.app.state.units.rebuild(hours)}

    @app.get("/api/aircraft")
    def list_aircraft(request: Request):
        aircraft = request.app.state.aircraft
        if aircraft is None:
            return {"enabled": False, "aircraft": []}
        return {"enabled": True, "aircraft": aircraft.aircraft(), "error": aircraft.error,
                "updated": aircraft.updated}

    def _traffic(request: Request) -> Traffic511:
        if request.app.state.traffic is None:
            raise HTTPException(404, "PennDOT traffic is turned off in config.toml ([traffic] enabled)")
        return request.app.state.traffic

    @app.get("/api/traffic")
    def list_traffic(request: Request):
        traffic = request.app.state.traffic
        if traffic is None:
            return {"enabled": False, "events": [], "cameras": []}
        return {"enabled": True, "events": traffic.events(), "cameras": traffic.cameras(),
                "match_miles": config.traffic.match_miles, "error": traffic.error, "updated": traffic.updated}

    @app.get("/api/traffic/cameras/{image_id}/image")
    def camera_image(request: Request, image_id: int):
        traffic = _traffic(request)
        if not traffic.known_image(image_id):
            raise HTTPException(404)
        try:
            content, kind = traffic.image(image_id)
        except Exception as e:
            raise HTTPException(502, f"511PA: {e}")
        return Response(content, media_type=kind, headers={"Cache-Control": "no-store"})

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
                                                  call["talkgroup_group"], call_time=call.get("call_time"))
                call["highlights"] = highlights(call["transcript"])
            # What the map colors and filters by: MVA first, since crashes go to every agency.
            incident["type"] = "mva" if incident["nature"] == "MVA" else incident["category"] or "other"
            # Police, fire and EMS working it, from the talkgroups and words of its calls.
            incident["services"] = services_of(incident["calls"])
            # Tone-outs just before the first call (pages carry no address, so this is by time only).
            incident["pages"] = db.pages(incident["first_time"] - 600, incident["first_time"] + 60, limit=10)
        if request.app.state.summarizer:
            summaries = db.summaries("incident", [incident["id"] for incident in incidents])
            for incident in incidents:
                calls = sorted({c["id"]: c for c in incident["calls"] if c["transcript"]}.values(),
                               key=lambda c: (c["call_time"], c["id"]))
                attach_summary(summaries, incident, incident_basis(calls))
        return _json_unless_unchanged(request, incidents)

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

    # Fixing which incident a call is in, from the map. Remembered when the map is rebuilt.

    def _edit(action, *args):
        try:
            return {"incidents": action(*args)}
        except KeyError as e:
            raise HTTPException(404, str(e.args[0]) if e.args else "not found")

    @app.post("/api/calls/{call_id}/move")
    async def move_call(request: Request, call_id: int):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("incident_id"), int):
            raise HTTPException(400, "send {\"incident_id\": <id>}")
        return _edit(_map_worker(request).move_call, call_id, body["incident_id"])

    @app.post("/api/calls/{call_id}/split")
    def split_call(request: Request, call_id: int):
        return _edit(_map_worker(request).split_call, call_id)

    @app.delete("/api/calls/{call_id}/place")
    def remove_call_from_map(request: Request, call_id: int):
        return _edit(_map_worker(request).remove_call, call_id)

    @app.post("/api/incidents/{incident_id}/merge")
    async def merge_incident(request: Request, incident_id: int):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("into"), int):
            raise HTTPException(400, "send {\"into\": <incident id>}")
        return _edit(_map_worker(request).merge, incident_id, body["into"])

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
            "summaries": _summary_status(request.app.state.summarizer),
        }

    def _summary_status(summarizer):
        if summarizer is None:
            return {"enabled": False}
        return {"enabled": True, "model": config.summaries.model, "reachable": summarizer.reachable,
                "queue": summarizer.queue_size(), "error": summarizer.last_error}

    return app
