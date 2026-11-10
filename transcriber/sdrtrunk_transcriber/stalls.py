"""Writes down what the transcriber is doing when one call takes far too long.

The worker tells the watch which call it is on and which step of it (reading the audio, Whisper, saving,
the steps after). When a call has been going for a minute, the watch logs the step, the PC's free
memory, and where every thread of the program is at that moment, so transcriber.log shows what held it
up (a stuck web lookup, the call log being used by another part, Ollama, a full PC). A call that was
slow but finished gets one line with how long each step took.
"""

import ctypes
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger(__name__)

# A call normally takes one to three seconds.
STUCK_AFTER_S = 60.0
# While it stays stuck, written again this often.
STUCK_AGAIN_S = 300.0
# A finished call that took this long gets its steps logged.
SLOW_S = 15.0
POLL_S = 5.0
# Lines of each thread's stack to keep (the innermost ones say where it waits).
STACK_LINES = 14


def free_memory() -> str:
    """"1.2 of 16.0 GB memory free" on Windows, "" elsewhere."""
    if sys.platform != "win32":
        return ""

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("free_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("free_virtual", ctypes.c_ulonglong),
                    ("free_extended", ctypes.c_ulonglong)]

    try:
        status = Status()
        status.length = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return ""
        gb = 1024 ** 3
        return (f"{status.free / gb:.1f} of {status.total / gb:.1f} GB memory free, "
                f"{status.free_page / gb:.1f} GB of page file free")
    except Exception:
        return ""


def thread_report() -> str:
    """Where every thread of this program is right now."""
    frames = sys._current_frames()
    parts = []
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        if frame is None or thread is threading.current_thread():
            continue
        stack = traceback.format_stack(frame)[-STACK_LINES:]
        parts.append(f"--- {thread.name} ---\n" + "".join(stack).rstrip())
    return "\n".join(parts)


class StallWatch:
    def __init__(self, stuck_after_s: float = STUCK_AFTER_S, stuck_again_s: float = STUCK_AGAIN_S,
                 slow_s: float = SLOW_S, poll_s: float = POLL_S, clock=time.monotonic):
        self.stuck_after_s = stuck_after_s
        self.stuck_again_s = stuck_again_s
        self.slow_s = slow_s
        self.poll_s = poll_s
        self.clock = clock
        self._lock = threading.Lock()
        self._call_id = None
        self._started = 0.0
        self._step = ""
        self._step_started = 0.0
        self._steps: list[tuple[str, float]] = []
        self._next_report = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stall-watch", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    # Called by the worker -------------------------------------------------------------------------

    def begin(self, call_id):
        now = self.clock()
        with self._lock:
            self._call_id, self._started = call_id, now
            self._step, self._step_started, self._steps = "starting", now, []
            self._next_report = now + self.stuck_after_s

    def step(self, name: str):
        now = self.clock()
        with self._lock:
            if self._call_id is None:
                return
            self._steps.append((self._step, now - self._step_started))
            self._step, self._step_started = name, now

    def end(self):
        now = self.clock()
        with self._lock:
            call_id, total = self._call_id, now - self._started
            steps = self._steps + [(self._step, now - self._step_started)]
            self._call_id = None
        if call_id is not None and total >= self.slow_s:
            took = ", ".join(f"{name} {seconds:.1f} s" for name, seconds in steps if seconds >= 0.5)
            log.warning("Call %s took %.0f s in all: %s", call_id, total, took or "no one step stood out")

    # The watching thread --------------------------------------------------------------------------

    def check(self) -> bool:
        """Logs the stuck call, if there is one and it's due. True when something was logged."""
        now = self.clock()
        with self._lock:
            if self._call_id is None or now < self._next_report:
                return False
            call_id, total = self._call_id, now - self._started
            step, in_step = self._step, now - self._step_started
            self._next_report = now + self.stuck_again_s
        memory = free_memory()
        log.warning("STUCK: call %s has taken %.0f s so far, %.0f s of it in the step \"%s\".%s "
                    "What every part of the program is doing right now:\n%s",
                    call_id, total, in_step, step, f" {memory}." if memory else "", thread_report())
        return True

    def _run(self):
        while not self._stop.wait(self.poll_s):
            try:
                self.check()
            except Exception:
                log.exception("Stall watch failed")
