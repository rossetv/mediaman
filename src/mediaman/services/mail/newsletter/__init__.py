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
from datetime import datetime, timezone

from mediaman.services.infra.format import format_day_month as _format_day_month

from .enrich import _annotate_rec_download_states
from .recipients import _load_recipients, _send_to_recipients
from .render import _TEMPLATE_DIR, _build_subject, _get_jinja_env
from .schedule import _load_scheduled_items, _mark_notified
from .summary import _load_deleted_items, _load_recommendations, _load_storage_stats

logger = logging.getLogger("mediaman")


class NewsletterConfigError(Exception):
    """Raised when the newsletter cannot be sent due to missing configuration.

    Distinct from a transient send failure — callers should not retry
    automatically; the administrator must fix the settings first.
    """


def _load_mailgun_settings(conn: sqlite3.Connection, secret_key: str) -> tuple[str, str, str, str]:
    """Read and validate the four required Mailgun settings.

    Returns ``(domain, api_key, from_address, base_url)``.

    Raises :exc:`NewsletterConfigError` when some (but not all) required
    settings are missing.  When *all* four are missing, logs at DEBUG and
    returns empty strings (caller should then return early quietly).
    """
    from mediaman.services.infra.settings_reader import get_string_setting

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
            return "", "", "", ""
        logger.error(
            "Newsletter aborted — required setting(s) missing: %s. "
            "Configure all of mailgun_domain, mailgun_api_key, "
            "mailgun_from_address, and base_url before sending.",
            ", ".join(missing),
        )
        raise NewsletterConfigError(
            f"Newsletter cannot be sent: missing required setting(s): {', '.join(missing)}"
        )

    return domain, api_key, from_address, base_url


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

    domain, api_key, from_address, base_url = _load_mailgun_settings(conn, secret_key)
    if not domain:
        # All four missing — already logged at DEBUG; skip silently.
        return

    recipient_emails = _load_recipients(conn, recipients)
    if recipient_emails is None:
        return

    # Check if there is content worth sending.
    scheduled_items = _load_scheduled_items(
        conn, secret_key, base_url, datetime.now(timezone.utc), mark_notified
    )
    rec_check = conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()
    rec_enabled_check = conn.execute(
        "SELECT value FROM settings WHERE key='suggestions_enabled'"
    ).fetchone()
    has_recommendations = (
        (not rec_enabled_check or rec_enabled_check["value"] != "false")
        and rec_check
        and rec_check["n"] > 0
    )
    if not scheduled_items and not has_recommendations:
        logger.debug("Newsletter skipped — nothing to report")
        return

    now = datetime.now(timezone.utc)
    deleted_items = _load_deleted_items(conn, secret_key, base_url, now)
    this_week_items = _load_recommendations(conn)
    storage, reclaimed_week, reclaimed_month, reclaimed_total = _load_storage_stats(conn, now)

    if this_week_items:
        _annotate_rec_download_states(this_week_items, conn, secret_key)

    env = _get_jinja_env()
    if env is None:  # pragma: no cover
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("email/newsletter.html")

    mailgun = MailgunClient(domain, api_key, from_address)
    subject = _build_subject(scheduled_items, dry_run)
    report_date = _format_day_month(now, long_month=True)

    successfully_sent = _send_to_recipients(
        recipient_emails=recipient_emails,
        scheduled_items=scheduled_items,
        deleted_items=deleted_items,
        this_week_items=this_week_items,
        storage=storage,
        reclaimed_week=reclaimed_week,
        reclaimed_month=reclaimed_month,
        reclaimed_total=reclaimed_total,
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
        # Finding 23: only flip ``notified=1`` for items that every
        # active recipient was successfully delivered to. A partial
        # failure now leaves the row at ``notified=0`` so the next scan
        # tick re-attempts delivery for the recipients that missed out.
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


__all__ = ["send_newsletter", "NewsletterConfigError"]
