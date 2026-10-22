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
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    faster_whisper: FasterWhisperConfig = field(default_factory=FasterWhisperConfig)
    whisper_cpp: WhisperCppConfig = field(default_factory=WhisperCppConfig)
    base_dir: Path = Path(".")

    @property
    def data_path(self) -> Path:
        return self._resolve(self.server.data_dir)

    @property
    def vocabulary_path(self) -> Path:
        return self._resolve(self.transcription.vocabulary_file)

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
