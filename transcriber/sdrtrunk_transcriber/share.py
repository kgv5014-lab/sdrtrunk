"""Sharing the map with friends: a read-only guest view behind a password, on the public Funnel port.

Only the public listen app (127.0.0.1:3001, which Tailscale Funnel puts on the internet over HTTPS) has
it, and only once a password is set with setup-share-link.bat. Until then every guest address is a 404.

- Everything lives under /g/<long random path>/, so scanners guessing addresses find nothing.
- The password is stored as a scrypt hash in data/share-password.txt, never in plain text.
- After a right password the browser gets a signed cookie (HttpOnly, Secure) good for 30 days.
  Changing the password or turning sharing off signs every guest out.
- Wrong passwords: one address is locked out for 15 minutes after 5 tries (longer each time after),
  and after 30 wrong tries in an hour from anywhere, logins pause for 15 minutes.
- Guests get the map, call log, call audio and live audio, read-only. Only GET requests to the
  addresses in GUEST_API are passed on to the main service, so nothing can be edited, removed or
  changed, and settings, status and the admin pages are never reachable.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
COOKIE = "guest"
SESSION_DAYS = 30
MIN_PASSWORD = 10
# scrypt: about 30 ms and 32 MB per try on a PC, so guessing offline from a stolen file is slow too.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
# Lockouts for wrong passwords.
IP_TRIES = 5
IP_LOCK_S = 15 * 60
ALL_TRIES = 30
ALL_WINDOW_S = 3600
ALL_LOCK_S = 15 * 60
WRONG_DELAY_S = 1.0

# The pages guests can open, and the main service's addresses those pages read (GET only).
PAGES = {"": "index.html", "map": "map.html", "listen": "listen.html"}
GUEST_API = re.compile(r"^/(api/calls|api/calls/\d+/audio|api/pages|api/incidents|api/map/status|api/map/mileposts|api/traffic"
                       r"|api/traffic/cameras/\d+/image|listen/talkgroups|listen/stream\.mp3|listen/now)$")
# Query values guests may not set: removed incidents stay hidden.
DROP_PARAMS = {"hidden"}
MAX_LIMIT = 300

HEADERS = {
    # The secret path must not leak to map tile servers or links through the Referer header.
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Robots-Tag": "noindex, nofollow",
}


# ---- files in the data folder

def _path_file(data_path: Path) -> Path:
    return data_path / "share-path.txt"


def _password_file(data_path: Path) -> Path:
    return data_path / "share-password.txt"


def _secret_file(data_path: Path) -> Path:
    return data_path / "share-secret.txt"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def read_path(data_path: Path) -> str:
    return _read(_path_file(data_path)) or new_path(data_path)


def new_path(data_path: Path) -> str:
    token = secrets.token_urlsafe(24)
    _write(_path_file(data_path), token)
    return token


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode()[:1024], salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, bytes.fromhex(digest))


def set_password(data_path: Path, password: str):
    """Saves a new password and signs every guest out."""
    _write(_password_file(data_path), hash_password(password))
    _write(_secret_file(data_path), secrets.token_hex(32))
    read_path(data_path)


def turn_off(data_path: Path):
    """Removes the password and makes a new path, so the old link and every guest are shut out."""
    for path in (_password_file(data_path), _secret_file(data_path)):
        path.unlink(missing_ok=True)
    new_path(data_path)


def is_on(data_path: Path) -> bool:
    return bool(_read(_password_file(data_path)))


def make_password() -> str:
    """A strong password that is still easy to read out or type on a phone: about 80 bits."""
    letters = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(letters) for _ in range(4)) for _ in range(4))


# ---- the gate in front of the public app

class _Lockout:
    def __init__(self):
        self._lock = threading.Lock()
        self._per_ip: dict[str, list] = {}   # ip -> [wrong tries, locked until, times locked]
        self._all: deque = deque()
        self._all_until = 0.0

    def blocked(self, ip: str) -> float:
        """Seconds left before this address may try again (0 = may try)."""
        now = time.monotonic()
        with self._lock:
            entry = self._per_ip.get(ip)
            waits = [self._all_until - now, (entry[1] - now) if entry else 0]
        return max(0.0, *waits)

    def wrong(self, ip: str):
        now = time.monotonic()
        with self._lock:
            entry = self._per_ip.setdefault(ip, [0, 0.0, 0])
            entry[0] += 1
            if entry[0] >= IP_TRIES:
                entry[2] += 1
                entry[0] = 0
                entry[1] = now + IP_LOCK_S * 2 ** min(entry[2] - 1, 6)
                log.warning("Share link: %s locked out after %d wrong passwords", ip, IP_TRIES)
            self._all.append(now)
            while self._all and self._all[0] < now - ALL_WINDOW_S:
                self._all.popleft()
            if len(self._all) >= ALL_TRIES:
                self._all.clear()
                self._all_until = now + ALL_LOCK_S
                log.warning("Share link: %d wrong passwords in an hour; logins paused for %d minutes",
                            ALL_TRIES, ALL_LOCK_S // 60)
            if len(self._per_ip) > 10000:
                self._per_ip = {k: v for k, v in self._per_ip.items() if v[1] > now}

    def right(self, ip: str):
        with self._lock:
            entry = self._per_ip.get(ip)
            if entry:
                entry[0] = 0


def _client_ip(scope) -> str:
    # The app listens on 127.0.0.1 only, so the request came through Tailscale, which adds the real
    # address last in X-Forwarded-For. Earlier entries could be made up by the visitor, so use the last.
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            return value.decode("latin-1").split(",")[-1].strip() or "?"
    client = scope.get("client")
    return client[0] if client else "?"


def _cookie(scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == COOKIE:
                    return val
    return ""


class ShareGate:
    """ASGI app: /g/<path>/... is the guest view, anything else goes to the listen-only app."""

    def __init__(self, public_app, main_app, data_path: Path):
        self.public_app = public_app
        self.main_app = main_app
        self.data_path = data_path
        self.lockout = _Lockout()

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/g/"):
            await self.public_app(scope, receive, send)
            return
        response = await self._handle(scope, receive, send)
        if response is not None:
            for key, value in HEADERS.items():
                response.headers[key] = value
            await response(scope, receive, send)

    # Session cookies: "<expires>.<signature>", signed with share-secret.txt and the password hash,
    # so a new password or turning sharing off makes every old cookie useless.
    def _signer(self) -> bytes:
        return (_read(_secret_file(self.data_path)) + _read(_password_file(self.data_path))).encode()

    def _new_session(self) -> str:
        expires = str(int(time.time()) + SESSION_DAYS * 86400)
        sig = hmac.new(self._signer(), expires.encode(), hashlib.sha256).digest()
        return expires + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def _session_ok(self, value: str) -> bool:
        expires, _, sig = value.partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        want = base64.urlsafe_b64encode(hmac.new(self._signer(), expires.encode(), hashlib.sha256).digest())
        return hmac.compare_digest(sig.encode(), want.decode().rstrip("=").encode())

    async def _handle(self, scope, receive, send):
        not_found = Response("Not Found", status_code=404, media_type="text/plain")
        if not is_on(self.data_path):
            return not_found
        token = read_path(self.data_path)
        prefix = f"/g/{token}"
        path = scope["path"]
        if not hmac.compare_digest(path[:len(prefix)].encode(), prefix.encode()):
            return not_found
        rest = path[len(prefix):]
        if rest == "":
            return RedirectResponse(prefix + "/map", status_code=302)
        method = scope["method"]

        if rest == "/login":
            if method == "POST":
                return await self._login(scope, receive, prefix)
            return self._login_page(prefix)

        if not self._session_ok(_cookie(scope)):
            if method == "GET" and rest.lstrip("/") in PAGES:
                return RedirectResponse(f"{prefix}/login", status_code=302)
            return Response("Sign in again", status_code=401, media_type="text/plain")
        if method not in ("GET", "HEAD"):
            return Response("Read only", status_code=405, media_type="text/plain")

        page = PAGES.get(rest.lstrip("/"))
        if page is not None:
            return self._page(page)
        if GUEST_API.match(rest):
            await self._pass_on(scope, receive, send, rest)
            return None
        return not_found

    def _page(self, name: str) -> Response:
        text = (WEB_DIR / name).read_text(encoding="utf-8")
        text = text.replace('<html lang="en">', '<html lang="en" data-guest="1">', 1)
        text = text.replace("<head>", '<head>\n<meta name="referrer" content="no-referrer">', 1)
        return HTMLResponse(text, headers={"Cache-Control": "no-store"})

    async def _pass_on(self, scope, receive, send, rest: str):
        params = [(k, v) for k, v in parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
                  if k not in DROP_PARAMS]
        params = [(k, str(min(int(v), MAX_LIMIT)) if k == "limit" and v.isdigit() else v) for k, v in params]
        query = urlencode(params).encode()
        inner = dict(scope, path=rest, raw_path=rest.encode(), query_string=query, root_path="")
        await self.main_app(inner, receive, send)

    def _login_page(self, prefix: str, message: str = "", status: int = 200) -> Response:
        page = LOGIN_PAGE.replace("{action}", html.escape(prefix + "/login")).replace(
            "{message}", f'<p class="err">{html.escape(message)}</p>' if message else "")
        return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store"})

    async def _login(self, scope, receive, prefix: str) -> Response:
        ip = _client_ip(scope)
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > 4096:
                return Response("Too long", status_code=413)
            if not message.get("more_body"):
                break
        wait = self.lockout.blocked(ip)
        if wait:
            return self._login_page(prefix, f"Too many wrong tries. Try again in {int(wait // 60) + 1} minutes.", 429)
        password = dict(parse_qsl(body.decode("utf-8", "replace"))).get("password", "")
        stored = _read(_password_file(self.data_path))
        if not password or not await asyncio.to_thread(check_password, password, stored):
            self.lockout.wrong(ip)
            await asyncio.sleep(WRONG_DELAY_S)
            return self._login_page(prefix, "Wrong password.", 401)
        self.lockout.right(ip)
        log.info("Share link: a guest signed in from %s", ip)
        response = RedirectResponse(f"{prefix}/map", status_code=303)
        response.set_cookie(COOKIE, self._new_session(), max_age=SESSION_DAYS * 86400, path=prefix + "/",
                            secure=True, httponly=True, samesite="lax")
        return response


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>Sign in</title>
<style>
  :root { --bg: #fafafa; --fg: #1d1d1f; --muted: #6b6b70; --line: #e2e2e6; --card: #fff; --accent: #2a6fdb; --err: #c0392b; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #161618; --fg: #ececf0; --muted: #9a9aa2; --line: #2c2c31; --card: #1e1e22; --accent: #6ea2ff; --err: #ff6b5e; }
  }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.45 system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh; }
  form { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 20px; width: min(340px, calc(100vw - 32px)); box-sizing: border-box; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  input, button { font: inherit; width: 100%; box-sizing: border-box; padding: 9px 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); }
  button { margin-top: 10px; background: var(--accent); color: #fff; border: 0; cursor: pointer; }
  .err { color: var(--err); margin: 0 0 10px; }
  .muted { color: var(--muted); font-size: 13px; margin-top: 12px; }
</style>
</head>
<body>
<form method="post" action="{action}">
  <h1>Incident map</h1>
  {message}
  <input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>
  <button type="submit">Sign in</button>
  <div class="muted">View only. You stay signed in on this device for 30 days.</div>
</form>
</body>
</html>
"""


