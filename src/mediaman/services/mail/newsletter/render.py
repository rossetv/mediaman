"""Jinja2 environment and subject-line rendering for the newsletter."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ._types import DeletedNewsletterItem, NewsletterRecItem, ScheduledNewsletterItem

if TYPE_CHECKING:
    from jinja2 import Template

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


# Gmail cuts a mail at 102 KB, and the unsubscribe link is at its end.
_MAX_BYTES = 102_000
# What Mailgun adds to an address when its domain tracks clicks.
_TRACKING_BYTES = 300

_MAX_TITLE = 200


def _shown_title(title: str) -> str:
    return title if len(title) <= _MAX_TITLE else title[: _MAX_TITLE - 1].rstrip() + "…"


def _fits(html: str) -> bool:
    return len(html.encode()) + html.count("<a ") * _TRACKING_BYTES <= _MAX_BYTES


def render_to_fit(
    template: Template,
    *,
    scheduled_items: list[ScheduledNewsletterItem],
    deleted_items: list[DeletedNewsletterItem],
    this_week_items: list[NewsletterRecItem],
    **context: object,
) -> tuple[str, list[ScheduledNewsletterItem]]:
    """Render the mail small enough that no client cuts it short. Returns the HTML and the
    leaving titles it names.

    A long list of leaving titles shows the soonest as they are and the rest as short rows,
    then gives up picks and deleted titles, so that each keeps its Keep link. Only when even
    short rows do not fit are the latest left out, with a link to mediaman. A title too long to
    read is cut, so that it takes no other's place.
    """
    scheduled_items = [{**i, "title": _shown_title(i["title"])} for i in scheduled_items]
    deleted_items = [{**i, "title": _shown_title(i["title"])} for i in deleted_items]
    this_week_items = [{**i, "title": _shown_title(i["title"])} for i in this_week_items]
    count = len(scheduled_items)
    soonest = sorted(scheduled_items, key=lambda item: item["days_left"])

    def render(pictured: int, listed: int, picks: int, gone: int) -> str:
        items = scheduled_items if pictured >= count else soonest
        by_category: dict[str, int] = {}
        shown_picks = []
        for pick in this_week_items:
            by_category[pick["category"]] = by_category.get(pick["category"], 0) + 1
            if by_category[pick["category"]] <= picks:
                shown_picks.append(pick)
        return template.render(
            scheduled_items=items[:pictured],
            short_items=items[pictured:listed],
            unlisted_leaving=count - listed,
            deleted_items=deleted_items[:gone],
            unlisted_gone=len(deleted_items[gone:]),
            unlisted_picks=len(this_week_items) - len(shown_picks),
            this_week_items=shown_picks,
            **context,
        )

    every = max(len(this_week_items), len(deleted_items))
    for pictured, others in ((count, every), (12, every), (6, every), (6, 3), (6, 0)):
        html = render(min(pictured, count), count, others, others)
        if _fits(html):
            return html, scheduled_items
    low, high = 0, count
    while low < high:
        middle = (low + high + 1) // 2
        if _fits(render(0, middle, 0, 0)):
            low = middle
        else:
            high = middle - 1
    return render(0, low, 0, 0), soonest[:low]


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
