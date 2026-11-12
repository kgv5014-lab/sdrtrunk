"""Keeps Ollama, this service and SDRTrunk running on Windows: starts them, and restarts any of them
that crashes or freezes, with a phone alert each time.

    python -m sdrtrunk_transcriber.watchdog --config config.toml        (start-everything.bat)
    python -m sdrtrunk_transcriber.watchdog --config config.toml --background   (boot-start.bat)

--background is for the boot job (boot-on.bat), which runs everything before anyone signs in to
Windows: SDRTrunk is then started with no window. While edit-mode.bat has the normal SDRTrunk window
open (edit-mode.flag exists), SDRTrunk is left alone, and the no-window copy is started again as soon
as the window is closed. The tray icon's "Stop all" (stop.flag) stops every program and then the
watchdog itself, which exits with code 0 so the boot task isn't started again.

Only one watchdog runs at a time (it holds watchdog.lock), and one with windows always wins: started
while the no-window boot one is running, it has that one stop everything and starts it all again with
windows. A boot one gives way to a watchdog already running. If two SDRTrunks are ever running anyway
(they fight over the dongles, and one gets "access denied"), both are closed and one is started again.

Ollama and this service are checked by asking their web address for an answer. SDRTrunk has no web
address, so it is checked the way Task Manager does: is its java process there, and is its window
"Not responding". A program is only restarted after several checks in a row find it down, so a busy
moment or a manual restart doesn't set it off.
"""

import argparse
import json
import logging
import logging.handlers
import ntpath
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger("watchdog")

# The transcriber folder, where the .bat files are.
HOME = Path(__file__).resolve().parent.parent
# Tells start-transcriber.bat not to wait for a key press when the service stops, so it can be restarted.
WATCHDOG_ENV = "SDRTRUNK_WATCHDOG"
# Tells the batch files their window is hidden (start-ollama.bat then keeps Ollama in it too).
HIDDEN_ENV = "SDRTRUNK_HIDDEN"
# Between stopping a program and starting it again, so the tuners and the port are let go.
SETTLE_S = 5
# A program restarted too often is left alone this long.
PAUSE_S = 3600
# A transcriber that finished a call this recently is busy, not stuck, even when it is slow to answer: low
# memory or a pile of calls that waited can hold up its answers, and a restart would only make both worse.
TRANSCRIBER_BUSY_S = 180
# The file the transcriber touches after every call, in its data folder (worker.HEARTBEAT_FILE; not imported,
# so the watchdog doesn't load the audio and Whisper code).
HEARTBEAT_FILE = "transcriber-heartbeat"
# Made by edit-mode.bat while the normal SDRTrunk window is open for changes.
EDIT_FLAG = HOME / "edit-mode.flag"
# Made by the updater while it updates: nothing is checked or restarted until it's gone.
UPDATING_FLAG = HOME / "updating.flag"
# Made by the tray icon's "Restart everything".
RESTART_FLAG = HOME / "restart.flag"
# Made by the tray icon's "Stop all": the watchdog stops every program, then itself.
STOP_FLAG = HOME / "stop.flag"
# What the tray icon shows: each program's state, written after every check.
STATUS_FILE = HOME / "watchdog-status.json"
# Held (locked) by the one watchdog that is running, so a second one can't start its own programs.
LOCK_FILE = HOME / "watchdog.lock"
# How long a watchdog with windows waits for a no-window one to stop everything and hand over.
TAKEOVER_WAIT_S = 150
# Exit code when this watchdog didn't start because one with windows is already running.
ALREADY_RUNNING_EXIT = 2
# Makes SDRTrunk start with no window (its own built-in mode: playlist loaded, channels auto-started).
HEADLESS_OPTS = "-Djava.awt.headless=true"

# Lists running SDRTrunk java processes as "pid|responding|java.exe path".
SDRTRUNK_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='java.exe' OR Name='javaw.exe'\" | "
    "Where-Object { $_.CommandLine -like '*dsheirer*' -or $_.CommandLine -like '*sdr-trunk*' } | "
    "ForEach-Object { $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
    "if ($p) { '{0}|{1}|{2}' -f $_.ProcessId, $p.Responding, $_.ExecutablePath } }"
)


