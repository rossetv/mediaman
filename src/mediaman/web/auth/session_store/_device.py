"""The short name of the browser and system a session was signed in with."""

from __future__ import annotations

# Browsers built on another carry its name too, so the more particular come first.
_BROWSERS = (
    ("Edge", ("Edg/", "EdgA/", "EdgiOS/")),
    ("Opera", ("OPR/", "OPT/", "OPiOS/")),
    ("Samsung Internet", ("SamsungBrowser/",)),
    ("Firefox", ("Firefox/", "FxiOS/")),
    ("Chrome", ("Chrome/", "CriOS/", "Chromium/")),
    ("Safari", ("Safari/",)),
)
# iPhones say "like Mac OS X" and Android says "Linux", so they come before those.
_SYSTEMS = (
    ("iPhone", ("iPhone",)),
    ("iPad", ("iPad",)),
    ("Android", ("Android",)),
    ("ChromeOS", ("CrOS",)),
    ("Windows", ("Windows",)),
    ("macOS", ("Macintosh", "Mac OS X")),
    ("Linux", ("Linux", "X11")),
)


def device_label(user_agent: str) -> str | None:
    """Return e.g. ``Safari on iPhone``, made only of the names above, or None without a UA."""
    if not user_agent.strip():
        return None
    browser = _first(_BROWSERS, user_agent) or "Unknown browser"
    system = _first(_SYSTEMS, user_agent)
    return f"{browser} on {system}" if system else browser


def _first(names: tuple[tuple[str, tuple[str, ...]], ...], user_agent: str) -> str | None:
    return next((name for name, marks in names if any(m in user_agent for m in marks)), None)
