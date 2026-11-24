"""Weekly newsletter for subscribers — public entry point.

Builds the "what's about to be deleted, what was reclaimed, what the AI
recommends" digest and sends it via Mailgun. Called automatically at the
end of every scan and on-demand from the admin UI.

This package is a thin aggregator. The heavy lifting is split into focused
submodules:

* render      — Jinja2 env, subject-line formatting.
* schedule    — scheduled-deletion cards + ``notified`` flag bookkeeping.
* summary     — disk-usage stats, deleted-items cards, recommendation batch load.
* enrich      — per-card Arr download-state annotation.
* subscribers — subscriber resolution, per-subscriber render/send loop.

Callers keep importing ``send_newsletter`` and ``NewsletterConfigError``
from mediaman.services.newsletter.
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
from .summary import (
    StorageSummary,
    _load_deleted_items,
    _load_recommendations,
    _load_storage_stats,
    load_own_picks,
)

if TYPE_CHECKING:
    from jinja2 import Template as _JinjaTemplate

    from mediaman.services.mail.mailgun import MailgunClient as _MailgunClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MailgunSettings:
    domain: str
    api_key: str
    from_address: str
    base_url: str


class NewsletterConfigError(Exception):
    """Raised when required configuration is missing. Not transient: callers should not
    retry; the administrator must fix the settings.
    """


@dataclass(frozen=True, slots=True)
class SendResult:
    """What a send did. *attempted* is 0 when there was nothing to send or Mailgun is unset."""

    sent: int = 0
    attempted: int = 0
    configured: bool = True


@dataclass(frozen=True, slots=True)
class SendContext:
    deleted_items: list[DeletedNewsletterItem]
    this_week_items: list[NewsletterRecItem]
    own_picks: dict[str, list[NewsletterRecItem]]
    storage_summary: StorageSummary
    template: _JinjaTemplate
    mailgun: _MailgunClient
    subject: str
    report_date: str


def _load_mailgun_settings(conn: sqlite3.Connection, secret_key: str) -> MailgunSettings:
    """Read and validate the four required Mailgun settings. All fields are empty when all
    four are missing (the caller returns quietly); raises NewsletterConfigError when
    only some are missing.
    """
    from mediaman.services.infra import get_string_setting

    domain = get_string_setting(conn, "mailgun_domain", secret_key=secret_key)
    api_key = get_string_setting(conn, "mailgun_api_key", secret_key=secret_key)
    from_address = get_string_setting(conn, "mailgun_from_address", secret_key=secret_key)
    base_url = get_string_setting(conn, "base_url", secret_key=secret_key).rstrip("/")

    # Reject non-HTTP(S) base_url schemes so email links can never use
    # javascript:, data:, or other dangerous schemes.
    if base_url and not base_url.lower().startswith(("http://", "https://")):
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
            logger.debug("newsletter.skipped", extra={"reason": "mailgun_not_configured"})
            return MailgunSettings(domain="", api_key="", from_address="", base_url="")
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
    this_week_items: list[NewsletterRecItem],
    own_picks: dict[str, list[NewsletterRecItem]],
    dry_run: bool,
    now: datetime,
) -> SendContext:
    """Load newsletter content and build the Mailgun client and Jinja template.

    Factored out of send_newsletter to keep the orchestrator short. *now* is computed
    once by the caller so all sections share a reference time and cannot straddle
    midnight. Raises jinja2.TemplateNotFound if newsletter.html is missing.
    """
    from mediaman.services.mail.mailgun import MailgunClient

    deleted_items = _load_deleted_items(conn, secret_key, mg_settings.base_url, now)
    storage_summary = _load_storage_stats(conn, now)

    rec_items = [*this_week_items, *(pick for picks in own_picks.values() for pick in picks)]
    if rec_items:
        _annotate_rec_download_states(rec_items, conn, secret_key)

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
        own_picks=own_picks,
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
    return bool(scheduled_items) or has_recommendations


def send_newsletter(
    conn: sqlite3.Connection,
    secret_key: str,
    dry_run: bool = False,
    *,
    recipients: list[str] | None = None,
    mark_notified: bool = True,
) -> SendResult:
    """Send the newsletter to subscribers or specific recipients.

    The scan engine's default call sends to all active subscribers, includes only
    unnotified items and marks them notified=1 afterwards. A manual call (recipients
    given, mark_notified=False) sends to those addresses, includes all scheduled items
    and leaves the notified flag alone.

    Returns how many addresses were mailed out of how many were tried, and whether Mailgun
    is configured at all.
    """
    mg_settings = _load_mailgun_settings(conn, secret_key)
    if not mg_settings.domain:
        # All four missing — already logged at DEBUG; skip silently.
        return SendResult(configured=False)
    base_url = mg_settings.base_url

    subscriber_emails = _load_subscribers(conn, recipients)
    if subscriber_emails is None:
        return SendResult()

    # Compute now once so all sections share the same reference time
    # and cannot straddle midnight.
    now = now_utc()

    from mediaman.services.infra import get_bool_setting

    this_week_items: list[NewsletterRecItem] = []
    own_picks: dict[str, list[NewsletterRecItem]] = {}
    if get_bool_setting(conn, "suggestions_enabled", default=True):
        this_week_items = _load_recommendations(conn, check_enabled=False)
        own_picks = load_own_picks(conn, subscriber_emails)
    has_recommendations = bool(this_week_items) or any(own_picks.values())

    scheduled_items = _load_scheduled_items(conn, secret_key, base_url, now, mark_notified)
    if not _has_content_to_report(scheduled_items, has_recommendations):
        logger.debug("newsletter.skipped", extra={"reason": "no_content"})
        return SendResult()

    ctx = _build_send_context(
        conn,
        mg_settings=mg_settings,
        secret_key=secret_key,
        scheduled_items=scheduled_items,
        this_week_items=this_week_items,
        own_picks=own_picks,
        dry_run=dry_run,
        now=now,
    )

    successfully_sent = _send_to_subscribers(
        recipient_emails=subscriber_emails,
        scheduled_items=scheduled_items,
        deleted_items=ctx.deleted_items,
        this_week_items=ctx.this_week_items,
        own_picks=ctx.own_picks,
        storage=ctx.storage_summary.stats,
        reclaimed_week=ctx.storage_summary.reclaimed_week,
        reclaimed_month=ctx.storage_summary.reclaimed_month,
        reclaimed_total=ctx.storage_summary.reclaimed_total,
        subject=ctx.subject,
        base_url=base_url,
        secret_key=secret_key,
        dry_run=dry_run,
        template=ctx.template,
        mailgun=ctx.mailgun,
        report_date=ctx.report_date,
        conn=conn if mark_notified else None,
    )

    if mark_notified:
        # Only flip ``notified=1`` for items where every active subscriber was
        # successfully delivered — a partial failure must leave the row at
        # ``notified=0`` for re-attempt on the next tick.
        _mark_notified(
            conn,
            scheduled_items,
            active_recipients=subscriber_emails,
        )

    logger.info(
        "newsletter.sent",
        extra={
            "sent": len(successfully_sent),
            "total": len(subscriber_emails),
            "scheduled": len(scheduled_items),
            "deleted": len(ctx.deleted_items),
        },
    )
    return SendResult(sent=len(successfully_sent), attempted=len(subscriber_emails))


__all__ = ["NewsletterConfigError", "SendResult", "send_newsletter"]
