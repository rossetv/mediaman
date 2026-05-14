"""Weekly newsletter for subscribers — public entry point.

Builds the "what's about to be deleted, what was reclaimed, what the AI
recommends" digest and sends it via Mailgun. Called automatically at the
end of every scan and on-demand from the admin UI.

This package is a thin aggregator. The heavy lifting is split into focused
submodules:

* :mod:`.render`     — Jinja2 env, subject-line formatting.
* :mod:`.schedule`   — scheduled-deletion cards + ``notified`` flag bookkeeping.
* :mod:`.summary`    — disk-usage stats, deleted-items cards, recommendation batch load.
* :mod:`.enrich`     — per-card Arr download-state annotation.
* :mod:`.recipients` — subscriber resolution, per-recipient render/send loop.

Callers keep importing ``send_newsletter`` and ``NewsletterConfigError``
from :mod:`mediaman.services.newsletter`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from mediaman.core.format import format_day_month as _format_day_month
from mediaman.core.time import now_utc

from .enrich import _annotate_rec_download_states
from .recipients import _load_recipients, _send_to_recipients
from .render import _TEMPLATE_DIR, _build_subject, _get_jinja_env
from .schedule import _load_scheduled_items, _mark_notified
from .summary import _load_deleted_items, _load_recommendations, _load_storage_stats

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MailgunSettings:
    """Validated Mailgun settings loaded from the database.

    Returned by :func:`_load_mailgun_settings` instead of a plain 4-tuple
    so callers can access fields by name and static analysis can check them.
    """

    domain: str
    api_key: str
    from_address: str
    base_url: str


class NewsletterConfigError(Exception):
    """Raised when the newsletter cannot be sent due to missing configuration.

    Distinct from a transient send failure — callers should not retry
    automatically; the administrator must fix the settings first.
    """


def _load_mailgun_settings(conn: sqlite3.Connection, secret_key: str) -> MailgunSettings:
    """Read and validate the four required Mailgun settings.

    Returns a :class:`MailgunSettings` dataclass.  All fields are empty
    strings when *all four* settings are missing (caller should return
    early quietly).

    Raises :exc:`NewsletterConfigError` when some (but not all) required
    settings are missing.
    """
    from mediaman.services.infra import get_string_setting

    domain = get_string_setting(conn, "mailgun_domain", secret_key=secret_key)
    api_key = get_string_setting(conn, "mailgun_api_key", secret_key=secret_key)
    from_address = get_string_setting(conn, "mailgun_from_address", secret_key=secret_key)
    base_url = get_string_setting(conn, "base_url", secret_key=secret_key).rstrip("/")

    # H70: reject non-HTTP(S) base_url schemes so email links can never use
    # javascript:, data:, or other dangerous schemes.
    if base_url and not base_url.lower().startswith(("http://", "https://")):
        logger.error(
            "Newsletter aborted — base_url has a non-HTTP scheme: %r. "
            "Only http:// and https:// are permitted.",
            base_url,
        )
        raise NewsletterConfigError(
            f"Newsletter cannot be sent: base_url must use http:// or https://, got: {base_url!r}"
        )

    missing = [
        k
        for k, v in (
            ("mailgun_domain", domain),
            ("mailgun_api_key", api_key),
            ("mailgun_from_address", from_address),
            ("base_url", base_url),
        )
        if not v
    ]

    if missing:
        if set(missing) == {
            "mailgun_domain",
            "mailgun_api_key",
            "mailgun_from_address",
            "base_url",
        }:
            logger.debug("Newsletter skipped — Mailgun not configured")
            return MailgunSettings(domain="", api_key="", from_address="", base_url="")
        logger.error(
            "Newsletter aborted — required setting(s) missing: %s. "
            "Configure all of mailgun_domain, mailgun_api_key, "
            "mailgun_from_address, and base_url before sending.",
            ", ".join(missing),
        )
        raise NewsletterConfigError(
            f"Newsletter cannot be sent: missing required setting(s): {', '.join(missing)}"
        )

    return MailgunSettings(
        domain=domain, api_key=api_key, from_address=from_address, base_url=base_url
    )


def send_newsletter(
    conn: sqlite3.Connection,
    secret_key: str,
    dry_run: bool = False,
    grace_days: int = 14,
    *,
    recipients: list[str] | None = None,
    mark_notified: bool = True,
) -> None:
    """Send the newsletter to subscribers or specific recipients.

    Queries scheduled_actions, builds storage stats and recent deletions,
    renders the Jinja2 email template, and sends via Mailgun.

    When called by the scan engine (default), sends to all active subscribers,
    includes only unnotified items, and marks them as notified=1 afterwards.

    When called manually (``recipients`` provided, ``mark_notified=False``),
    sends to the given addresses, includes *all* scheduled items regardless
    of notification status, and does not update the notified flag.

    Args:
        conn: Open SQLite connection.
        secret_key: Application secret for decrypting Mailgun API key.
        dry_run: Passed through to the template so recipients can see the banner.
        grace_days: Included in the email body so recipients know the deadline.
        recipients: If provided, send to these addresses instead of the
            subscribers table.
        mark_notified: If True (default), mark scheduled items as notified=1
            after sending.
    """
    from mediaman.services.mail.mailgun import MailgunClient

    mg_settings = _load_mailgun_settings(conn, secret_key)
    if not mg_settings.domain:
        # All four missing — already logged at DEBUG; skip silently.
        return
    domain = mg_settings.domain
    api_key = mg_settings.api_key
    from_address = mg_settings.from_address
    base_url = mg_settings.base_url

    recipient_emails = _load_recipients(conn, recipients)
    if recipient_emails is None:
        return

    # Cheap settings/COUNT checks first — invert the previous order so a
    # tick with no scheduled items and no suggestions does not pay for
    # the per-item ``_load_scheduled_items`` join (which fans out to
    # ``media_items`` and runs the same join every minute on busy
    # servers).  We only fetch the heavier scheduled-item payload when
    # there is at least one notification candidate to render.
    rec_enabled_check = conn.execute(
        "SELECT value FROM settings WHERE key='suggestions_enabled'"
    ).fetchone()
    if not rec_enabled_check or rec_enabled_check["value"] != "false":
        rec_check = conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()
        has_recommendations = bool(rec_check and rec_check["n"] > 0)
    else:
        has_recommendations = False

    scheduled_items = _load_scheduled_items(conn, secret_key, base_url, now_utc(), mark_notified)
    if not scheduled_items and not has_recommendations:
        logger.debug("Newsletter skipped — nothing to report")
        return

    now = now_utc()
    deleted_items = _load_deleted_items(conn, secret_key, base_url, now)
    this_week_items = _load_recommendations(conn)
    storage_summary = _load_storage_stats(conn, now)

    if this_week_items:
        _annotate_rec_download_states(this_week_items, conn, secret_key)

    env = _get_jinja_env()
    if env is None:  # pragma: no cover
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("newsletter.html")

    mailgun = MailgunClient(domain, api_key, from_address)
    subject = _build_subject(scheduled_items, dry_run)
    report_date = _format_day_month(now, long_month=True)

    successfully_sent = _send_to_recipients(
        recipient_emails=recipient_emails,
        scheduled_items=scheduled_items,
        deleted_items=deleted_items,
        this_week_items=this_week_items,
        storage=storage_summary.stats,
        reclaimed_week=storage_summary.reclaimed_week,
        reclaimed_month=storage_summary.reclaimed_month,
        reclaimed_total=storage_summary.reclaimed_total,
        subject=subject,
        base_url=base_url,
        secret_key=secret_key,
        dry_run=dry_run,
        grace_days=grace_days,
        template=template,
        mailgun=mailgun,
        report_date=report_date,
        conn=conn if mark_notified else None,
    )

    if mark_notified and successfully_sent:
        # Only flip ``notified=1`` for items where every active recipient was
        # successfully delivered — a partial failure must leave the row at
        # ``notified=0`` for re-attempt on the next tick.
        _mark_notified(
            conn,
            scheduled_items,
            active_recipients=recipient_emails,
        )

    logger.info(
        "Newsletter sent to %d/%d subscriber(s) — %d scheduled, %d deleted",
        len(successfully_sent),
        len(recipient_emails),
        len(scheduled_items),
        len(deleted_items),
    )


__all__ = ["NewsletterConfigError", "send_newsletter"]