# Keeps the commands below from flashing a console window when run from the tray icon (pythonw).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class System:
    """The calls to Windows, in one place so the tests can stand in for them.

    hidden: start the programs' batch files in a console window nobody sees (the no-window mode, also
    when started from the tray icon while someone is signed in), instead of a normal window."""

    def __init__(self, hidden: bool = False):
        self.hidden = hidden

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float):
        time.sleep(seconds)

    def modified_ago(self, path: Path) -> float | None:
        """Seconds since a file last changed, or None when it isn't there."""
        try:
            return time.time() - path.stat().st_mtime
        except OSError:
            return None

    def answers(self, url: str, timeout: float = 20) -> bool:
        try:
            return requests.get(url, timeout=timeout).status_code < 500
        except requests.RequestException:
            return False

    def launch(self, bat: str, cwd: str, extra_env: dict | None = None, hidden: bool | None = None) -> int:
        """Runs a batch file in its own console window, as a double-click would (or a hidden one).
        Returns its pid."""
        hidden = self.hidden if hidden is None else hidden
        env = dict(os.environ, **{WATCHDOG_ENV: "1"}, **(extra_env or {}))
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
        startup = None
        if hidden:
            env[HIDDEN_ENV] = "1"
            startup = subprocess.STARTUPINFO() if hasattr(subprocess, "STARTUPINFO") else None
            if startup is not None:
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0  # SW_HIDE
        else:
            env.pop(HIDDEN_ENV, None)
        return subprocess.Popen(["cmd", "/c", bat], cwd=cwd, env=env, creationflags=flags,
                                startupinfo=startup).pid

    def kill_tree(self, pid: int):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)

    def kill_name(self, image: str):
        """Stops every process with this file name, e.g. ollama.exe."""
        subprocess.run(["taskkill", "/F", "/IM", image], capture_output=True, creationflags=NO_WINDOW)

    def port_pids(self, port: int) -> set[int]:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        return listening_pids(out, port)

    def sdrtrunk_processes(self) -> list[tuple[int, bool, str]] | None:
        """(pid, responding, java.exe path) for each SDRTrunk running, or None if Windows didn't say."""
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", SDRTRUNK_QUERY],
                                 capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return parse_sdrtrunk(out.stdout)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def listdir(self, path: str) -> list[str]:
        try:
            return os.listdir(path)
        except OSError:
            return []

    def editing(self) -> bool:
        """Is edit-mode.bat showing the normal SDRTrunk window right now?"""
        return EDIT_FLAG.exists()

    def updating(self) -> bool:
        return UPDATING_FLAG.exists()

    def take_restart_request(self) -> bool:
        """True once after the tray icon asked to restart everything."""
        try:
            RESTART_FLAG.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def take_stop_request(self) -> bool:
        """True once after the tray icon asked to stop everything."""
        try:
            STOP_FLAG.unlink()
            return True
        except OSError:
            return False

    def take_lock(self) -> bool:
        """Locks watchdog.lock for as long as this watchdog runs. False if another watchdog holds it.
        Windows lets the lock go by itself when the process ends, however it ends."""
        try:
            self._lock = open(LOCK_FILE, "a+b")
        except OSError:
            return True  # can't tell; better to watch than to start nothing
        try:
            if os.name == "nt":
                import msvcrt
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._lock.close()
            self._lock = None
            return False

    def read_status(self) -> dict | None:
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def ask_stop(self):
        try:
            STOP_FLAG.write_text("A watchdog with windows is taking over\n", encoding="utf-8")
        except OSError:
            pass

    def clear_status(self):
        """Tells the tray icon the watchdog has stopped (its dot turns grey)."""
        try:
            STATUS_FILE.unlink()
        except OSError:
            pass

    def write_status(self, status: dict):
        try:
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(status), encoding="utf-8")
            os.replace(tmp, STATUS_FILE)
        except OSError:
            pass


def listening_pids(netstat: str, port: int) -> set[int]:
    """Process ids listening on a TCP port, from `netstat -ano` output."""
    pids = set()
    for line in netstat.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "TCP" and parts[3] == "LISTENING" \
                and parts[1].rsplit(":", 1)[-1] == str(port) and parts[4].isdigit() and parts[4] != "0":
            pids.add(int(parts[4]))
    return pids


