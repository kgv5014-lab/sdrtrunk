"""The tray icon's Stop all and Start menus.

    python -m sdrtrunk_transcriber.control stop-all
    python -m sdrtrunk_transcriber.control start everything|sdrtrunk|transcriber|ollama [--window]

stop-all asks the watchdog to stop every program and then itself (stop.flag), so it doesn't start them
again. That works without admin rights even when the boot task started them before sign-in. Anything
still running after that (or when no watchdog was running) is stopped directly.

start starts one program, or everything with the watchdog, either with no windows (the way boot-on.bat
does at boot) or with its normal windows (--window). A program started on its own isn't watched: if it
crashes, it stays stopped until it is started again. "Everything" starts the watchdog, which watches all.
Whatever copy is already running is stopped first (for Everything: the watchdog and all programs), so
switching between no windows and windows never leaves two copies fighting over the dongles or the port.

The tray icon shows what happened from control-result.txt (first line: ok or failed; then a
sentence). Each run is also written to watchdog.log.
"""

import argparse
import json
import ntpath
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import watchdog
from .watchdog import HOME, STOP_FLAG, STATUS_FILE

RESULT_FILE = HOME / "control-result.txt"
BOOT_TASK = "SDRTrunk Plus (boot)"
# The tray icon calls the watchdog gone when its status is older than this (seconds); so does this.
STATUS_STALE_S = 180
# How long to give the watchdog to stop everything: it looks for stop.flag every 30 s, then stops.
STOP_WAIT_S = 120

NAMES = {"everything": "Everything", "sdrtrunk": "SDRTrunk", "transcriber": "The Whisper transcriber",
         "ollama": "Summaries (Ollama)"}


def note(message: str):
    """A line in watchdog.log, opened and closed each time so the watchdog can still rotate it."""
    try:
        with open(HOME / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S},000 INFO [tray] {message}\n")
    except OSError:
        pass


