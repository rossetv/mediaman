"""Jinja2 environment and subject-line rendering for the newsletter."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ._types import ScheduledNewsletterItem

logger = logging.getLogger(__name__)

# Lazy Jinja2 environment — built once on the first call to send_newsletter,
# not at module import time.  This avoids touching the filesystem during
# import (beneficial for tests and CLI subcommands that never send mail) while
# still sharing the compiled environment across all subsequent calls.
_TEMPLATE_DIR = Path(__file__).parent / "templates"

try:
    from jinja2 import Environment, FileSystemLoader
    from jinja2 import TemplateError as _TemplateError
except ImportError:  # pragma: no cover
    Environment = None  # type: ignore[assignment,misc]
    FileSystemLoader = None  # type: ignore[assignment,misc]
    _TemplateError = Exception  # type: ignore[assignment,misc]

# Shared environment rebuilt only once per process lifetime.
# The lock prevents a data race when the scheduler and FastAPI thread-pool
# both call send_newsletter concurrently and each see _JINJA_ENV is None.
_JINJA_ENV: Environment | None = None
_JINJA_ENV_LOCK = threading.Lock()


def _get_jinja_env() -> Environment | None:
    """Return the shared Jinja2 environment, built on first call, or None when Jinja2 or
    the template directory is unavailable so callers can degrade.
    """
    global _JINJA_ENV
    # Fast path — no lock needed for the read once the env is built.
    if _JINJA_ENV is not None:
        return _JINJA_ENV
    with _JINJA_ENV_LOCK:
        # Re-check inside the lock to avoid double-init if two threads
        # raced past the fast-path check above.
        if _JINJA_ENV is not None:
            return _JINJA_ENV
        if Environment is None:  # pragma: no cover
            return None
        try:
            _JINJA_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
        except (FileNotFoundError, _TemplateError):  # pragma: no cover
            logger.exception("Jinja2 environment could not be initialised")
            return None
    return _JINJA_ENV


def _build_subject(scheduled_items: list[ScheduledNewsletterItem], dry_run: bool) -> str:
    if not scheduled_items:
        return "[DRY RUN] Mediaman Weekly Report" if dry_run else "Mediaman Weekly Report"
    total_size_bytes = sum(i["file_size_bytes"] for i in scheduled_items)
    # Each unit hands over to the next once its rounded value would read 1024.
    if total_size_bytes <= 0:
        size_str = ""
    elif round(total_size_bytes / 1024) < 1024:
        size_str = f"{max(1, round(total_size_bytes / 1024))} KB"
    elif round(total_size_bytes / (1 << 20)) < 1024:
        size_str = f"{total_size_bytes / (1 << 20):.0f} MB"
    elif float(f"{total_size_bytes / (1 << 30):.1f}") < 1024:
        size_str = f"{total_size_bytes / (1 << 30):.1f} GB"
    else:
        size_str = f"{total_size_bytes / (1 << 40):.1f} TB"
    subject = (
        f"Mediaman Weekly Report — {len(scheduled_items)} item"
        f"{'s' if len(scheduled_items) != 1 else ''} scheduled"
    )
    if size_str:
        subject += f" · {size_str} to reclaim"
    return f"[DRY RUN] {subject}" if dry_run else subject
