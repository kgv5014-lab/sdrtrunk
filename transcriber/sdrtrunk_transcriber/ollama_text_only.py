"""Remakes the [summaries] Ollama model without its image-reading add-on, run by start-ollama.bat.

Models such as Qwen 3.5 come with a vision projector ("mmproj", about 1 GB) that Ollama loads onto the
graphics card next to the model. Summaries and self-checks only send text, so the add-on only takes room:
with it, the model no longer fit on an 8 GB card and part of it ran on the CPU.

This keeps a copy of the model as it was ("<model>-with-images", sharing the same files on disk) and remakes
the model under its own name from the same files minus the add-on. Once done, the model has no add-on and
this does nothing. Any failure leaves the model as it was.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from .config import load_config

BACKUP_SUFFIX = "-with-images"


def has_image_addon(show: dict) -> bool:
    """True when Ollama's /api/show answer lists a separate vision projector file.

    Only the separate file counts: a model whose own file holds the vision parts can't be split here,
    and checking its "vision" capability would remake it at every start.
    """
    return bool(show.get("projector_info"))


def text_only_modelfile(modelfile: str, size_of=None) -> str:
    """The modelfile with only the largest FROM file kept.

    `ollama show --modelfile` lists the model file and its projector each as a FROM line; the model is
    several GB and the projector under one, so the largest file is the model.
    """
    size_of = size_of or os.path.getsize
    lines = modelfile.splitlines()
    froms = [(i, line.split(None, 1)[1].strip()) for i, line in enumerate(lines)
             if line.upper().startswith("FROM ") and len(line.split(None, 1)) == 2]
    if not froms:
        raise ValueError("the modelfile has no FROM line")
    sizes = {}
    for _, path in froms:
        try:
            sizes[path] = size_of(path)
        except OSError:
            sizes[path] = -1
    keep = max(froms, key=lambda f: sizes[f[1]])
    if sizes[keep[1]] < 0:
        raise ValueError("none of the model's files were found on disk")
    drop = {i for i, _ in froms if i != keep[0]}
    return "\n".join(line for i, line in enumerate(lines) if i not in drop) + "\n"


def show(url: str, model: str) -> dict:
    r = requests.post(f"{url.rstrip('/')}/api/show", json={"model": model}, timeout=30)
    r.raise_for_status()
    return r.json()


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ollama", *args], capture_output=True, text=True, timeout=1800)


def drop_image_addon(url: str, model: str) -> str:
    """Returns a line to print. Raises on failure, leaving the model as it was."""
    info = show(url, model)
    if not has_image_addon(info):
        return f"{model} has no image add-on; nothing to do."
    backup = model.split(":")[0] + BACKUP_SUFFIX
    try:
        show(url, backup)
    except requests.HTTPError:
        done = run("cp", model, backup)
        if done.returncode != 0:
            raise RuntimeError(f"could not copy {model} to {backup}: {done.stderr.strip()}")
    modelfile = text_only_modelfile(info.get("modelfile") or "")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Modelfile"
        path.write_text(modelfile, encoding="utf-8")
        done = run("create", model, "-f", str(path))
    if done.returncode != 0:
        raise RuntimeError(f"ollama create failed: {(done.stderr or done.stdout).strip()[-500:]}")
    if has_image_addon(show(url, model)):
        raise RuntimeError("the remade model still has the image add-on")
    return (f"Remade {model} without its image add-on (about 1 GB less on the graphics card). "
            f"The old one is kept as {backup}.")


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path("config.toml")
    s = load_config(path if path.exists() else None).summaries
    if not s.enabled or not s.drop_image_addon:
        return 0
    try:
        print(drop_image_addon(s.url, s.model))
    except Exception as e:  # never stop start-ollama.bat; the model just keeps its add-on
        print(f"Could not remove the image add-on from {s.model} ({e}). It keeps working as before.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