class Control:
    def __init__(self, config, system=None, home: Path = HOME):
        self.config = config
        self.system = system or watchdog.System()
        self.home = home

    # What's running -------------------------------------------------------------------------------

    def watchdog_status(self) -> dict | None:
        """The watchdog's status, or None when no watchdog is running."""
        try:
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - float(status.get("time", 0)) > STATUS_STALE_S:
            return None
        return status

    def transcriber_url(self) -> str:
        return f"http://127.0.0.1:{self.config.server.port}/api/calls?limit=1"

    def ollama_url(self) -> str:
        return self.config.summaries.url.rstrip("/") + "/api/version"

    def sdrtrunk_bat(self) -> str:
        """sdr-trunk.bat: from config.toml, else where the watchdog last saw it, else from a running copy."""
        bat = self.config.watchdog.sdrtrunk.strip()
        if not bat:
            status = self.watchdog_status_any()
            bat = str(status.get("sdrtrunk_bat", "")) if status else ""
        if not bat:
            for _, _, java in self.system.sdrtrunk_processes() or []:
                candidate = ntpath.join(ntpath.dirname(ntpath.normpath(java)), "sdr-trunk.bat")
                if self.system.exists(candidate):
                    bat = candidate
                    break
        if bat and not self.system.exists(bat):
            bat = watchdog.moved_image_bat(self.system, bat) or bat
        return bat

    def watchdog_status_any(self) -> dict | None:
        """The last status the watchdog wrote, however old (for where SDRTrunk is)."""
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # Stop all -------------------------------------------------------------------------------------

    def stop_all(self, wait_s: float = STOP_WAIT_S, poll_s: float = 2) -> tuple[str, str]:
        if self.watchdog_status():
            note("Stop all: asking the watchdog to stop everything")
            STOP_FLAG.write_text("Stop all, from the tray icon\n", encoding="utf-8")
            waited = 0.0
            while waited < wait_s and self.watchdog_status():
                time.sleep(poll_s)
                waited += poll_s
            if self.watchdog_status():
                note("Stop all: the watchdog didn't answer; stopping the programs directly")
        # The watchdog is gone (or never ran): make sure nothing is left.
        STOP_FLAG.unlink(missing_ok=True)
        for pid, _, _ in self.system.sdrtrunk_processes() or []:
            self.system.kill_tree(pid)
        for pid in self.system.port_pids(self.config.server.port):
            self.system.kill_tree(pid)
        if self.config.summaries.enabled:
            self.system.kill_name("ollama.exe")
        time.sleep(3)

        left = []
        if self.system.sdrtrunk_processes():
            left.append("SDRTrunk")
        if self.system.port_pids(self.config.server.port):
            left.append("the transcriber")
        if self.watchdog_status():
            left.append("the watchdog")
        if left:
            message = (f"Couldn't stop {', '.join(left)}. Windows may need admin rights for them: "
                       "restart the PC, or stop them in Task Manager (Details).")
            note("Stop all: " + message)
            return "failed", message
        note("Stop all: everything is stopped")
        return "ok", "Everything is stopped. Use Start in this menu to start it again."

    # Start ----------------------------------------------------------------------------------------

    def start(self, what: str, window: bool) -> tuple[str, str]:
        how = "with its window" if window else "with no window"
        if what == "everything":
            return self.start_everything(window)
        if what not in NAMES:
            return "failed", f"Don't know how to start {what}."
        # Whatever copy is running now goes first, so the two don't clash (dongles, port 3000, the GPU).
        swapped = self.stop_one(what)
        if what == "sdrtrunk":
            bat = self.sdrtrunk_bat()
            if not bat or not self.system.exists(bat):
                return "failed", ("Can't tell where SDRTrunk is. Set [watchdog] sdrtrunk in config.toml "
                                  "to its sdr-trunk.bat.")
            if window:
                env = None
                # The launcher adds JAVA_OPTS to java's settings; a no-window one left over would hide it.
                os.environ.pop("JAVA_OPTS", None)
            else:
                opts = (os.environ.get("JAVA_OPTS", "") + " " + watchdog.HEADLESS_OPTS).strip()
                env = {"JAVA_OPTS": opts}
            self.system.launch(bat, ntpath.dirname(bat), env, hidden=not window)
        elif what == "transcriber":
            self.system.launch(str(self.home / "start-transcriber.bat"), str(self.home), hidden=not window)
        elif what == "ollama":
            self.system.launch(str(self.home / "start-ollama.bat"), str(self.home), hidden=not window)
        message = f"Starting {NAMES[what]} {how}."
        if swapped:
            message = f"Stopped the running {NAMES[what]} and started it again {how}."
        if not self.watchdog_status():
            message += " Nothing restarts it if it stops (Start > Everything does)."
        note(message)
        return "ok", message

    def stop_one(self, what: str) -> bool:
        """Stops the running copy of one program before Start opens a new one. True if one was running."""
        if what == "sdrtrunk":
            found = self.system.sdrtrunk_processes() or []
            for pid, _, _ in found:
                self.system.kill_tree(pid)
        elif what == "transcriber":
            found = self.system.port_pids(self.config.server.port)  # a frozen copy keeps the port too
            for pid in found:
                self.system.kill_tree(pid)
        elif what == "ollama":
            found = self.system.answers(self.ollama_url(), timeout=5)
            if found:
                self.system.kill_name("ollama.exe")
        else:
            return False
        if found:
            note(f"Start: stopping the running {NAMES[what]} first")
            time.sleep(watchdog.SETTLE_S)  # let the dongles, the port and the GPU go
        return bool(found)

    def start_everything(self, window: bool) -> tuple[str, str]:
        # Stop what's running first (the watchdog and every program), so nothing is left in the old mode.
        stopped = self.stop_all()
        if stopped[0] == "failed":
            return "failed", "Couldn't stop the running copies first, so nothing was started. " + stopped[1]
        STOP_FLAG.unlink(missing_ok=True)
        if window:
            self.system.launch(str(self.home / "start-everything.bat"), str(self.home), hidden=False)
            message = "Starting everything with windows (start-everything.bat)."
        elif self.run_boot_task():
            message = "Starting everything with no windows, the same way as at boot."
        else:
            # No boot task (boot-on.bat wasn't run), or Windows wouldn't start it: run the watchdog here.
            python = self.home / ".venv" / "Scripts" / "python.exe"
            exe = str(python) if python.exists() else sys.executable
            subprocess.Popen([exe, "-m", "sdrtrunk_transcriber.watchdog", "--config", "config.toml",
                              "--background"], cwd=str(self.home), creationflags=watchdog.NO_WINDOW)
            message = "Starting everything with no windows."
        note(message)
        return "ok", message + " The dot turns green when it's all running (a minute or two)."

    def run_boot_task(self) -> bool:
        try:
            out = subprocess.run(["schtasks", "/Run", "/TN", BOOT_TASK], capture_output=True, text=True,
                                 timeout=30, creationflags=watchdog.NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return out.returncode == 0


def write_result(result: tuple[str, str]):
    try:
        RESULT_FILE.write_text(f"{result[0]}\n{result[1]}\n", encoding="utf-8")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.control")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stop-all")
    start = sub.add_parser("start")
    start.add_argument("what", choices=sorted(NAMES))
    start.add_argument("--window", action="store_true", help="with its normal window")
    args = parser.parse_args()

    from .config import load_config

    config = load_config(args.config if args.config.exists() else None)
    control = Control(config)
    try:
        if args.command == "stop-all":
            result = control.stop_all()
        else:
            result = control.start(args.what, args.window)
    except Exception as e:  # shown by the tray icon, not lost in a window nobody sees
        result = ("failed", f"Something went wrong: {e}")
        note(result[1])
    write_result(result)
    print(result[1])
    sys.exit(0 if result[0] != "failed" else 2)


if __name__ == "__main__":
    main()
