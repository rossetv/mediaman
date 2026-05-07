"""Jinja2 environment and subject-line rendering for the newsletter."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy Jinja2 environment — built once on the first call to send_newsletter,
# not at module import time.  This avoids touching the filesystem during
# import (beneficial for tests and CLI subcommands that never send mail) while
# still sharing the compiled environment across all subsequent calls.
# ---------------------------------------------------------------------------
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "web" / "templates"

try:
    from jinja2 import Environment, FileSystemLoader
    from jinja2 import TemplateError as _TemplateError
except ImportError:  # pragma: no cover
    Environment = None  # type: ignore[assignment,misc]
    FileSystemLoader = None  # type: ignore[assignment,misc]
    _TemplateError = Exception  # type: ignore[assignment,misc]

_JINJA_ENV: Environment | None = None


def _get_jinja_env() -> Environment | None:
    """Return the shared Jinja2 environment, building it on first call.

    Returns ``None`` when Jinja2 is unavailable or the template directory
    cannot be found, so the caller can fall back gracefully.
    """
    global _JINJA_ENV
    if _JINJA_ENV is not None:
        return _JINJA_ENV
    if Environment is None:  # pragma: no cover
        return None
    try:
        _JINJA_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    except (FileNotFoundError, _TemplateError) as exc:  # pragma: no cover
        logger.warning("Jinja2 environment could not be initialised: %s", exc)
        return None
    return _JINJA_ENV


def _build_subject(scheduled_items: list[dict], dry_run: bool) -> str:
    """Format the newsletter email subject line."""
    total_size_bytes = sum(i["file_size_bytes"] for i in scheduled_items)
    if total_size_bytes >= 1 << 40:
        size_str = f"{total_size_bytes / (1 << 40):.1f} TB"
    elif total_size_bytes >= 1 << 30:
        size_str = f"{total_size_bytes / (1 << 30):.1f} GB"
    elif total_size_bytes >= 1 << 20:
        size_str = f"{total_size_bytes / (1 << 20):.0f} MB"
    else:
        size_str = f"{total_size_bytes} B"
    subject = (
        f"Mediaman Weekly Report — {len(scheduled_items)} item"
        f"{'s' if len(scheduled_items) != 1 else ''} scheduled"
        f" · {size_str} to reclaim"
    )
    return f"[DRY RUN] {subject}" if dry_run else subject
