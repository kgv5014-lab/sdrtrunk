"""Loads config.toml and fills in defaults."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 3000
    # When set, uploads must carry the same API key that is entered in SDRTrunk.
    api_key: str = ""
    data_dir: str = "data"


@dataclass
class TranscriptionConfig:
    # faster-whisper | whisper-cpp | none
    backend: str = "faster-whisper"
    language: str = "en"
    # Calls shorter than this are logged but not transcribed (Whisper invents text on tiny clips).
    min_duration_s: float = 1.0
    beam_size: int = 5
    # One term per line: unit names, street names, 10-codes. Fed to Whisper as a prompt.
    vocabulary_file: str = "vocabulary.txt"
    # Segments Whisper itself thinks are probably not speech are dropped.
    no_speech_threshold: float = 0.6
    log_prob_threshold: float = -1.0
    # Drop text Whisper made up: whole calls of "Thank you." heard with low confidence, words
    # repeated over and over, counting runs, and echoes of the hint list.
    junk_filter: bool = True
    # A call that is only a stock phrase is dropped when Whisper's confidence is below this.
    stock_phrase_logprob: float = -0.7
    # With language "en": stop Whisper from writing other scripts (Korean, Chinese, Cyrillic, Arabic...)
    # and remove any that still get through.
    english_only: bool = True
    # Also forbid Whisper from even writing other scripts while it decodes. That list is thousands of
    # tokens and made every call 3-7 times slower on Ken's GTX 1070 Ti for the same text, so it is off;
    # english_only still removes any that get through.
    block_other_scripts: bool = False
    # Stops a call from running away into a loop ("RIT, RIT, RIT...") that holds up the queue for
    # minutes: Whisper may write at most this many tokens per second of audio (about 4 words a second).
    # 0 = no limit.
    max_tokens_per_second: float = 8.0
    # When a try comes out as a loop or very unsure, Whisper tries again at these temperatures (more
    # randomness each time). Whisper's own list goes to 1.0, six tries in all, which is slow and mostly
    # adds made-up text on radio audio.
    temperatures: list = field(default_factory=lambda: [0.0, 0.2, 0.4])
    # When calls pile up, ones on these talkgroups are transcribed first, in this order, so fire and EMS
    # dispatch (and their alerts) don't wait behind police chatter. Matched against the talkgroup's name,
    # group or number. Everything else waits its turn, oldest first.
    first_talkgroups: list = field(default_factory=lambda: ["Countywide", "Fire", "FD", "EMS", "Med", "Ops", "Tac"])


@dataclass
class AudioConfig:
    # Before Whisper hears a call: keep only the voice band and set speech to a standard level.
    clean: bool = True
    # Also cut out steady tones (paging tones, alert beeps); Whisper tends to invent words over them.
    remove_tones: bool = True
    # Also turn down the steady hiss of weak FM channels. Clean calls, such as P25, are left as they are.
    reduce_hiss: bool = True
    # Hide calls that are only a Morse-code station ID (Nancy FD's every half hour) from the call log.
    hide_station_ids: bool = True


@dataclass
class TonesConfig:
    # Finds two-tone pages in call recordings, for talkgroups where SDRTrunk's own paging decoder can't
    # hear them (P25 and other digital voice). Found pages are logged like SDRTrunk's and can alert.
    enabled: bool = True
    # Station names: tone_a,tone_b,name per line, the same format as SDRTrunk's paging_tones.csv.
    file: str = "paging_tones.csv"
    # Only calls whose talkgroup name or number contains one of these. Empty = every call.
    talkgroups: list = field(default_factory=list)
    # Name unlisted tones by themselves: when dispatch names the same station after more than learn_share
    # of a tone pair's tone-outs that name one (last learn_days, at least learn_min_seen times), the pair
    # is added to the station list.
    learn: bool = True
    learn_min_seen: int = 3
    learn_share: float = 0.5
    learn_days: int = 30


@dataclass
class FasterWhisperConfig:
    model: str = "large-v3-turbo"
    # cuda | cpu
    device: str = "cuda"
    # Which card Whisper uses when more than one is visible. start-transcriber.bat normally picks the
    # GTX 1070 Ti by name (see use-whisper-gpu.bat) and Whisper then sees only that card, so this is
    # ignored. Card numbers are only a fallback: CUDA and nvidia-smi can number cards differently.
    device_index: int = 0
    # GTX 10xx (Pascal) cards: int8 or float32. float16 needs a 20xx or newer.
    compute_type: str = "int8"
    vad_filter: bool = True
    # How sure the voice detector must be that a stretch is speech (0-1). Its own default, 0.5, threw
    # away real radio speech whole, leaving calls blank. 0.2 keeps it and still drops tones and silence.
    vad_threshold: float = 0.2
    # Pauses shorter than this stay part of the speech around them.
    vad_min_silence_ms: int = 300
    # Where downloaded models are cached; empty = the Hugging Face default.
    download_root: str = ""


@dataclass
class WhisperCppConfig:
    # whisper.cpp's bundled server: whisper-server -m ggml-large-v3-turbo.bin --port 8080
    url: str = "http://127.0.0.1:8080/inference"
    timeout_s: float = 120.0


@dataclass
class CodesConfig:
    # Local radio codes and call-sign numbering; see codes.example.csv. Empty = off.
    file: str = "codes.csv"
    # A spoken time ("1346", or "14-16" at the end of a dispatch) within this many minutes of the call
    # becomes "13:46". 0 = off.
    time_window_minutes: int = 3
    # Write those times the 12-hour way ("1:46 PM") in transcripts.
    twelve_hour: bool = True


@dataclass
class CorrectionsConfig:
    # Fixes made with the Edit button on the call log page, learned as word replacements for later
    # calls and given to Whisper as spelling hints. Hand edits apply without a restart. Empty = off.
    file: str = "corrections.csv"


@dataclass
class PlacesConfig:
    # Local streets, routes and landmarks; see places.example.csv and fetch-places.bat. Empty = off.
    file: str = "places.csv"
    # Room in Whisper's hint list for place names, in characters (the whole hint list is ~800).
    prompt_chars: int = 400
    # Talkgroup name (or part of it) -> municipalities whose streets to hint, e.g. {"County Fire" = [...]}.
    # Talkgroups whose name already contains a town ("Easton PD") don't need an entry.
    areas: dict = field(default_factory=dict)
    # House-number ranges for every street (US Census), written by fetch-places.bat. With them, a house
    # number (or street) Whisper got slightly wrong is snapped to a real address; see address_fix.py.
    ranges_file: str = "address-ranges.json"
    fix_addresses: bool = True


@dataclass
class HintsConfig:
    # Whisper hints learned from the call log (see hints.py): streets and units of incidents from the
    # last hour, and the ones each talkgroup names most. Off = the hint list works as before.
    enabled: bool = True
    # Room for them in Whisper's hint list, in characters. They go ahead of the places list, codes and
    # learned corrections, which are dropped first when the list is full; vocabulary.txt stays.
    prompt_chars: int = 160
    # Incidents with a call on the map this recently count as going on now.
    active_minutes: int = 60
    # How far back "most heard" looks, and how many times a name must be heard to count.
    history_days: int = 30
    min_count: int = 3
    # How often "most heard" is counted again.
    refresh_minutes: int = 30


@dataclass
class SelfCheckConfig:
    # Spot transcripts that look garbled and run Whisper again on them, slower and more carefully, keeping
    # whichever version looks better (see selfcheck.py). The call log marks them "rechecked" or "unsure".
    enabled: bool = True
    # Whisper's own confidence below this counts as unsure (the call log's "low confidence").
    low_confidence: float = -0.8
    # A street name not in places.csv, and a fire or EMS unit whose station isn't in codes.csv.
    streets: bool = True
    units: bool = True
    # Real radio speech is about 2-3 words a second.
    max_words_per_second: float = 5.0
    # Calls with at least this much speech (tones cut) but fewer words a second than below.
    min_words_speech_s: float = 8.0
    min_words_per_second: float = 0.3
    # Ask the [summaries] Ollama model about calls the quick checks pass but Whisper was only middling
    # sure of (confidence below ollama_below_confidence) with at least ollama_min_words words. Needs
    # [summaries] enabled; Ollama down = quick checks only.
    ollama: bool = True
    ollama_below_confidence: float = -0.45
    ollama_min_words: int = 8
    ollama_timeout_s: float = 30.0
    # The careful retry: a wider beam search and more temperature steps. Off = only mark the calls.
    retry: bool = True
    retry_beam_size: int = 10
    retry_temperatures: list = field(default_factory=lambda: [0.0, 0.2, 0.4, 0.6, 0.8])


@dataclass
class SpeakersConfig:
    # Who is talking in each call, dispatch or a unit (see speakers.py). Off = no labels, and the map,
    # unit board and summaries read calls as before.
    enabled: bool = True
    # Talkgroups (or part of their name) that only dispatch talks on.
    dispatch_talkgroups: list = field(default_factory=lambda: ["Countywide"])
    # Talkgroups (or part of their name) that are police: there a unit says its number before County.
    police_talkgroups: list = field(default_factory=lambda: ["PD", "Police", "Sheriff"])


@dataclass
class MapConfig:
    # Possible incidents on a map at /map, from addresses heard in transcripts.
    enabled: bool = True
    # Road shapes written by fetch_places.py next to places.csv; needed to place intersections.
    geo_file: str = "places-geo.json"
    # Look up house-number addresses with OpenStreetMap's free search (one a second, results cached).
    online_lookup: bool = True
    # Optional: an email address sent with lookups, as OpenStreetMap asks heavy users to.
    contact_email: str = ""
    # South, west, north, east. Lookups outside this box are ignored. Easton to Allentown, PA.
    area: list = field(default_factory=lambda: [40.50, -75.62, 40.78, -75.17])
    # Calls naming places this close together, this close in time, are one incident.
    incident_window_minutes: int = 45
    incident_radius_m: int = 500
    # Calls on these kinds of channel join the incident last sent there ("switch to Tac 2"), for this long
    # after its latest call. Ops channels carry many incidents at once, so they are not followed.
    follow_channels: list = field(default_factory=lambda: ["Tac", "Fireground"])
    channel_window_minutes: int = 60
    # Map background tiles.
    tiles_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


@dataclass
class AircraftConfig:
    # Helicopters on the incident map, from an ADS-B receiver on the network (readsb, tar1090, ultrafeeder).
    enabled: bool = False
    # The receiver's aircraft.json. Ultrafeeder serves it on port 8080.
    url: str = "http://192.168.50.102:8080/data/aircraft.json"
    poll_s: float = 5.0
    # Show every helicopter. Off = only aircraft on the watch list.
    all_helicopters: bool = True
    # Aircraft to always show and name: [{match = "N123AB", name = "St. Luke's 1"}]. match is a tail
    # number, flight callsign or ICAO hex code, and may end in * ("LIFE*").
    watch: list = field(default_factory=list)
    # A helicopter this low (feet), within this distance of an incident from the last incident_hours, is
    # marked as landing there.
    landing_altitude_ft: int = 1500
    landing_radius_miles: float = 1.0
    incident_hours: float = 2.0
    # Also send a phone alert when that happens (needs [alerts] set up).
    alert_landing: bool = False


@dataclass
class TrafficConfig:
    # PennDOT crashes, closures and roadwork, and traffic cameras, from the public 511PA website.
    enabled: bool = False
    # South, west, north, east. Empty = the same box as [map] area.
    area: list = field(default_factory=list)
    poll_s: float = 180.0
    cameras: bool = True
    # The camera list hardly changes; it is read again this often.
    cameras_refresh_hours: float = 24.0
    # A radio incident this close to a PennDOT crash or closure shows it.
    match_miles: float = 0.5


@dataclass
class RecordingsConfig:
    # Call audio older than this many days is deleted. 0 = keep it by age.
    max_age_days: float = 7.0
    # When all call audio together is over this size, the oldest is deleted first. 0 = no size limit.
    max_total_gb: float = 1.0
    # How often to check, in minutes.
    check_minutes: float = 60.0


@dataclass
class AlertsConfig:
    # Phone alerts through the ntfy app, answered by this service itself (see the README).
    enabled: bool = False
    # The topic name entered in the ntfy app. Letters, digits, - and _ only.
    topic: str = "sdrtrunk-alerts"
    # This PC's address exactly as entered in the ntfy app, e.g. "https://desktop.tail1234.ts.net".
    base_url: str = ""
    # iPhones can only be woken up through Apple. This server passes on a wake-up ping holding just
    # a random id; the phone then fetches the alert from this PC. Empty = no ping (Android only).
    wakeup_url: str = "https://ntfy.sh"
    # Incident types (as shown on the map) that send an alert.
    incidents: list = field(default_factory=lambda: ["MVA", "Structure fire"])
    # Tone-outs whose station name (from SDRTrunk's paging_tones.csv) contains one of these.
    stations: list = field(default_factory=list)
    # Watch zones: any call placed within radius_miles of a spot alerts, whatever it is about. Each is
    # {name, lat, lon (or address, looked up online), radius_miles, prefix (put first in the alert)}.
    zones: list = field(default_factory=list)
    # Calls and tone-outs older than this don't alert (e.g. old calls placed after a restart).
    max_age_minutes: int = 15
    # Bark iPhone app, instead of or as well as ntfy. The device key from the app's push URL
    # (https://api.day.app/<key>/...). Empty = Bark off.
    bark_key: str = ""
    bark_server: str = "https://api.day.app"
    # The key from Bark's Push Encryption settings (16, 24 or 32 characters for AES128/192/256), so
    # only the phone can read the alert. Empty sends the alert text readable by Bark's server.
    bark_encryption_key: str = ""
    # The mode picked in Bark's Push Encryption settings: GCM or CBC.
    bark_encryption_mode: str = "GCM"


@dataclass
class SignalConfig:
    # SDRTrunk sends each dongle's signal quality every hour (decode rate, lost sync, signal and noise levels,
    # drift, USB errors). A phone alert goes out (needs [alerts] set up) when a dongle gets worse than its
    # own normal, once per problem until it clears.
    enabled: bool = True
    alert: bool = True
    # "Normal" is the median of the hours this far back. Needs this many hours of history first.
    baseline_days: float = 7.0
    min_history_hours: int = 6
    # Alert when the share of messages that decode drops this many percentage points below normal.
    decode_drop_points: float = 15.0
    # Alert when a control channel loses sync for this many minutes in an hour (and 3x its normal).
    sync_lost_minutes: float = 2.0
    # Alert when the signal on calls is this many dB weaker than normal, or the noise this many dB higher.
    signal_drop_db: float = 10.0
    noise_rise_db: float = 10.0
    # Alert when the dongle's frequency correction moves this much in an hour, or sits this far from normal.
    ppm_jump: float = 1.0
    ppm_drift: float = 2.0
    # Alert at this many USB errors in an hour.
    usb_errors: int = 5
    # Hourly rows older than this are deleted.
    keep_days: int = 90


@dataclass
class SummariesConfig:
    # Short summaries and tags for incidents and long calls, from a local Ollama model (see the README).
    enabled: bool = False
    url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b"
    # Calls with at least this many words also get their own one-line summary. 0 = incidents only.
    call_min_words: int = 25
    # Wait this long after a call joins an incident before summarizing it, so a burst is read together.
    delay_s: float = 20.0
    # Incident summaries are kept to about this many words.
    incident_words: int = 40
    # A long incident: the first 3 calls and the latest ones, this many in all, are read.
    max_calls: int = 30
    timeout_s: float = 180.0
    # How long Ollama keeps the model loaded on the GPU after the last summary ("-1" = always).
    keep_alive: str = "-1"
    # On start, summarize incidents and calls from this far back that have none yet.
    backlog_hours: int = 24


@dataclass
class WatchdogConfig:
    # start-everything.bat starts Ollama (when [summaries] is on), this service and SDRTrunk, and
    # restarts any of them that crashes or freezes (see "Auto-restart" in the README).
    # SDRTrunk's sdr-trunk.bat. Empty = the one a running SDRTrunk was started from.
    sdrtrunk: str = ""
    # Look at each program this often.
    check_s: int = 30
    # Restart after this many checks in a row find a program frozen or not answering.
    fail_checks: int = 4
    # Give a program this long to start before checking it (Whisper takes a while to load).
    startup_s: int = 180
    # A program restarted this many times within an hour is left alone for an hour, with one alert.
    max_restarts_per_hour: int = 4
    # Send a phone alert (Bark, from [alerts]) on each restart.
    alert: bool = True


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    tones: TonesConfig = field(default_factory=TonesConfig)
    faster_whisper: FasterWhisperConfig = field(default_factory=FasterWhisperConfig)
    whisper_cpp: WhisperCppConfig = field(default_factory=WhisperCppConfig)
    codes: CodesConfig = field(default_factory=CodesConfig)
    corrections: CorrectionsConfig = field(default_factory=CorrectionsConfig)
    places: PlacesConfig = field(default_factory=PlacesConfig)
    hints: HintsConfig = field(default_factory=HintsConfig)
    self_check: SelfCheckConfig = field(default_factory=SelfCheckConfig)
    speakers: SpeakersConfig = field(default_factory=SpeakersConfig)
    map: MapConfig = field(default_factory=MapConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    aircraft: AircraftConfig = field(default_factory=AircraftConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    summaries: SummariesConfig = field(default_factory=SummariesConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    recordings: RecordingsConfig = field(default_factory=RecordingsConfig)
    base_dir: Path = Path(".")

    @property
    def data_path(self) -> Path:
        return self._resolve(self.server.data_dir)

    @property
    def vocabulary_path(self) -> Path:
        return self._resolve(self.transcription.vocabulary_file)

    @property
    def codes_path(self) -> Path | None:
        return self._resolve(self.codes.file) if self.codes.file else None

    @property
    def corrections_path(self) -> Path | None:
        return self._resolve(self.corrections.file) if self.corrections.file else None

    @property
    def tones_path(self) -> Path | None:
        return self._resolve(self.tones.file) if self.tones.file else None

    @property
    def places_path(self) -> Path | None:
        return self._resolve(self.places.file) if self.places.file else None

    @property
    def ranges_path(self) -> Path | None:
        return self._resolve(self.places.ranges_file) if self.places.ranges_file else None

    @property
    def geo_path(self) -> Path | None:
        return self._resolve(self.map.geo_file) if self.map.geo_file else None

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path


def _apply(section, values: dict, name: str):
    for key, value in values.items():
        if not hasattr(section, key):
            raise ValueError(f"Unknown setting [{name}] {key}")
        setattr(section, key, type(getattr(section, key))(value))


def load_config(path: Path | None) -> Config:
    config = Config()
    if path is not None:
        _load_file(config, path)
    # use-whisper-gpu.bat found the card by name and left Whisper only that one to see
    index = os.environ.get("WHISPER_DEVICE_INDEX", "")
    if index.isdigit():
        config.faster_whisper.device_index = int(index)
    return config


def _load_file(config: Config, path: Path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    config.base_dir = path.resolve().parent

    for name, values in raw.items():
        section = getattr(config, name, None)
        if section is None or name == "base_dir":
            raise ValueError(f"Unknown section [{name}] in {path}")
        _apply(section, values, name)
