"""Weekly newsletter for subscribers — public entry point.

Builds the "what's about to be deleted, what was reclaimed, what the AI
recommends" digest and sends it via Mailgun. Called automatically at the
end of every scan and on-demand from the admin UI.

This package is a thin aggregator. The heavy lifting is split into focused
submodules:

* :mod:`.render`      — Jinja2 env, subject-line formatting.
* :mod:`.schedule`    — scheduled-deletion cards + ``notified`` flag bookkeeping.
* :mod:`.summary`     — disk-usage stats, deleted-items cards, recommendation batch load.
* :mod:`.enrich`      — per-card Arr download-state annotation.
* :mod:`.subscribers` — subscriber resolution, per-subscriber render/send loop.

Callers keep importing ``send_newsletter`` and ``NewsletterConfigError``
from :mod:`mediaman.services.newsletter`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from mediaman.core.format import format_day_month as _format_day_month
from mediaman.core.time import now_utc

from ._types import DeletedNewsletterItem, NewsletterRecItem, ScheduledNewsletterItem
from .enrich import _annotate_rec_download_states
from .render import _TEMPLATE_DIR, _build_subject, _get_jinja_env
from .schedule import _load_scheduled_items, _mark_notified
from .subscribers import _load_subscribers, _send_to_subscribers
from .summary import StorageSummary, _load_deleted_items, _load_recommendations, _load_storage_stats

if TYPE_CHECKING:
    from jinja2 import Template as _JinjaTemplate

    from mediaman.services.mail.mailgun import MailgunClient as _MailgunClient

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


# F-02: named dataclass replaces the 7-tuple return of _build_send_context (§5.9)
@dataclass(frozen=True, slots=True)
class SendContext:
    """All content and dispatch objects needed to send one newsletter run.

    Returned by :func:`_build_send_context` so callers access fields by name
    rather than positionally unpacking a 7-tuple.
    """

    deleted_items: list[DeletedNewsletterItem]
    this_week_items: list[NewsletterRecItem]
    storage_summary: StorageSummary
    template: _JinjaTemplate
    mailgun: _MailgunClient
    subject: str
    report_date: str


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
        # F-11: dotted event name, variable in extra=
        logger.error(
            "newsletter.aborted",
            extra={"reason": "invalid_base_url_scheme", "base_url": base_url},
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
            # F-11: dotted event name
            logger.debug("newsletter.skipped", extra={"reason": "mailgun_not_configured"})
            return MailgunSettings(domain="", api_key="", from_address="", base_url="")
        # F-11: dotted event name, variable in extra=
        logger.error(
            "newsletter.aborted",
            extra={"reason": "missing_settings", "missing": ", ".join(missing)},
        )
        raise NewsletterConfigError(
            f"Newsletter cannot be sent: missing required setting(s): {', '.join(missing)}"
        )

    return MailgunSettings(
        domain=domain, api_key=api_key, from_address=from_address, base_url=base_url
    )


def _build_send_context(
    conn: sqlite3.Connection,
    *,
    mg_settings: MailgunSettings,
    secret_key: str,
    scheduled_items: list[ScheduledNewsletterItem],
    dry_run: bool,
    now: datetime,
) -> SendContext:
    """Load newsletter content and construct the Mailgun client + Jinja template.

    Returns a :class:`SendContext` dataclass with named fields:
    ``deleted_items``, ``this_week_items``, ``storage_summary``, ``template``,
    ``mailgun``, ``subject``, ``report_date``.

    Factored out of :func:`send_newsletter` to keep the orchestrator body under
    60 lines.

    Args:
        conn: Open SQLite connection.
        mg_settings: Validated Mailgun settings from :func:`_load_mailgun_settings`.
        secret_key: Application secret for signing poster and download tokens.
        scheduled_items: Pre-loaded scheduled-deletion cards (avoids a second query).
        dry_run: Passed through to subject-line formatting.
        now: Timestamp computed once by the caller so all sections share the same
            reference time and cannot straddle midnight.

    Raises:
        jinja2.TemplateNotFound: if ``newsletter.html`` is absent from the
            templates directory.
        Any exception propagated from ``_load_deleted_items``,
            ``_load_recommendations``, or ``_load_storage_stats``.
    """
    from mediaman.services.mail.mailgun import MailgunClient

    deleted_items = _load_deleted_items(conn, secret_key, mg_settings.base_url, now)
    # F-12: pass has_recommendations through; _load_recommendations does not
    # re-query suggestions_enabled — the outer check in send_newsletter is sufficient.
    this_week_items = _load_recommendations(conn, check_enabled=False)
    storage_summary = _load_storage_stats(conn, now)

    if this_week_items:
        _annotate_rec_download_states(this_week_items, conn, secret_key)

    env = _get_jinja_env()
    if env is None:  # pragma: no cover
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("newsletter.html")

    mailgun = MailgunClient(mg_settings.domain, mg_settings.api_key, mg_settings.from_address)
    subject = _build_subject(scheduled_items, dry_run)
    report_date = _format_day_month(now, long_month=True)

    return SendContext(
        deleted_items=deleted_items,
        this_week_items=this_week_items,
        storage_summary=storage_summary,
        template=template,
        mailgun=mailgun,
        subject=subject,
        report_date=report_date,
    )


def _has_content_to_report(
    scheduled_items: list[ScheduledNewsletterItem],
    has_recommendations: bool,
) -> bool:
    """Return True when the newsletter has at least one section worth sending.

    F-05: extracted predicate (§4.2) for the inline boolean guard in
    :func:`send_newsletter`.
    """
    return bool(scheduled_items) or has_recommendations


def send_newsletter(  # rationale: extracted from send_newsletter to keep the orchestrator under the 60-line ceiling; a single context dataclass (see SendContext) would allow re-merging
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
    mg_settings = _load_mailgun_settings(conn, secret_key)
    if not mg_settings.domain:
        # All four missing — already logged at DEBUG; skip silently.
        return
    base_url = mg_settings.base_url

    subscriber_emails = _load_subscribers(conn, recipients)
    if subscriber_emails is None:
        return

    # F-13: compute now once so all sections share the same reference time
    # and cannot straddle midnight.
    now = now_utc()

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

    scheduled_items = _load_scheduled_items(conn, secret_key, base_url, now, mark_notified)
    if not _has_content_to_report(scheduled_items, has_recommendations):
        # F-11: dotted event name
        logger.debug("newsletter.skipped", extra={"reason": "no_content"})
        return

    ctx = _build_send_context(
        conn,
        mg_settings=mg_settings,
        secret_key=secret_key,
        scheduled_items=scheduled_items,
        dry_run=dry_run,
        now=now,
    )

    successfully_sent = _send_to_subscribers(
        recipient_emails=subscriber_emails,
        scheduled_items=scheduled_items,
        deleted_items=ctx.deleted_items,
        this_week_items=ctx.this_week_items,
        storage=ctx.storage_summary.stats,
        reclaimed_week=ctx.storage_summary.reclaimed_week,
        reclaimed_month=ctx.storage_summary.reclaimed_month,
        reclaimed_total=ctx.storage_summary.reclaimed_total,
        subject=ctx.subject,
        base_url=base_url,
        secret_key=secret_key,
        dry_run=dry_run,
        grace_days=grace_days,
        template=ctx.template,
        mailgun=ctx.mailgun,
        report_date=ctx.report_date,
        conn=conn if mark_notified else None,
    )

    if mark_notified and successfully_sent:
        # Only flip ``notified=1`` for items where every active subscriber was
        # successfully delivered — a partial failure must leave the row at
        # ``notified=0`` for re-attempt on the next tick.
        _mark_notified(
            conn,
            scheduled_items,
            active_recipients=subscriber_emails,
        )

    # F-11: dotted event name, variables in extra=
    logger.info(
        "newsletter.sent",
        extra={
            "sent": len(successfully_sent),
            "total": len(subscriber_emails),
            "scheduled": len(scheduled_items),
            "deleted": len(ctx.deleted_items),
        },
    )


__all__ = ["NewsletterConfigError", "send_newsletter"]
