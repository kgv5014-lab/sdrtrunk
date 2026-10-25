"""Loads config.toml and fills in defaults."""

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


@dataclass
class AudioConfig:
    # Before Whisper hears a call: keep only the voice band and set speech to a standard level.
    clean: bool = True
    # Also cut out steady tones (paging tones, alert beeps); Whisper tends to invent words over them.
    remove_tones: bool = True


@dataclass
class FasterWhisperConfig:
    model: str = "large-v3-turbo"
    # cuda | cpu
    device: str = "cuda"
    # CUDA device index, in the same order as `nvidia-smi -L`.
    device_index: int = 0
    # GTX 10xx (Pascal) cards: int8 or float32. float16 needs a 20xx or newer.
    compute_type: str = "int8"
    vad_filter: bool = True
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
    # A spoken four-digit time ("1346") within this many minutes of the call becomes "13:46". 0 = off.
    time_window_minutes: int = 3


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
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    faster_whisper: FasterWhisperConfig = field(default_factory=FasterWhisperConfig)
    whisper_cpp: WhisperCppConfig = field(default_factory=WhisperCppConfig)
    codes: CodesConfig = field(default_factory=CodesConfig)
    corrections: CorrectionsConfig = field(default_factory=CorrectionsConfig)
    places: PlacesConfig = field(default_factory=PlacesConfig)
    map: MapConfig = field(default_factory=MapConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    summaries: SummariesConfig = field(default_factory=SummariesConfig)
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
    def places_path(self) -> Path | None:
        return self._resolve(self.places.file) if self.places.file else None

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

    if path is None:
        return config

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    config.base_dir = path.resolve().parent

    for name, values in raw.items():
        section = getattr(config, name, None)
        if section is None or name == "base_dir":
            raise ValueError(f"Unknown section [{name}] in {path}")
        _apply(section, values, name)

    return config
