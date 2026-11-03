"""Prints the [summaries] model from config.toml, so start-ollama.bat downloads the model the transcriber uses."""
import sys
from pathlib import Path

from .config import load_config

if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config.toml")
    print(load_config(path if path.exists() else None).summaries.model)
