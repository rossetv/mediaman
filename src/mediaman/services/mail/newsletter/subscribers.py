"""Subscriber resolution and per-subscriber delivery loop."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, cast
from urllib.parse import quote as _url_quote

from requests.exceptions import RequestException

from mediaman.core.time import now_iso
from mediaman.crypto import generate_download_token, generate_unsubscribe_token
from mediaman.services.infra import SafeHTTPError

from ._types import DeletedNewsletterItem, NewsletterRecItem, ScheduledNewsletterItem, StorageStats

if TYPE_CHECKING:
    from jinja2 import Template as _JinjaTemplate

    from mediaman.services.mail.mailgun import MailgunClient as _MailgunClient

logger = logging.getLogger(__name__)


def _load_subscribers(conn: sqlite3.Connection, recipients: list[str] | None) -> list[str] | None:
    """Return the subscriber list, or ``None`` to signal "skip — no subscribers".

    When *recipients* is provided and not ``None`` it is returned as-is (even
    if empty — an explicit empty list means "send to nobody").  Otherwise the
    active subscribers table is queried; ``None`` is returned (not an empty
    list) when there are no active subscribers so the caller can skip
    quietly rather than sending to zero addresses.
    """
    # F-07: use ``is not None`` so an explicit empty list is honoured and
    # does not fall through to the full subscriber query.
    if recipients is not None:
        return recipients
    rows = conn.execute("SELECT email FROM subscribers WHERE active=1").fetchall()
    if not rows:
        logger.debug("newsletter.skipped", extra={"reason": "no_active_subscribers"})
        return None
    return [row["email"] for row in rows]


def _record_delivery_attempt(
    conn: sqlite3.Connection,
    *,
    scheduled_action_ids: list[int],
    subscriber: str,
    success: bool,
    error: str | None,
) -> None:
    """Persist one row per (scheduled_action, subscriber) for the send.

    The newsletter previously flagged each scheduled item as ``notified=1``
    after the first successful Mailgun call. With multiple subscribers a
    later send failure would silently drop notifications for everyone
    after the first success. We now record one row per scheduled-item ×
    subscriber pair so the orchestrating function can decide whether to
    mark the item done only when *every* subscriber has been served.

    Best-effort: a row that cannot be persisted is logged but does not
    break the send loop.

    Note: the DB column is literally named ``recipient`` (part of a composite
    PRIMARY KEY on ``newsletter_deliveries``); we retain that name in SQL
    to avoid a destructive schema migration.
    """
    if not scheduled_action_ids:
        return
    sent_at = now_iso() if success else None
    attempted_at = now_iso()
    err_text = None if success else (error or "send failed")
    try:
        # F-08: wrap DML in ``with conn:`` so a crash between INSERT and
        # commit does not leave partial rows without a commit.
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO newsletter_deliveries "
                # legacy column name ``recipient`` retained — see docstring
                "(scheduled_action_id, recipient, sent_at, error, attempted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (action_id, subscriber, sent_at, err_text, attempted_at)
                    for action_id in scheduled_action_ids
                ],
            )
    except sqlite3.Error:
        # F-17: variables in extra=, not interpolated into the message string
        logger.warning(
            "newsletter.delivery_record_failed",
            extra={"subscriber": subscriber, "action_count": len(scheduled_action_ids)},
            exc_info=True,
        )


def _mint_deleted_tokens(
    items: list[DeletedNewsletterItem],
    *,
    email: str,
    base_url: str,
    secret_key: str,
) -> None:
    """Stamp ``redownload_url`` on each deleted item in-place.

    Only mint a re-download token when we have a stable TMDB identifier;
    without one, the /download submit endpoint would have to fall back to
    title lookup, which can enqueue the wrong title.  When tmdb_id is
    missing the template's ``{% if item.redownload_url %}`` guard hides
    the button rather than render a link that would fail at submit.
    """
    for del_item in items:
        item_tmdb = del_item.get("tmdb_id")
        if base_url and item_tmdb:
            token = generate_download_token(
                email=email,
                action="redownload",
                title=del_item["title"],
                media_type=del_item.get("media_type", "movie"),
                tmdb_id=item_tmdb,
                recommendation_id=None,
                secret_key=secret_key,
            )
            del_item["redownload_url"] = f"{base_url}/download/{token}"
        else:
            del_item["redownload_url"] = ""


def _mint_rec_tokens(
    items: list[NewsletterRecItem],
    *,
    email: str,
    base_url: str,
    secret_key: str,
) -> None:
    """Stamp ``download_url`` on each recommendation item in-place."""
    for rec_item in items:
        if base_url:
            token = generate_download_token(
                email=email,
                action="download",
                title=rec_item["title"],
                media_type=rec_item["media_type"],
                tmdb_id=rec_item.get("tmdb_id"),
                recommendation_id=rec_item.get("id"),
                secret_key=secret_key,
            )
            rec_item["download_url"] = f"{base_url}/download/{token}"
        else:
            rec_item["download_url"] = ""


def _render_for_subscriber(
    *,
    email: str,
    deleted_items: list[DeletedNewsletterItem],
    this_week_items: list[NewsletterRecItem],
    scheduled_items: list[ScheduledNewsletterItem],
    storage: StorageStats,
    reclaimed_week: int,
    reclaimed_month: int,
    reclaimed_total: int,
    report_date: str,
    base_url: str,
    secret_key: str,
    dry_run: bool,
    grace_days: int,
    template: _JinjaTemplate,
) -> str:
    """Mint per-subscriber tokens and render the newsletter HTML.

    Builds per-subscriber shallow copies of deleted and recommendation items
    so token URLs don't bleed between subscribers, then mints unsubscribe and
    download tokens before calling ``template.render``.

    Returns the rendered HTML string — no side effects, no DB access.
    """
    unsub_token = generate_unsubscribe_token(email=email, secret_key=secret_key)
    # The email is encoded inside the signed token — no need to expose it
    # as a query parameter, which would leak PII into server logs.
    unsub_url = (
        f"{base_url}/unsubscribe?token={_url_quote(unsub_token, safe='')}" if base_url else ""
    )
    # F-11: dotted event name, variable in extra=
    logger.debug("newsletter.unsub_url_minted", extra={"subscriber": email})

    # Build per-subscriber shallow copies so token URLs don't bleed between subscribers.
    # Without this, subscriber N's tokens overwrite subscriber N-1's in the shared dicts.
    subscriber_deleted: list[DeletedNewsletterItem] = [
        cast(DeletedNewsletterItem, dict(item)) for item in deleted_items
    ]
    subscriber_this_week: list[NewsletterRecItem] = [
        cast(NewsletterRecItem, dict(item)) for item in this_week_items
    ]

    _mint_deleted_tokens(subscriber_deleted, email=email, base_url=base_url, secret_key=secret_key)
    _mint_rec_tokens(subscriber_this_week, email=email, base_url=base_url, secret_key=secret_key)

    return template.render(
        report_date=report_date,
        storage=storage,
        reclaimed_week=reclaimed_week,
        reclaimed_month=reclaimed_month,
        reclaimed_total=reclaimed_total,
        scheduled_items=scheduled_items,
        deleted_items=subscriber_deleted,
        this_week_items=subscriber_this_week,
        dashboard_url=base_url,
        dry_run=dry_run,
        base_url=base_url,
        grace_days=grace_days,
        unsubscribe_url=unsub_url,
    )


def _send_to_subscribers(
    *,
    recipient_emails: list[str],
    scheduled_items: list[ScheduledNewsletterItem],
    deleted_items: list[DeletedNewsletterItem],
    this_week_items: list[NewsletterRecItem],
    storage: StorageStats,
    reclaimed_week: int,
    reclaimed_month: int,
    reclaimed_total: int,
    subject: str,
    base_url: str,
    secret_key: str,
    dry_run: bool,
    grace_days: int,
    template: _JinjaTemplate,
    mailgun: _MailgunClient,
    report_date: str,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Render and send the newsletter to each subscriber.

    Returns the list of email addresses to which sending succeeded.
    Each subscriber gets a unique unsubscribe URL and per-item download tokens.

    When *conn* is provided we also persist a per-subscriber delivery
    record for every scheduled item. The orchestrator then only flips
    ``notified=1`` for items where every active subscriber has succeeded.
    """
    scheduled_action_ids = [
        int(item["_action_id"]) for item in scheduled_items if item.get("_action_id") is not None
    ]
    successfully_sent: list[str] = []
    for email in recipient_emails:
        html = _render_for_subscriber(
            email=email,
            deleted_items=deleted_items,
            this_week_items=this_week_items,
            scheduled_items=scheduled_items,
            storage=storage,
            reclaimed_week=reclaimed_week,
            reclaimed_month=reclaimed_month,
            reclaimed_total=reclaimed_total,
            report_date=report_date,
            base_url=base_url,
            secret_key=secret_key,
            dry_run=dry_run,
            grace_days=grace_days,
            template=template,
        )
        try:
            mailgun.send(to=email, subject=subject, html=html)
            successfully_sent.append(email)
            if conn is not None:
                _record_delivery_attempt(
                    conn,
                    scheduled_action_ids=scheduled_action_ids,
                    subscriber=email,
                    success=True,
                    error=None,
                )
        except (SafeHTTPError, RequestException, ValueError) as exc:
            # F-11: dotted event name, variable in extra=
            logger.exception(
                "newsletter.send_failed",
                extra={"subscriber": email},
            )
            if conn is not None:
                _record_delivery_attempt(
                    conn,
                    scheduled_action_ids=scheduled_action_ids,
                    subscriber=email,
                    success=False,
                    error=str(exc),
                )

    return successfully_sent