def parse_sdrtrunk(text: str) -> list[tuple[int, bool, str]]:
    found = []
    for line in text.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            found.append((int(parts[0]), parts[1].strip().lower() == "true", parts[2].strip()))
    return found


class Program:
    """One watched program. check() says "ok", "down", "frozen" or "unknown" (couldn't tell)."""
    name = ""
    # What a failed check means, for the log and the alert.
    down_reason = "stopped"

    def __init__(self, system: System):
        self.system = system
        self.pid = None             # the console window it was started in, when this started it
        self.started_at = None
        self.fails = 0
        self.last_state = "ok"
        self.restarts = deque()
        self.paused_until = 0.0
        # For the tray icon: ok, starting, down, frozen, unknown, editing, paused or updating.
        self.state = "starting"

    def check(self) -> str:
        raise NotImplementedError

    def start(self) -> bool:
        raise NotImplementedError

    def stop(self):
        if self.pid:
            self.system.kill_tree(self.pid)
            self.pid = None


class Ollama(Program):
    name = "Ollama"
    down_reason = "stopped answering"

    def __init__(self, system, url: str):
        super().__init__(system)
        self.url = url.rstrip("/") + "/api/version"

    def check(self):
        return "ok" if self.system.answers(self.url, timeout=10) else "down"

    def start(self):
        # start-ollama.bat stops any Ollama left over itself, then starts it on the GTX 1070.
        self.pid = self.system.launch(str(HOME / "start-ollama.bat"), str(HOME))
        return True

    def stop(self):
        pass  # start-ollama.bat does it, and its own window closes when it's done

    def shut_down(self):
        """Stop all: nothing is started again after this, so Ollama itself is stopped."""
        self.system.kill_name("ollama.exe")


class Transcriber(Program):
    name = "Transcriber"
    down_reason = "stopped answering"

    def __init__(self, system, port: int, heartbeat: Path | None = None):
        super().__init__(system)
        self.port = port
        # Answered by the web server alone (no database), so it only fails when the program is really stuck.
        self.url = f"http://127.0.0.1:{port}/api/alive"
        # Touched by the transcriber after every call (worker.HEARTBEAT_FILE).
        self.heartbeat = heartbeat
        self.told_busy = False

    def check(self):
        if self.system.answers(self.url, timeout=30):
            self.told_busy = False
            return "ok"
        ago = self.system.modified_ago(self.heartbeat) if self.heartbeat else None
        if ago is not None and ago < TRANSCRIBER_BUSY_S:
            if not self.told_busy:
                log.info("Transcriber is slow to answer but still transcribing calls; leaving it be")
                self.told_busy = True
            return "unknown"
        return "down"

    def start(self):
        self._free_port()
        self.pid = self.system.launch(str(HOME / "start-transcriber.bat"), str(HOME))
        return True

    def stop(self):
        super().stop()
        self._free_port()

    def _free_port(self):
        # An old copy left running (or frozen) keeps the port, and a new one can't start beside it.
        pids = self.system.port_pids(self.port)
        for pid in pids:
            log.info("Stopping process %s, which holds port %s", pid, self.port)
            self.system.kill_tree(pid)
        if pids:
            self.system.sleep(SETTLE_S)


def moved_image_bat(system, bat: str) -> str:
    """When a new SDRTrunk version is built, its folder name changes, e.g.
    build\\image\\sdr-trunk-windows-x86_64-v0.6.2-beta-1 becomes ...-v1.0.0-beta-1, so a saved path to
    sdr-trunk.bat stops working. Returns the sdr-trunk.bat in the folder that replaced it, or ""."""
    bin_dir = ntpath.dirname(ntpath.normpath(bat))
    version_dir = ntpath.dirname(bin_dir)
    image_dir = ntpath.dirname(version_dir)
    if ntpath.basename(image_dir).lower() != "image":
        return ""
    for name in sorted(system.listdir(image_dir), reverse=True):
        candidate = ntpath.join(image_dir, name, ntpath.basename(bin_dir), ntpath.basename(bat))
        if system.exists(candidate):
            return candidate
    return ""


