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

import asyncio
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

from . import audio, incident_audio, live, share
from .alerts import Alerter
from .backends import create_backend
from .codes import CodeBook
from .corrections import CorrectionBook
from .hints import LearnedHints
from .places import PlaceBook
from .address_fix import AddressBook, AddressFixer
from .addresses import Gazetteer
from .aircraft import KINDS, AircraftWatch, incident_helicopters
from .aviation import AviationLog, tar1090_url
from .traffic import Traffic511
from .config import Config
from .db import Database
from .geocode import Geocoder
from .incidents import MapWorker, highlights, services_of
from .history import TYPES as HISTORY_TYPES, HistoryKeeper, to_csv as history_csv
from .zones import ZoneWatch, read_zones
from .speakers import SpeakerBook
from .recaps import RecapMaker, to_api as recap_to_api
from .summaries import Summarizer, attach as attach_summary, attach_updates, incident_basis
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
    """Gzips the pages and JSON replies, but not call audio, the live stream or camera pictures.

    Those are already compressed, and Safari plays audio with range requests that gzip would break.
    """

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1000)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if scope["type"] == "http" and not path.endswith(("/audio", "/image", ".mp3")):
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
        placebook = PlaceBook(config.places_path, config.places.prompt_chars, config.places.areas)
        codebook = CodeBook(config.codes_path, config.codes.time_window_minutes, placebook.street_at)
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
        # Catch-up jobs (calls that waited, map re-sort, summaries) for the window, /map and fetch-places.
        progress = worker.progress
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
        if summarizer:
            summarizer.progress = progress
        if config.tones.enabled:
            tone_finder = worker.on_audio = ToneFinder(config, db, alerter, audio.WHISPER_RATE, codebook)
        aviation = AviationLog(config, db, alerter=alerter) if config.aviation.enabled else None
        if aviation:
            worker.urgent = aviation.urgent
            try:
                # Before the map starts, so earlier airport calls aren't placed on it.
                marked = aviation.backfill()
                if marked:
                    log.info("Aviation: moved %d earlier calls to the aviation page", marked)
            except Exception:
                log.exception("Aviation: could not mark earlier calls")
        map_worker = None
        if config.map.enabled:
            geocoder = Geocoder(db, config.map.area, contact=config.map.contact_email) \
                if config.map.online_lookup else None
            map_worker = MapWorker(config, db, gazetteer, geocoder, address_book)
            map_worker.progress = progress

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
        # A lasting copy of every incident for the heat map (/history), kept when the map is rebuilt.
        history = HistoryKeeper(db) if map_worker else None
        if history:
            history.start()

        units = UnitBoard(db, codebook)
        try:
            units.rebuild()
        except Exception:
            log.exception("Unit board: could not read recent calls")

        def done(call_id):
            if aviation:
                try:
                    air = aviation.call_done(call_id)
                except Exception:
                    log.exception("Aviation: call %s failed", call_id)
                    air = (db.get_call(call_id) or {}).get("air")
                if air:
                    # Airport and helicopter radio: not for the unit board, the map or the summaries.
                    return
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
            if aircraft and not aviation:
                # A pilot saying "Life Flight 2" on the air band marks that helicopter as medevac (with the
                # aviation page on, AviationLog.call_done does this with the plane it matched).
                try:
                    aircraft.heard_on_radio(db.get_call(call_id) or {})
                except Exception:
                    log.exception("Helicopters: call %s failed", call_id)

        worker.on_done = done
        aircraft = AircraftWatch(config, db, alerter) if config.aircraft.enabled else None
        if aircraft:
            aircraft.start()
        if aviation:
            aviation.aircraft = aircraft
        traffic = Traffic511(config) if config.traffic.enabled else None
        if traffic:
            traffic.start()
        if summarizer:
            summarizer.start()
        recaps = RecapMaker(config, db, alerter) if config.recaps.enabled and config.map.enabled else None
        if recaps:
            recaps.start()
        recordings = RecordingCleanup(config, db)
        if recordings.enabled:
            recordings.start()
        worker.start()
        app.state.db = db
        app.state.worker = worker
        app.state.progress = progress
        app.state.codebook = codebook
        app.state.corrections = corrections
        app.state.map_worker = map_worker
        app.state.history = history
        app.state.alerter = alerter
        app.state.summarizer = summarizer
        app.state.recaps = recaps
        app.state.aircraft = aircraft
        app.state.aviation = aviation
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
        if history:
            history.stop()
        if map_worker:
            map_worker.stop()
        if alerter:
            alerter.stop()
        if summarizer:
            summarizer.stop()
        if recaps:
            recaps.stop()
        db.close()

    app = FastAPI(title="SDRTrunk transcriber", lifespan=lifespan)
    app.add_middleware(_CompressText)
    app.state.live = live.LiveHub() if config.listen.enabled else None

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

        call = {
            "call_time": call_time,
            "received_at": int(time.time()),
            "system_id": _text(form, "system"),
            "system_label": _text(form, "systemLabel"),
            "talkgroup": talkgroup,
            "talkgroup_label": config.channels.name_for(_int(_text(form, "frequency")))
                               or _text(form, "talkgroupLabel"),
            "talkgroup_group": _text(form, "talkgroupGroup"),
            "source": source,
            "talker_alias": _text(form, "talkerAlias"),
            "frequency": _int(_text(form, "frequency")),
            "patches": _text(form, "patches"),
            "audio_path": relative.as_posix(),
        }
        if request.app.state.aviation:
            # Airport and helicopter channels go to the aviation page, not the main call log.
            call["air"] = request.app.state.aviation.air_of(call)
        call_id = request.app.state.db.insert_call(call)
        request.app.state.worker.submit(call_id)
        if request.app.state.live:
            await live.add_call(request.app.state.live, {**call, "id": call_id}, target)
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
        _aircraft_of(calls)
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

    def _aircraft_of(calls: list[dict]):
        for call in calls:
            call["aircraft"] = json.loads(call["aircraft"]) if call.get("aircraft") else []

    @app.get("/aviation", response_class=HTMLResponse)
    def aviation_page():
        return (WEB_DIR / "aviation.html").read_text(encoding="utf-8")

    @app.get("/api/aviation/calls")
    def list_aviation_calls(request: Request, q: str = "", talkgroup: str = "", before_id: int | None = None,
                            limit: int = 100):
        """Calls on the airport and helicopter channels, newest first, with the aircraft each was matched to."""
        calls = request.app.state.db.search(q, talkgroup, "", before_id, limit, aviation=True)
        _aircraft_of(calls)
        emergency = request.app.state.aviation
        for call in calls:
            call["emergency"] = bool(emergency and emergency.urgent(call))
        return _json_unless_unchanged(request, {
            "enabled": request.app.state.aviation is not None, "adsb": request.app.state.aircraft is not None,
            "adsb_map_url": tar1090_url(config), "calls": calls})

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
        if request.app.state.aviation and call.get("air"):
            # A fixed call sign may match a different plane.
            request.app.state.aviation.call_done(call_id, alert=False)
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

    @app.get("/api/incidents/{incident_id}/play")
    async def incident_play(request: Request, incident_id: int):
        """Play all: where each call starts in the incident's joined audio (made here if needed)."""
        info = await asyncio.to_thread(incident_audio.build, request.app.state.db, data_path, incident_id)
        if info is None:
            raise HTTPException(404)
        url = f"api/incidents/{incident_id}/audio.mp3?v={info['version']}" if info["file"] else None
        return {"url": url, "seconds": info["seconds"], "calls": info["calls"], "missing": info["missing"]}

    @app.get("/api/incidents/{incident_id}/audio.mp3")
    async def incident_play_audio(request: Request, incident_id: int):
        info = await asyncio.to_thread(incident_audio.build, request.app.state.db, data_path, incident_id)
        if info is None or info["file"] is None:
            raise HTTPException(404)
        return FileResponse(info["file"], media_type="audio/mpeg")

    if config.listen.enabled:
        live.add_routes(app, app, data_path, public=False)

        @app.get("/api/listen/link")
        def listen_link():
            """The secret link for the public listen page (this route is never on the public port)."""
            key = live.read_key(data_path)
            base = config.listen.public_url.rstrip("/")
            # The friends' map link (share.py), once setup-share-link.bat has set a password.
            share_path = f"/g/{share.read_path(data_path)}/map" if share.is_on(data_path) else None
            return {"key": key, "port": config.listen.public_port,
                    "url": f"{base}/listen?key={key}" if base and config.listen.public_port else None,
                    "share_path": share_path,
                    "share_url": f"{base}{share_path}" if base and share_path else None}

        @app.post("/api/listen/new-link")
        def new_listen_link():
            """Makes a new secret, so an old link that got out stops working."""
            live.new_key(data_path)
            return listen_link()

    @app.get("/map", response_class=HTMLResponse)
    def map_page():
        return (WEB_DIR / "map.html").read_text(encoding="utf-8")

    # ---- incident history: heat map page and CSV download (history.py)

    @app.get("/history", response_class=HTMLResponse)
    def history_page():
        return (WEB_DIR / "history.html").read_text(encoding="utf-8")

    def _history_rows(request: Request, since: int | None, until: int | None, hidden: bool,
                      types: str | None = None, nature: str | None = None) -> list[dict]:
        keeper = request.app.state.history
        if keeper:
            try:
                keeper.sync_if_stale()
            except Exception:
                log.exception("Incident history: could not save")
        rows = request.app.state.db.history(since, until, hidden)
        if types:
            wanted = set(types.split(","))
            rows = [r for r in rows if r["type"] in wanted]
        if nature:
            rows = [r for r in rows if (r["nature"] or "") == nature]
        return rows

    @app.get("/api/history")
    def incident_history(request: Request, since: int | None = None, until: int | None = None,
                         hidden: bool = False):
        rows = _history_rows(request, since, until, hidden)
        oldest = rows[0]["first_time"] if rows else None
        return {
            "types": HISTORY_TYPES,
            "oldest": oldest,
            # Short rows: years of incidents still load quickly.
            "fields": ["id", "time", "end", "lat", "lon", "type", "nature", "label", "services", "calls", "hidden",
                       "incident_id", "dismissed"],
            "rows": [[r["first_call_id"], r["first_time"], r["last_time"], round(r["lat"], 5), round(r["lon"], 5),
                      r["type"], r["nature"], r["label"], r["services"] or "", r["calls"], r["hidden"],
                      r["incident_id"], r.get("dismissed")] for r in rows],
        }

    @app.get("/api/history.csv")
    def incident_history_csv(request: Request, since: int | None = None, until: int | None = None,
                             hidden: bool = False, types: str | None = None, nature: str | None = None):
        rows = _history_rows(request, since, until, hidden, types, nature)
        name = f"incident-history-{datetime.now():%Y-%m-%d}.csv"
        return Response(history_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/history/{first_call_id}")
    def incident_history_entry(request: Request, first_call_id: int):
        entry = request.app.state.db.history_entry(first_call_id)
        if entry is None:
            raise HTTPException(404)
        return entry

    def _map_worker(request: Request) -> MapWorker:
        if request.app.state.map_worker is None:
            raise HTTPException(404, "The incident map is turned off in config.toml ([map] enabled)")
        return request.app.state.map_worker

    @app.get("/checker", response_class=HTMLResponse)
    def checker_page():
        return (WEB_DIR / "checker.html").read_text(encoding="utf-8")

    @app.get("/api/checker")
    def checker_calls(request: Request):
        """The calls Ken types out for the Whisper check. Whisper's text is only sent once he has saved
        his own, so it can't steer what he hears."""
        out = []
        for row in request.app.state.db.checks():
            done = row["typed_at"] is not None
            out.append({"id": row["id"], "call_time": row["call_time"], "duration_s": row["duration_s"],
                        "talkgroup": row["talkgroup_label"] or row["talkgroup"],
                        "audio": not row["audio_deleted_at"], "done": done, "skipped": bool(row["skipped"]),
                        "typed": row["typed"], "whisper": row["transcript"] if done else None})
        return out

    @app.post("/api/checker/pick")
    async def checker_pick(request: Request):
        """Adds calls from the last two weeks to the checker (30 unless the body says otherwise)."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        count = body.get("count", 30) if isinstance(body, dict) else 30
        if not isinstance(count, int) or not 1 <= count <= 200:
            raise HTTPException(400, "count must be 1-200")
        now = int(time.time())
        return {"added": request.app.state.db.pick_checks(count, now - 14 * 86400, now)}

    @app.post("/api/checker/{call_id}")
    async def checker_save(request: Request, call_id: int):
        """Saves what Ken heard on a checker call, or {"skip": true} when he couldn't make it out."""
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected {\"text\": ...} or {\"skip\": true}")
        skipped = body.get("skip") is True
        text = body.get("text")
        if not skipped and not isinstance(text, str):
            raise HTTPException(400, "Expected {\"text\": ...} or {\"skip\": true}")
        typed = None if skipped else re.sub(r"\s+", " ", text).strip()[:4000]
        db = request.app.state.db
        if not db.save_check(call_id, typed, skipped, int(time.time())):
            raise HTTPException(404, "Not a checker call")
        call = db.get_call(call_id)
        return {"id": call_id, "typed": typed, "skipped": skipped, "whisper": call["transcript"]}

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

    @app.post("/api/aircraft/{hex_id}/kind")
    async def set_aircraft_kind(request: Request, hex_id: str):
        """Mark a helicopter medevac, police or other by hand; "auto" goes back to learning."""
        aircraft = request.app.state.aircraft
        if aircraft is None:
            raise HTTPException(404, "[aircraft] is off")
        body = await request.json()
        kind = body.get("kind") if isinstance(body, dict) else None
        if kind not in (*KINDS, "auto"):
            raise HTTPException(400, "send {\"kind\": \"medevac\" | \"police\" | \"other\" | \"auto\"}")
        return aircraft.set_kind(hex_id, None if kind == "auto" else kind)

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
        # Medevac (and police) helicopters that came down near it, from the ADS-B receiver.
        helicopters = incident_helicopters(db, [incident["id"] for incident in incidents],
                                           config.aircraft.learn_landings)
        for incident in incidents:
            incident["helicopters"] = helicopters.get(incident["id"], [])
        if request.app.state.summarizer:
            summaries = db.summaries("incident", [incident["id"] for incident in incidents])
            # What changed along the way ("Second alarm called"), written with each later summary.
            updates = db.incident_updates([incident["id"] for incident in incidents])
            for incident in incidents:
                attach_updates(updates, incident)
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

    # "Needs an address": dispatches the map couldn't place, placed by hand. Not on the friends' link
    # (share.py GUEST_API), so only Ken can see and change them.

    @app.get("/api/map/needs-address")
    def needs_address(request: Request, hours: float = 24):
        worker = _map_worker(request)
        since = int(time.time() - max(1.0, min(hours, 24 * 30)) * 3600)
        keep = ("id", "call_time", "talkgroup", "talkgroup_label", "talkgroup_group", "transcript", "duration_s",
                "audio_deleted_at", "map_status", "heard_place", "tones")
        return [{k: c.get(k) for k in keep} for c in worker.needs_address(since)]

    @app.get("/api/places/suggest")
    def suggest_places(request: Request, q: str = ""):
        return _map_worker(request).gazetteer.suggest(q[:120])

    @app.post("/api/calls/{call_id}/address")
    async def assign_address(request: Request, call_id: int):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("address"), str):
            raise HTTPException(400, "send {\"address\": \"123 Main St, Easton\"}")
        lat, lon = body.get("lat"), body.get("lon")
        point = (float(lat), float(lon)) if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else (None, None)
        try:
            incident_id = _map_worker(request).assign_address(call_id, body["address"], *point)
        except KeyError:
            raise HTTPException(404, "no such call")
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"incident": incident_id}

    @app.post("/api/calls/{call_id}/not-incident")
    def not_an_incident(request: Request, call_id: int):
        try:
            _map_worker(request).not_an_incident(call_id)
        except KeyError:
            raise HTTPException(404, "no such call")
        return {"removed": call_id}

    @app.post("/api/map/rebuild")
    def rebuild_map(request: Request):
        """Places every call again, e.g. after places.csv or places-geo.json changed."""
        if not _is_loopback(request.client.host if request.client else None):
            raise HTTPException(403, "Only from this computer")
        worker = _map_worker(request)
        worker.gazetteer.reload_if_changed()
        worker.rebuild()
        return {"queued": worker.queue_size()}

    @app.get("/api/progress")
    def progress_now(request: Request):
        """Catch-up jobs still running, e.g. {"jobs": [{"title": "Placing calls on the map", "done": 4210,
        "total": 9800, "percent": 42, "eta": "about 6 min left", ...}]}. With job=map, that one job even
        when finished (fetch-places.bat waits on it)."""
        progress = getattr(request.app.state, "progress", None)
        job = request.query_params.get("job")
        if job:
            return {"job": progress.get(job) if progress else None}
        jobs = progress.jobs() if progress else []
        summarizer = request.app.state.summarizer
        for j in jobs:
            if j["key"] == "summaries" and summarizer and summarizer.reachable is False:
                j["waiting"] = summarizer.last_error
        return {"jobs": jobs}

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

    @app.get("/api/map/mileposts")
    def map_mileposts(request: Request):
        """Every highway mile marker the map knows, for the map's mile marker layer."""
        worker = request.app.state.map_worker
        mileposts = worker.gazetteer.mileposts if worker else None
        return {"markers": mileposts.markers() if mileposts else []}

    # 12-hour summaries (recaps.py).

    def _recaps(request: Request) -> RecapMaker:
        if request.app.state.recaps is None:
            raise HTTPException(404, "12-hour summaries are off ([recaps] enabled, and [map] must be on)")
        return request.app.state.recaps

    @app.get("/api/recaps")
    def list_recaps(request: Request, limit: int = 30, before: int | None = None):
        _recaps(request)
        return _json_unless_unchanged(request, [recap_to_api(r) for r in request.app.state.db.recaps(limit, before)])

    @app.get("/api/recaps/{recap_id}")
    def get_recap(request: Request, recap_id: int):
        _recaps(request)
        row = request.app.state.db.get_recap(recap_id)
        if row is None:
            raise HTTPException(404)
        return recap_to_api(row)

    @app.post("/api/recaps")
    async def make_recap(request: Request):
        """Makes one now, for the last `hours` (12 when not given). No phone alert."""
        maker = _recaps(request)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        hours = body.get("hours", config.recaps.hours) if isinstance(body, dict) else config.recaps.hours
        if not isinstance(hours, (int, float)) or not 1 <= hours <= 24 * 31:
            raise HTTPException(400, "send {\"hours\": 1 to 744}")
        now = time.time()
        return await asyncio.to_thread(maker.make, now - hours * 3600, now)

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