# ---- setup from the command line (setup-share-link.bat, stop-sharing.bat)

def _tailscale_name() -> str:
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10).stdout
        return json.loads(out)["Self"]["DNSName"].rstrip(".")
    except Exception:
        return ""


def link(data_path: Path, public_url: str = "") -> str:
    base = public_url.rstrip("/")
    if not base:
        name = _tailscale_name()
        base = f"https://{name}:8443" if name else "https://<this PC's Tailscale name>:8443"
    return f"{base}/g/{read_path(data_path)}/map"


def setup(data_path: Path, public_url: str = "") -> int:
    import getpass

    print("Pick the password your friends will type (at least %d characters)," % MIN_PASSWORD)
    print("or just press Enter and one will be made for you.")
    while True:
        password = getpass.getpass("Password (hidden as you type): ").strip()
        if not password:
            password = make_password()
            print(f"\nYour password is:  {password}\n")
            break
        if len(password) < MIN_PASSWORD:
            print(f"That's too short; use at least {MIN_PASSWORD} characters.")
            continue
        if getpass.getpass("Type it again: ").strip() != password:
            print("Those didn't match; try again.")
            continue
        break
    set_password(data_path, password)
    print("Saved (only a scrambled copy is kept). Anyone signed in before has to sign in again.\n")
    print("Link for your friends:")
    print("  " + link(data_path, public_url) + "\n")
    return 0


def stop(data_path: Path) -> int:
    turn_off(data_path)
    print("Sharing is off. The old link and password no longer work, and every guest is signed out.")
    print("Run setup-share-link.bat again to share with a new link.")
    return 0