class SDRTrunk(Program):
    name = "SDRTrunk"

    def __init__(self, system, bat: str, headless: bool = False):
        super().__init__(system)
        self.bat = bat
        self.headless = headless
        self.java_pids = []
        self.told_no_bat = False
        self.editing = False

    def check(self):
        found = self.system.sdrtrunk_processes()
        if found is None:
            return "unknown"
        self.java_pids = [pid for pid, _, _ in found]
        if not self.bat and found:
            # sdr-trunk.bat starts bin\..\bin\java.exe, so java.exe sits beside it.
            bat = ntpath.join(ntpath.dirname(ntpath.normpath(found[0][2])), "sdr-trunk.bat")
            if self.system.exists(bat):
                self.bat = bat
                log.info("SDRTrunk was started from %s; it is restarted from there", bat)
        if not found:
            if not self.bat:
                if not self.told_no_bat:
                    log.warning("SDRTrunk isn't running and [watchdog] sdrtrunk isn't set in config.toml, "
                                "so it can't be started. Start it yourself once and it is watched from then on.")
                    self.told_no_bat = True
                return "unknown"
            return "down"
        if len(found) > 1:
            # Two copies fight over the dongles: each gets "access denied" on the ones the other holds.
            return "doubled"
        return "ok" if any(responding for _, responding, _ in found) else "frozen"

    def start(self):
        if not self.bat:
            return False
        if not self.system.exists(self.bat):
            moved = moved_image_bat(self.system, self.bat)
            if moved:
                log.info("%s is gone (a new SDRTrunk version renames its folder); starting %s instead",
                         self.bat, moved)
                self.bat = moved
        if self.headless:
            # The launcher adds JAVA_OPTS to java's settings.
            opts = (os.environ.get("JAVA_OPTS", "") + " " + HEADLESS_OPTS).strip()
            self.pid = self.system.launch(self.bat, ntpath.dirname(self.bat), {"JAVA_OPTS": opts})
        else:
            self.pid = self.system.launch(self.bat, ntpath.dirname(self.bat))
        return True

    def stop(self):
        for pid in self.java_pids:
            self.system.kill_tree(pid)
        self.java_pids = []
        super().stop()


