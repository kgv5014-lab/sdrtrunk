"""The one-app wrapper: the /app page with tabs, the shared dark look, and the phone home-screen icon.

Every page is sent through dress(), which adds the dark look (app-theme.css), the bit that lets a page
work inside the app's tabs (app-frame.js), and the icon/name the iPhone uses for "Add to Home Screen".
All addresses are relative, so the same files work at / on the PC and Tailscale, and under the
friends' /g/<path>/ link.
"""
from __future__ import annotations

from pathlib import Path

WEB_DIR = Path(__file__).parent / "web"

# Files the pages load, with their types. Nothing secret: the friends' link serves these before sign-in.
ASSETS = {
    "app-theme.css": "text/css",
    "app-frame.js": "text/javascript",
    "manifest.webmanifest": "application/manifest+json",
    "app-icon-180.png": "image/png",
    "app-icon-512.png": "image/png",
}

HEAD = """<meta name="theme-color" content="#0d1117">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="SDR Plus">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="app-icon-180.png">
<link rel="icon" type="image/png" href="app-icon-180.png">
<link rel="stylesheet" href="app-theme.css">
<script src="app-frame.js"></script>
"""


def dress(text: str) -> str:
    """Adds the shared look last in <head>, so it wins over each page's own colors."""
    return text.replace("</head>", HEAD + "</head>", 1)


def page(name: str) -> str:
    return dress((WEB_DIR / name).read_text(encoding="utf-8"))


def asset(name: str) -> tuple[bytes, str] | None:
    media = ASSETS.get(name)
    if media is None:
        return None
    return (WEB_DIR / name).read_bytes(), media
