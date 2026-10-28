"""Short summaries and tags for incidents and long calls, written by a local Ollama model.

Ollama (https://ollama.com) runs a language model on this PC; nothing is sent anywhere else. Each
incident is summarized again a little while after a call joins it, so a burst of calls is read
together. Summaries are kept in their own table and never change how calls are grouped.
"""

import hashlib
import json
import logging
import threading
import time
from datetime import datetime

import requests

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

# Tags the model picks from, so the same thing is always tagged the same way.
TAGS = (
    "injuries", "entrapment", "fatality", "transport", "refusal", "cardiac", "breathing", "unconscious",
    "fall", "overdose", "psych", "fire showing", "smoke", "working fire", "alarm", "gas leak", "hazmat",
    "wires down", "road blocked", "traffic", "water rescue", "rescue", "mutual aid", "police needed",
    "weapon", "domestic", "pursuit", "DUI", "theft", "disturbance", "welfare check", "canceled",
    "under control", "cleared",
)
STATUSES = ("active", "under control", "cleared", "canceled", "unknown")

INCIDENT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string", "enum": list(TAGS)}},
        "status": {"type": "string", "enum": list(STATUSES)},
    },
    "required": ["summary", "tags", "status"],
}
CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string", "enum": list(TAGS)}},
    },
    "required": ["summary", "tags"],
}

SYSTEM = (
    "You summarize public safety radio traffic (fire, EMS, police) from Northampton and Lehigh County, "
    "Pennsylvania. The transcripts come from speech recognition and contain mistakes, unit numbers and "
    "10-codes. Write plain English for someone who did not hear the calls. Use only what the calls say: "
    "never invent addresses, units, injuries or outcomes. If something is unclear, leave it out. "
    "Answer in JSON."
)

INCIDENT_PROMPT = (
    "These radio calls are about one incident{place}{what}.\n{pages}\n{calls}\n\n"
    "Write \"summary\": one or two short sentences, at most {words} words, saying what happened, where, "
    "which units responded and the latest status. Pick \"tags\" that the calls clearly support (none is "
    "fine). Set \"status\" from the latest calls: active, under control, cleared, canceled, or unknown."
)

CALL_PROMPT = (
    "One radio call on {talkgroup} at {clock}{who}:\n{transcript}\n\n"
    "Write \"summary\": one short sentence, at most 20 words, saying what this call is about. "
    "Pick \"tags\" that the call clearly supports (none is fine)."
)

# How long to wait before trying again after Ollama could not be reached or had no model yet.
RETRY_S = 60


class NotReady(Exception):
    """Ollama can't summarize yet (not running, model still downloading); worth trying again later."""


def basis_of(*parts) -> str:
    """A short fingerprint of what a summary was made from; a new call or new wording changes it."""
    return hashlib.sha1("\x1f".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def incident_basis(calls: list[dict]) -> str:
    return basis_of(*(f"{c['id']}:{c['transcript'] or ''}" for c in calls))


def call_basis(call: dict) -> str:
    return basis_of(call["transcript"])


def word_count(text: str | None) -> int:
    return len((text or "").split())


def clean_tags(tags) -> list[str]:
    seen = []
    for tag in tags if isinstance(tags, list) else []:
        if tag in TAGS and tag not in seen:
            seen.append(tag)
    return seen


def clip_words(text: str, words: int) -> str:
    """Models sometimes run long; keep the summary to roughly the asked length."""
    parts = " ".join(str(text or "").split()).split(" ")
    if len(parts) <= words + 10:
        return " ".join(parts)
    return " ".join(parts[:words + 10]).rstrip(",;:") + "…"


class OllamaClient:
    def __init__(self, url: str, model: str, timeout_s: float, keep_alive: str, session=None):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        # Ollama takes a number of seconds (-1 = keep loaded) or a duration such as "30m".
        try:
            self.keep_alive = int(keep_alive)
        except ValueError:
            self.keep_alive = keep_alive
        self.session = session or requests.Session()

    def ask(self, prompt: str, schema: dict, system: str = SYSTEM) -> dict:
        response = self.session.post(f"{self.url}/api/chat", timeout=self.timeout_s, json={
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.2, "num_ctx": 4096},
        })
        if response.status_code == 404 and "not found" in response.text:
            raise NotReady(f"Ollama has no model {self.model} yet (still downloading? start-ollama.bat gets it)")
        response.raise_for_status()
        answer = json.loads(response.json()["message"]["content"])
        if not isinstance(answer, dict):
            raise ValueError("Ollama did not answer with a JSON object")
        return answer


