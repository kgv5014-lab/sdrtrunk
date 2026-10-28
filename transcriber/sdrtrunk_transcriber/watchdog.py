"""Keeps Ollama, this service and SDRTrunk running on Windows: starts them, and restarts any of them
that crashes or freezes, with a phone alert each time.

    python -m sdrtrunk_transcriber.watchdog --config config.toml        (start-everything.bat)

Ollama and this service are checked by asking their web address for an answer. SDRTrunk has no web
address, so it is checked the way Task Manager does: is its java process there, and is its window
"Not responding". A program is only restarted after several checks in a row find it down, so a busy
moment or a manual restart doesn't set it off.
"""

import argparse
import logging
import logging.handlers
import ntpath
import os
import subprocess
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
# Between stopping a program and starting it again, so the tuners and the port are let go.
SETTLE_S = 5
# A program restarted too often is left alone this long.
PAUSE_S = 3600

# Lists running SDRTrunk java processes as "pid|responding|java.exe path".
SDRTRUNK_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='java.exe' OR Name='javaw.exe'\" | "
    "Where-Object { $_.CommandLine -like '*dsheirer*' -or $_.CommandLine -like '*sdr-trunk*' } | "
    "ForEach-Object { $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
    "if ($p) { '{0}|{1}|{2}' -f $_.ProcessId, $p.Responding, $_.ExecutablePath } }"
)


class System:
    """The calls to Windows, in one place so the tests can stand in for them."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float):
        time.sleep(seconds)

    def answers(self, url: str, timeout: float = 20) -> bool:
        try:
            return requests.get(url, timeout=timeout).status_code < 500
        except requests.RequestException:
            return False

    def launch(self, bat: str, cwd: str) -> int:
        """Runs a batch file in its own console window, as a double-click would. Returns its pid."""
        env = dict(os.environ, **{WATCHDOG_ENV: "1"})
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
        return subprocess.Popen(["cmd", "/c", bat], cwd=cwd, env=env, creationflags=flags).pid

    def kill_tree(self, pid: int):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)

    def port_pids(self, port: int) -> set[int]:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
        return listening_pids(out, port)

    def sdrtrunk_processes(self) -> list[tuple[int, bool, str]] | None:
        """(pid, responding, java.exe path) for each SDRTrunk running, or None if Windows didn't say."""
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", SDRTRUNK_QUERY],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return parse_sdrtrunk(out.stdout)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)


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


class Transcriber(Program):
    name = "Transcriber"
    down_reason = "stopped answering"

    def __init__(self, system, port: int):
        super().__init__(system)
        self.port = port
        self.url = f"http://127.0.0.1:{port}/api/calls?limit=1"

    def check(self):
        return "ok" if self.system.answers(self.url, timeout=20) else "down"

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


class SDRTrunk(Program):
    name = "SDRTrunk"

    def __init__(self, system, bat: str):
        super().__init__(system)
        self.bat = bat
        self.java_pids = []
        self.told_no_bat = False

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
        return "ok" if any(responding for _, responding, _ in found) else "frozen"

    def start(self):
        if not self.bat:
            return False
        self.pid = self.system.launch(self.bat, ntpath.dirname(self.bat))
        return True

    def stop(self):
        for pid in self.java_pids:
            self.system.kill_tree(pid)
        self.java_pids = []
        super().stop()


class Watchdog:
    def __init__(self, config, system: System | None = None, notify=None):
        self.settings = config.watchdog
        self.system = system or System()
        self.notify = notify or (lambda title, message: None)
        self.programs = []
        if config.summaries.enabled:
            self.programs.append(Ollama(self.system, config.summaries.url))
        self.programs.append(Transcriber(self.system, config.server.port))
        self.programs.append(SDRTrunk(self.system, self.settings.sdrtrunk.strip()))

    def start_all(self):
        """Starts whatever isn't running yet, Ollama first and SDRTrunk last, so each finds the one
        it sends to already up. Programs already running are left as they are and watched."""
        for program in self.programs:
            state = program.check()
            if state in ("ok", "frozen", "unknown"):
                if state != "unknown":
                    log.info("%s is already running", program.name)
                program.started_at = self.system.now()
                continue
            log.info("Starting %s", program.name)
            if program.start():
                program.started_at = self.system.now()
                self.system.sleep(15)

    def tick(self):
        now = self.system.now()
        for program in self.programs:
            if program.started_at is not None and now - program.started_at < self.settings.startup_s:
                continue
            if now < program.paused_until:
                continue
            state = program.check()
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
        program.restarts.append(now)
        self.notify(f"{program.name} restarted",
                    f"It {reason}, so it was restarted at {datetime.fromtimestamp(now):%I:%M %p}.".replace("at 0", "at "))

    def run(self):
        log.info("Watching %s. Close this window to stop watching (they keep running).",
                 ", ".join(p.name for p in self.programs))
        self.start_all()
        while True:
            self.system.sleep(self.settings.check_s)
            try:
                self.tick()
            except Exception:
                log.exception("Watchdog check failed")


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.watchdog")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
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

    try:
        Watchdog(config, notify=notify).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
