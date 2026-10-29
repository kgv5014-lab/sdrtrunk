"""Entry point.

    python -m sdrtrunk_transcriber                      run the service (reads config.toml)
    python -m sdrtrunk_transcriber --test-file call.mp3 transcribe one file and exit
    python -m sdrtrunk_transcriber --share setup        set the friends' map password and show the link
    python -m sdrtrunk_transcriber --share stop         turn the friends' map link off
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Make CUDA number GPUs the same way nvidia-smi does, so device_index matches `nvidia-smi -L`.
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")


def add_nvidia_dll_dirs():
    """On Windows, lets CTranslate2 find the cuBLAS/cuDNN DLLs installed by requirements-gpu.txt.

    The pip wheels put them in site-packages/nvidia/<library>/bin, which is not on the DLL search path.
    """
    if sys.platform != "win32":
        return
    import site
    roots = [Path(p) / "nvidia" for p in site.getsitepackages() + [site.getusersitepackages()]]
    for root in roots:
        for bin_dir in sorted(root.glob("*/bin")) if root.is_dir() else []:
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber")
    parser.add_argument("--config", type=Path, default=Path("config.toml"),
                        help="settings file (default: config.toml)")
    parser.add_argument("--test-file", type=Path,
                        help="transcribe one audio file with the configured backend, print it, and exit")
    parser.add_argument("--share", choices=["setup", "stop"],
                        help="set the password for the friends' map link, or turn that link off")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    add_nvidia_dll_dirs()

    from .config import load_config

    config_path = args.config if args.config.exists() else None
    if config_path is None:
        logging.warning("%s not found, using built-in defaults", args.config)
    config = load_config(config_path)

    if args.share:
        from . import share
        if args.share == "setup":
            sys.exit(share.setup(config.data_path, config.listen.public_url))
        sys.exit(share.stop(config.data_path))

    if args.test_file:
        sys.exit(test_file(config, args.test_file))

    import uvicorn
    from .server import create_app

    app = create_app(config)
    if config.listen.enabled and config.listen.public_port:
        from .live import start_public
        start_public(app, config.data_path, config.listen.public_port)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


def test_file(config, path: Path) -> int:
    from . import audio
    from .backends import create_backend
    from .codes import CodeBook
    from .places import PlaceBook
    from .worker import build_prompt, load_vocabulary

    placebook = PlaceBook(config.places_path, config.places.prompt_chars, config.places.areas)
    codebook = CodeBook(config.codes_path, config.codes.time_window_minutes, placebook.street_at)
    backend = create_backend(config)
    samples = audio.decode(path)
    started = time.perf_counter()
    result = backend.transcribe(samples, build_prompt(placebook.prompt_terms(), codebook.prompt_terms(),
                                                      load_vocabulary(config.vocabulary_path)))
    elapsed = time.perf_counter() - started
    text = codebook.normalize(placebook.normalize(result.text))
    print(f"{path.name}: {audio.duration_seconds(samples):.1f}s of audio in {elapsed:.2f}s "
          f"with {backend.name} ({backend.model}), confidence {result.confidence}")
    print(text or "(no speech found)")
    for label in codebook.annotate(text):
        print(f"  {label['code']}: {label['meaning']}")
    return 0


if __name__ == "__main__":
    main()