class Summarizer:
    def __init__(self, config: Config, db: Database, session=None):
        self.config = config
        self.settings = config.summaries
        self.db = db
        self.client = OllamaClient(self.settings.url, self.settings.model, self.settings.timeout_s,
                                   self.settings.keep_alive, session)
        # (kind, id) -> when it is due (time.monotonic()).
        self._pending: dict[tuple[str, int], float] = {}
        self._busy = 0
        self._cond = threading.Condition()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="summaries", daemon=True)
        # Whether Ollama answered last time; only a change is logged, so a stopped Ollama doesn't fill the log.
        self.reachable = None
        self.last_error = None
        self._not_ready = False

    # ---- what other parts of the service call

    def start(self):
        since = int(time.time()) - self.settings.backlog_hours * 3600
        for incident_id in self.db.incidents_needing_summary(since):
            self._schedule("incident", incident_id, 0)
        if self.settings.call_min_words > 0:
            for call_id in self.db.calls_needing_summary(since, self.settings.call_min_words):
                self._schedule("call", call_id, 0)
        self._thread.start()

    def stop(self, timeout: float = 10):
        with self._cond:
            self._stopping = True
            self._cond.notify()
        self._thread.join(timeout)

    def incident_changed(self, incident_id: int):
        """A call joined, left or moved within an incident."""
        self._schedule("incident", incident_id, self.settings.delay_s)

    def call_done(self, call_id: int):
        """A call was transcribed."""
        if self.settings.call_min_words > 0:
            self._schedule("call", call_id, 0)

    def queue_size(self) -> int:
        with self._cond:
            return len(self._pending)

    def wait_idle(self, timeout: float = 30) -> bool:
        """Test helper: waits until nothing is waiting or being summarized."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cond:
                if not self._pending and not self._busy:
                    return True
            time.sleep(0.05)
        return False

    # ---- the worker thread

    def _schedule(self, kind: str, ref_id: int, delay_s: float):
        with self._cond:
            # Already waiting: keep the earlier time, so a busy incident still gets summarized. A call
            # that arrives meanwhile is picked up then, or schedules another pass afterwards.
            self._pending.setdefault((kind, ref_id), time.monotonic() + delay_s)
            self._cond.notify()

    def _next(self) -> tuple[str, int] | None:
        with self._cond:
            while not self._stopping:
                now = time.monotonic()
                due = [key for key, at in self._pending.items() if at <= now]
                if due:
                    # Incidents before single calls, oldest first.
                    key = min(due, key=lambda k: (k[0] != "incident", self._pending[k]))
                    del self._pending[key]
                    self._busy += 1
                    return key
                self._cond.wait(min(self._pending.values()) - now if self._pending else None)
            return None

    def _run(self):
        while True:
            key = self._next()
            if key is None:
                return
            kind, ref_id = key
            try:
                if kind == "incident":
                    self.summarize_incident(ref_id)
                else:
                    self.summarize_call(ref_id)
                self._set_reachable(True)
            except requests.ConnectionError:
                self._set_reachable(False, f"Ollama is not reachable at {self.settings.url}; is start-ollama.bat running?")
                self._schedule(kind, ref_id, RETRY_S)
            except requests.Timeout:
                self._set_reachable(False, f"Ollama took longer than {self.settings.timeout_s:.0f} s; is it on the GPU?")
                self._schedule(kind, ref_id, RETRY_S)
            except NotReady as e:
                self._set_reachable(False, str(e))
                self._schedule(kind, ref_id, RETRY_S)
            except Exception as e:
                self.last_error = str(e)[:300]
                log.warning("Summaries: %s %d failed: %s", kind, ref_id, e)
            finally:
                with self._cond:
                    self._busy -= 1

    def _set_reachable(self, ok: bool, problem: str | None = None):
        if ok:
            if self.reachable is not True:
                log.info("Summaries: using Ollama at %s with %s", self.settings.url, self.settings.model)
            if self._not_ready:
                self.last_error = None
            self._not_ready = False
        else:
            # Each new problem is logged once, not once per waiting incident.
            if problem != self.last_error:
                log.warning("Summaries: %s Trying again every minute.", problem)
            self.last_error = problem
            self._not_ready = True
        self.reachable = ok

    # ---- writing summaries

    def summarize_incident(self, incident_id: int):
        incident = self.db.get_incident(incident_id)
        if incident is None:
            # Merged away or the map was rebuilt.
            self.db.delete_summary("incident", incident_id)
            return
        calls = self.db.incident_calls_for_summary(incident_id)
        calls = [c for c in calls if c["transcript"]]
        if not calls:
            return
        basis = incident_basis(calls)
        stored = self.db.get_summary("incident", incident_id)
        if stored and stored["basis"] == basis:
            return
        # A long incident: the first calls (the dispatch) and the latest ones matter most.
        limit = self.settings.max_calls
        if len(calls) > limit:
            keep = calls[:3] + [{"call_time": None}] + calls[-(limit - 3):]
        else:
            keep = calls
        pages = self.db.pages(incident["first_time"] - 600, incident["first_time"] + 60, limit=5)
        prompt = INCIDENT_PROMPT.format(
            place=f" at {incident['label']}" if incident["label"] else "",
            what=f" (sorted as: {incident['nature']})" if incident["nature"] else "",
            pages="\n".join(f"Tone-out {_clock(p['page_time'])}: {p['label']}" for p in pages if p["label"]),
            calls="\n".join(_call_line(c) for c in keep),
            words=self.settings.incident_words,
        )
        started = time.perf_counter()
        answer = self.client.ask(prompt, INCIDENT_SCHEMA)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        status = answer.get("status") if answer.get("status") in STATUSES else "unknown"
        self.db.store_summary({
            "kind": "incident", "ref_id": incident_id, "basis": basis,
            "summary": clip_words(answer.get("summary", ""), self.settings.incident_words),
            "tags": ",".join(clean_tags(answer.get("tags"))), "status": status,
            "model": self.settings.model, "made_at": int(time.time()), "ms": elapsed_ms,
        })
        log.info("Summaries: incident %d in %d ms: %s", incident_id, elapsed_ms, answer.get("summary"))

    def summarize_call(self, call_id: int):
        call = self.db.get_call(call_id)
        if call is None or call["status"] != "done" or word_count(call["transcript"]) < self.settings.call_min_words:
            return
        basis = call_basis(call)
        stored = self.db.get_summary("call", call_id)
        if stored and stored["basis"] == basis:
            return
        prompt = CALL_PROMPT.format(
            talkgroup=call["talkgroup_label"] or call["talkgroup"] or "unknown talkgroup",
            clock=_clock(call["call_time"]),
            who=f" from {_speaker(call)}" if _speaker(call) else "",
            transcript=call["transcript"],
        )
        started = time.perf_counter()
        answer = self.client.ask(prompt, CALL_SCHEMA)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.db.store_summary({
            "kind": "call", "ref_id": call_id, "basis": basis,
            "summary": clip_words(answer.get("summary", ""), 20),
            "tags": ",".join(clean_tags(answer.get("tags"))), "status": None,
            "model": self.settings.model, "made_at": int(time.time()), "ms": elapsed_ms,
        })
        log.info("Summaries: call %d in %d ms: %s", call_id, elapsed_ms, answer.get("summary"))


def _clock(t) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def _speaker(call: dict) -> str:
    """Who said a call, so the model reads calls as a conversation: "Dispatch", "unit 1781", "a unit"."""
    if call.get("talker_alias"):
        return call["talker_alias"]
    if call.get("speaker_kind") == "dispatch":
        return "Dispatch"
    if call.get("speaker_kind") == "unit":
        name = call.get("speaker") or "Unit"
        return "a unit" if name == "Unit" else name if not name[:1].isdigit() else f"unit {name}"
    return ""


def _call_line(call: dict) -> str:
    if call["call_time"] is None:
        return "(... earlier calls left out ...)"
    who = f" ({_speaker(call)})" if _speaker(call) else ""
    talkgroup = call["talkgroup_label"] or call["talkgroup"] or ""
    return f"{_clock(call['call_time'])} {talkgroup}{who}: {call['transcript']}"


def attach(summaries: dict, item: dict, basis: str | None = None):
    """Adds summary, summary_tags and summary_status to an incident or call for the API. A summary made
    from older calls is still shown, marked not current, while a newer one is on its way."""
    stored = summaries.get(item["id"])
    if stored is None:
        return
    item["summary"] = stored["summary"]
    item["summary_tags"] = [t for t in (stored["tags"] or "").split(",") if t]
    item["summary_status"] = stored["status"]
    item["summary_current"] = basis is None or stored["basis"] == basis
