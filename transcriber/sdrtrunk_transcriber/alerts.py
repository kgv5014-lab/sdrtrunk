"""Phone alerts for big incidents and chosen tone-outs, shown by the ntfy app on a phone.

This service answers the ntfy app itself (server.py has the two addresses the app asks), so the
alert text only ever goes from this PC to the phone, over Tailscale when away from home. An iPhone
can only be woken up through Apple, so for each alert a wake-up ping holding nothing but a random id
goes through ntfy.sh to Apple; the iPhone then fetches the alert itself from this PC. That is the
same thing a self-hosted ntfy server does with its upstream-base-url setting.
"""

import hashlib
import logging
import re
import secrets
import string
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

TOPIC_PATTERN = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
# Incident types the map has called by another name.
SAME_TYPE = {"vehicle crash": "mva"}
# ntfy shows these tag names as emoji in front of the title.
TAGS = {"mva": "car", "structure fire": "fire"}
# The ntfy app shows alerts this old when it is opened.
KEEP_S = 12 * 3600
# The same station toned out again this soon is the same tone-out.
PAGE_REPEAT_S = 120


def _type(name: str | None) -> str:
    name = (name or "").strip().lower()
    return SAME_TYPE.get(name, name)


def wakeup_topic(base_url: str, topic: str) -> str:
    """The name the ntfy iPhone app listens on for wake-ups for a topic on another server."""
    return hashlib.sha256(f"{base_url.rstrip('/')}/{topic}".encode()).hexdigest()


class Alerter:
    def __init__(self, config: Config, db: Database, session=None):
        self.settings = config.alerts
        if not TOPIC_PATTERN.match(self.settings.topic) or self.settings.topic in ("api", "map"):
            raise ValueError(f"[alerts] topic {self.settings.topic!r}: use letters, digits, - and _ only")
        self.db = db
        self.base_url = self.settings.base_url.strip().rstrip("/")
        self.session = session or requests.Session()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="alerts")
        self.incident_types = {_type(t) for t in self.settings.incidents}
        if not self.base_url:
            log.warning("Phone alerts: set [alerts] base_url to this PC's address in the ntfy app, "
                        "or an iPhone only sees alerts when the app is opened")

    def stop(self):
        self._pool.shutdown(wait=True)

    def flush(self):
        """Test helper: waits for wake-up pings already handed off."""
        self._pool.submit(lambda: None).result(timeout=30)

    # ---- what alerts

    def incident_call(self, call_id: int, incident_id: int, call_time: int, transcript: str | None):
        """A call was placed at an incident on the map."""
        if time.time() - call_time > self.settings.max_age_minutes * 60:
            return
        incident = self.db.get_incident(incident_id)
        if incident is None or incident["hidden"] or _type(incident["nature"]) not in self.incident_types:
            return
        if self.db.alerted(incident_id=incident_id, call_id=call_id):
            return
        heard = (transcript or "").strip()
        if len(heard) > 240:
            heard = heard[:239] + "…"
        self.send("incident", incident["nature"], "\n".join(filter(None, (incident["label"], heard))),
                  tags=[TAGS.get(_type(incident["nature"]), "rotating_light")], priority=4,
                  click=f"{self.base_url}/map" if self.base_url else None,
                  incident_id=incident_id, call_id=call_id)

    def page(self, page: dict):
        """A tone-out came in from SDRTrunk."""
        label = page.get("label") or ""
        stations = [s.strip().lower() for s in self.settings.stations if s and s.strip()]
        if not label or not any(s in label.lower() for s in stations):
            return
        page_time = page.get("page_time") or time.time()
        if time.time() - page_time > self.settings.max_age_minutes * 60:
            return
        if self.db.alerted(page_label=label, since=time.time() - PAGE_REPEAT_S):
            return
        tones = f"{page['tone_a']:g}" + (f" / {page['tone_b']:g}" if page.get("tone_b") else "") + " Hz"
        at = datetime.fromtimestamp(page_time).strftime("%H:%M")
        where = page.get("channel") or page.get("system_label")
        self.send("page", f"Tone-out: {label}", ", ".join(filter(None, (f"{at} tones {tones}", where))),
                  tags=["loudspeaker"], priority=4, page_id=page.get("id"), page_label=label)

    # ---- sending

    def send(self, kind: str, title: str, message: str, tags: list[str] | None = None, priority: int = 3,
             click: str | None = None, **refs) -> dict:
        alert = {"id": "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)),
                 "time": int(time.time()), "kind": kind, "title": title, "message": message,
                 "tags": ",".join(tags or []), "priority": priority, "click": click, **refs}
        self.db.add_alert(alert)
        log.info("Phone alert: %s: %s", title, message.replace("\n", " / "))
        if self.base_url and self.settings.wakeup_url:
            self._pool.submit(self._wake, alert["id"])
        return self.to_ntfy(alert)

    def _wake(self, alert_id: str):
        url = f"{self.settings.wakeup_url.rstrip('/')}/{wakeup_topic(self.base_url, self.settings.topic)}"
        try:
            reply = self.session.post(url, data=b"", headers={"X-Poll-ID": alert_id}, timeout=15)
            reply.raise_for_status()
        except Exception as e:
            log.warning("Phone alerts: the iPhone wake-up ping failed (%s); the alert shows when the app "
                        "is opened", e)

    # ---- what the ntfy app reads

    def messages(self, since: str | None = None, alert_id: str | None = None) -> list[dict]:
        oldest = time.time() - KEEP_S
        if alert_id:
            rows = self.db.alerts(oldest, alert_id=alert_id)
        elif since and since.isdigit():
            rows = self.db.alerts(max(oldest, int(since)))
        elif since and since != "all":
            rows = self.db.alerts(oldest, after_id=since)
        else:
            rows = self.db.alerts(oldest)
        return [self.to_ntfy(row) for row in rows]

    def to_ntfy(self, alert: dict) -> dict:
        message = {"id": alert["id"], "time": alert["time"], "event": "message", "topic": self.settings.topic,
                   "title": alert["title"], "message": alert["message"] or "", "priority": alert["priority"] or 3}
        if alert["tags"]:
            message["tags"] = alert["tags"].split(",")
        if alert["click"]:
            message["click"] = alert["click"]
        return message
