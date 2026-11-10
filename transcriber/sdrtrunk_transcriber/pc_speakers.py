"""Plays the live calls on this PC's speakers when SDRTrunk runs with no window.

    python -m sdrtrunk_transcriber.pc_speakers --config config.toml        (run by the tray icon)

Started before Windows sign-in (boot-on.bat), SDRTrunk can't reach the speakers: Windows only gives
sound to someone signed in. So the tray icon (tray.ps1) runs this player when you sign in; it plays
the same live stream as the phone's Listen page, from this PC's transcriber. While edit-mode.bat has the normal
SDRTrunk window open, that window plays the calls itself, so this one goes quiet. It also goes quiet
whenever the watchdog says SDRTrunk runs with its window (start-everything.bat), or the tray's
"PC speakers" is unticked: a copy the tray icon lost track of must not play the calls a second time.
"""

import argparse
import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

log = logging.getLogger("pc_speakers")

HOME = Path(__file__).resolve().parent.parent
EDIT_FLAG = HOME / "edit-mode.flag"
OFF_FLAG = HOME / "speakers-off.flag"
STATUS_FILE = HOME / "watchdog-status.json"
# The watchdog writes its status every 30 s or so; older than this and it isn't running.
STATUS_FRESH_S = 180
# Sound cards all take 48 kHz; the stream is resampled to it.
RATE = 48000
RETRY_S = 15
# Name on the Listen page's "who is listening" list, and in the transcriber log.
LISTENER_ID = "pc-speakers"


def stream_url(config) -> str:
    query = {"id": LISTENER_ID}
    talkgroups = [str(t).strip() for t in config.pc_speakers.talkgroups if str(t).strip()]
    if talkgroups:
        query["tg"] = ",".join(talkgroups)
    host = config.server.host if config.server.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    return f"http://{host}:{config.server.port}/listen/stream.mp3?{urlencode(query)}"


def find_device(sd, name: str):
    """The output device whose name contains `name`, or None for the Windows default."""
    if not name:
        return None
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_output_channels", 0) > 0 and name.lower() in device["name"].lower():
            return index
    log.warning('No sound output named like "%s"; using the Windows default', name)
    return None


def windowed(status_file: Path = STATUS_FILE, now=time.time) -> bool:
    """True while the watchdog is running SDRTrunk with its window, which plays the calls itself."""
    try:
        status = json.loads(status_file.read_text(encoding="utf-8"))
        return now() - float(status["time"]) <= STATUS_FRESH_S and not status.get("background")
    except (OSError, ValueError, KeyError, TypeError):
        return False


def should_be_quiet() -> bool:
    return EDIT_FLAG.exists() or OFF_FLAG.exists() or windowed()


def play(url: str, device, editing=should_be_quiet):
    """Plays the stream until it breaks. Returns normally only if the stream ends."""
    import av
    import sounddevice as sd

    container = av.open(url, timeout=(15, 60))
    try:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        with sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", device=device) as out:
            log.info("Playing the live calls on the PC speakers")
            quiet = False
            silent, checked = False, 0.0
            for frame in container.decode(audio=0):
                for piece in resampler.resample(frame):
                    data = piece.to_ndarray().tobytes()
                    if time.monotonic() - checked >= 1:  # the check reads files; not for every frame
                        silent, checked = editing(), time.monotonic()
                    if silent:
                        if not quiet:
                            log.info("The SDRTrunk window plays the calls (or PC speakers is off), "
                                     "so this goes quiet")
                        quiet = True
                        data = bytes(len(data))
                    elif quiet:
                        log.info("Playing again")
                        quiet = False
                    out.write(data)
    finally:
        container.close()


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.pc_speakers")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()
    # The tray icon runs this with no window, so it also logs to a file.
    handlers = [logging.handlers.RotatingFileHandler(HOME / "pc-speakers.log", maxBytes=500_000, backupCount=1,
                                                     encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    from .config import load_config

    config = load_config(args.config if args.config.exists() else None)
    if not config.listen.enabled:
        log.error("[listen] is off in config.toml, so there are no live calls to play. Turn it on first.")
        return
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:
        log.error("Can't reach the sound card (%s). Run the updater or install.bat again.", e)
        return

    url = stream_url(config)
    device = find_device(sd, config.pc_speakers.device)
    log.info("PC speakers: playing %s. Close this window to stop.", url.split("?")[0])
    told = False
    while True:
        try:
            play(url, device)
            told = False
        except KeyboardInterrupt:
            return
        except Exception as e:
            if not told:
                log.warning("No live calls to play yet (%s). Trying again every %d s "
                            "(the transcriber may still be starting).", e, RETRY_S)
                told = True
        time.sleep(RETRY_S)


if __name__ == "__main__":
    main()