class Watchdog:
    def __init__(self, config, system: System | None = None, notify=None, background: bool = False):
        self.settings = config.watchdog
        self.system = system or System(hidden=background)
        self.notify = notify or (lambda title, message: None)
        self.background = background
        self.port = config.server.port
        self.told_updating = False
        self.stopped = False
        self.programs = []
        if config.summaries.enabled:
            self.programs.append(Ollama(self.system, config.summaries.url))
        self.programs.append(Transcriber(self.system, config.server.port, config.data_path / HEARTBEAT_FILE))
        self.programs.append(SDRTrunk(self.system, self.settings.sdrtrunk.strip(), headless=background))

    def start_all(self):
        """Starts whatever isn't running yet, Ollama first and SDRTrunk last, so each finds the one
        it sends to already up. Programs already running are left as they are and watched."""
        for program in self.programs:
            state = program.check()
            if state == "doubled":
                log.warning("%d copies of SDRTrunk are running (they fight over the dongles); closing them "
                            "all and starting one", len(program.java_pids))
                program.stop()
                self.system.sleep(SETTLE_S)
                state = "down"
            if state in ("ok", "frozen", "unknown"):
                if state != "unknown":
                    log.info("%s is already running", program.name)
                program.started_at = self.system.now()
                continue
            log.info("Starting %s", program.name)
            if program.start():
                program.started_at = self.system.now()
                program.state = "starting"
                self.system.sleep(15)
        self.write_status()

    def write_status(self):
        sdrtrunk = next((p for p in self.programs if isinstance(p, SDRTrunk)), None)
        self.system.write_status({
            "time": self.system.now(), "background": self.background, "port": self.port,
            "updating": self.told_updating, "sdrtrunk_bat": sdrtrunk.bat if sdrtrunk else "",
            "programs": [{"name": p.name, "state": p.state} for p in self.programs]})

    def restart_all(self):
        """The tray icon's "Restart everything": stops all, then starts them in order, without alerts."""
        log.info("Restarting everything (asked from the tray icon)")
        for program in reversed(self.programs):
            program.check()  # so SDRTrunk knows its java pids
            program.stop()
        self.system.sleep(SETTLE_S)
        for program in self.programs:
            program.fails = 0
            program.paused_until = 0.0
            if program.start():
                program.started_at = self.system.now()
                program.state = "starting"
                self.system.sleep(15)
        self.write_status()

    def stop_all(self):
        """The tray icon's "Stop all": stops every program, SDRTrunk first, and then the watchdog itself
        (run() returns), so nothing is started again until something starts it."""
        log.info("Stopping everything (asked from the tray icon)")
        for program in reversed(self.programs):
            program.check()  # so SDRTrunk knows its java pids
            program.stop()
            if isinstance(program, Ollama):
                program.shut_down()
            log.info("Stopped %s", program.name)
        self.stopped = True
        self.system.clear_status()
        log.info("Everything is stopped. Start it again from the tray icon (Start), or restart the PC.")

    def tick(self):
        if self.system.take_stop_request():
            self.stop_all()
            return
        if self.system.updating():
            if not self.told_updating:
                log.info("The updater is running: nothing is checked or restarted until it's done")
                self.told_updating = True
                for program in self.programs:
                    program.state = "updating"
                self.write_status()
            return
        if self.told_updating:
            log.info("The update is over")
            self.told_updating = False
        if self.system.take_restart_request():
            self.restart_all()
            return
        try:
            self._check_all()
        finally:
            self.write_status()

    def _check_all(self):
        now = self.system.now()
        for program in self.programs:
            if program.started_at is not None and now - program.started_at < self.settings.startup_s:
                if program.state not in ("ok", "starting"):
                    program.state = "starting"
                continue
            if now < program.paused_until:
                program.state = "paused"
                continue
            if isinstance(program, SDRTrunk) and self._editing(program):
                program.state = "editing" if program.editing else "starting"
                continue
            state = program.check()
            if state == "doubled":
                self.close_extra_copies(program)
                continue
            program.state = state
            if state in ("ok", "unknown"):
                if program.fails:
                    log.info("%s is fine again", program.name)
                program.fails = 0
                continue
            program.fails += 1
            program.last_state = state
            log.warning("%s %s (check %d of %d)", program.name,
                        "is not responding" if state == "frozen" else program.down_reason,
                        program.fails, self.settings.fail_checks)
            if program.fails >= self.settings.fail_checks:
                self.restart(program, state)

    def close_extra_copies(self, program: "SDRTrunk"):
        """More than one SDRTrunk is running (one started by hand, or left over from an earlier start):
        close them all and start one again, in this watchdog's mode, straight away."""
        log.warning("%d copies of SDRTrunk are running (they fight over the dongles: \"access denied\"); "
                    "closing them all and starting one", len(program.java_pids))
        program.fails = 0
        program.stop()
        self.system.sleep(SETTLE_S)
        if program.start():
            program.started_at = self.system.now()
            program.state = "starting"

    def _editing(self, program: "SDRTrunk") -> bool:
        """True while edit-mode.bat has the SDRTrunk window open. When it closes, the no-window copy is
        started right away instead of after several failed checks."""
        if self.system.editing():
            if not program.editing:
                log.info("Edit mode: SDRTrunk's window is open, so SDRTrunk isn't watched until it closes")
            program.editing = True
            program.fails = 0
            return True
        if program.editing:
            program.editing = False
            log.info("Edit mode is over")
            if program.check() == "down" and program.start():
                log.info("Started SDRTrunk again")
                program.started_at = self.system.now()
            return True
        return False

    def restart(self, program: Program, state: str):
        now = self.system.now()
        program.fails = 0
        while program.restarts and now - program.restarts[0] > 3600:
            program.restarts.popleft()
        if len(program.restarts) >= self.settings.max_restarts_per_hour:
            program.paused_until = now + PAUSE_S
            program.restarts.clear()
            log.error("%s was restarted %d times in an hour; leaving it alone for an hour",
                      program.name, self.settings.max_restarts_per_hour)
            self.notify(f"{program.name} keeps failing",
                        f"Restarted {self.settings.max_restarts_per_hour} times in an hour. "
                        "It needs a look on the PC; I'll try again in an hour.")
            return
        reason = "froze" if state == "frozen" else program.down_reason
        log.warning("Restarting %s, which %s", program.name, reason)
        program.stop()
        self.system.sleep(SETTLE_S)
        if not program.start():
            return
        program.started_at = self.system.now()
        program.state = "starting"
        program.restarts.append(now)
        self.notify(f"{program.name} restarted",
                    f"It {reason}, so it was restarted at {datetime.fromtimestamp(now):%I:%M %p}.".replace("at 0", "at "))

    def run(self):
        log.info("Watching %s. Close this window to stop watching (they keep running).",
                 ", ".join(p.name for p in self.programs))
        self.start_all()
        while not self.stopped:
            self.system.sleep(self.settings.check_s)
            try:
                self.tick()
            except Exception:
                log.exception("Watchdog check failed")


def claim(system, background: bool) -> bool:
    """Makes this the only watchdog. One with windows always wins: if a no-window one (the boot task) is
    running, it is asked to stop everything ("Stop all") and this one starts it all again with windows.
    A no-window one gives way to any watchdog already running, and so does a second one with windows.
    True when this watchdog may go on."""
    if system.take_lock():
        return True
    status = system.read_status()
    if background:
        log.info("Another watchdog is already running, so this no-window one leaves it be")
        return False
    if status and not status.get("background", False) and system.now() - float(status.get("time", 0)) < 300:
        log.info("A watchdog with windows is already running (look for the \"SDRTrunk watchdog\" window), "
                 "so this one doesn't start a second copy of everything")
        return False
    log.info("A no-window watchdog is running: asking it to stop everything, so this one starts it all "
             "with windows (up to two minutes)")
    system.ask_stop()
    waited = 0.0
    while waited < TAKEOVER_WAIT_S:
        system.sleep(3)
        waited += 3
        if system.take_lock():
            log.info("It has stopped; starting everything with windows")
            return True
    log.error("The no-window watchdog didn't stop. Use Stop all in the tray icon (or end python.exe and "
              "java.exe in Task Manager), then run start-everything.bat again.")
    return False


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.watchdog")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--background", action="store_true",
                        help="started at boot before sign-in: run SDRTrunk with no window")
    args = parser.parse_args()

    handlers = [logging.StreamHandler(),
                logging.handlers.RotatingFileHandler(HOME / "watchdog.log", maxBytes=1_000_000, backupCount=2,
                                                     encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    from .alerts import bark_push
    from .config import load_config

    config = load_config(args.config if args.config.exists() else None)
    alerts = config.alerts

    def notify(title, message):
        if config.watchdog.alert and alerts.enabled and alerts.bark_key:
            bark_push(alerts, title, message)

    # The same System for the whole run: it holds the lock that keeps a second watchdog out.
    system = System(hidden=args.background)
    if not claim(system, args.background):
        # 0 for the boot task (nothing failed; Task Scheduler mustn't retry). A window stays open to say why.
        sys.exit(0 if args.background else ALREADY_RUNNING_EXIT)

    # A "Stop all" the watchdog never got to (it wasn't running) mustn't stop this one straight away.
    STOP_FLAG.unlink(missing_ok=True)
    if args.background:
        # Left over from an edit-mode window that was open when the PC went down.
        EDIT_FLAG.unlink(missing_ok=True)
        UPDATING_FLAG.unlink(missing_ok=True)
        RESTART_FLAG.unlink(missing_ok=True)
        log.info("Background mode: SDRTrunk runs with no window (edit-mode.bat opens the normal one)")

    try:
        Watchdog(config, system=system, notify=notify, background=args.background).run()
    except KeyboardInterrupt:
        sys.exit(1)
    # Only "Stop all" ends the watchdog normally. Exit code 0 tells boot-start.bat (and Windows Task
    # Scheduler, which restarts the boot task when it fails) that this was on purpose.
    sys.exit(0)


if __name__ == "__main__":
    main()
