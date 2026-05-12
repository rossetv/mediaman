"""Subscriber resolution and per-recipient delivery loop."""

from __future__ import annotations

import logging
import sqlite3
from typing import cast
from urllib.parse import quote as _url_quote

import requests

from mediaman.core.time import now_iso
from mediaman.crypto import generate_download_token, generate_unsubscribe_token
from mediaman.services.infra import SafeHTTPError

from ._types import DeletedNewsletterItem, NewsletterRecItem, ScheduledNewsletterItem, StorageStats

logger = logging.getLogger(__name__)


def _load_recipients(conn: sqlite3.Connection, recipients: list[str] | None) -> list[str] | None:
    """Return the recipient list, or ``None`` to signal "skip — no recipients".

    When *recipients* is provided it is returned as-is.  Otherwise the
    active subscribers table is queried; ``None`` is returned (not an empty
    list) when there are no active subscribers so the caller can skip
    quietly rather than sending to zero addresses.
    """
    if recipients:
        return recipients
    rows = conn.execute("SELECT email FROM subscribers WHERE active=1").fetchall()
    if not rows:
        logger.debug("Newsletter skipped — no active subscribers")
        return None
    return [row["email"] for row in rows]


def _record_delivery_attempt(
    conn: sqlite3.Connection,
    *,
    scheduled_action_ids: list[int],
    recipient: str,
    success: bool,
    error: str | None,
) -> None:
    """Persist one row per (scheduled_action, recipient) for the send.

    The newsletter previously flagged each scheduled item as ``notified=1``
    after the first successful Mailgun call. With multiple subscribers a
    later send failure would silently drop notifications for everyone
    after the first success. We now record one row per scheduled-item ×
    recipient pair so the orchestrating function can decide whether to
    mark the item done only when *every* recipient has been served.

    Best-effort: a row that cannot be persisted is logged but does not
    break the send loop.
    """
    if not scheduled_action_ids:
        return
    sent_at = now_iso() if success else None
    attempted_at = now_iso()
    err_text = None if success else (error or "send failed")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO newsletter_deliveries "
            "(scheduled_action_id, recipient, sent_at, error, attempted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (action_id, recipient, sent_at, err_text, attempted_at)
                for action_id in scheduled_action_ids
            ],
        )
        conn.commit()
    except sqlite3.Error:
        logger.warning(
            "newsletter delivery record failed recipient=%s actions=%d",
            recipient,
            len(scheduled_action_ids),
            exc_info=True,
        )


def _send_to_recipients(
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
    template,
    mailgun,
    report_date: str,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Render and send the newsletter to each recipient.

    Returns the list of email addresses to which sending succeeded.
    Each recipient gets a unique unsubscribe URL and per-item download tokens.

    When *conn* is provided we also persist a per-recipient delivery
    record for every scheduled item. The orchestrator then only flips
    ``notified=1`` for items where every active recipient has succeeded.
    """
    scheduled_action_ids = [
        int(item["_action_id"]) for item in scheduled_items if item.get("_action_id") is not None
    ]
    successfully_sent: list[str] = []
    for email in recipient_emails:
        unsub_token = generate_unsubscribe_token(email=email, secret_key=secret_key)
        # The email is encoded inside the signed token — no need to expose it
        # as a query parameter, which would leak PII into server logs.
        unsub_url = (
            f"{base_url}/unsubscribe?token={_url_quote(unsub_token, safe='')}" if base_url else ""
        )
        logger.debug("newsletter.unsub_url_minted recipient=%s", email)

        # Build per-recipient shallow copies so token URLs don't bleed between recipients.
        # Without this, recipient N's tokens overwrite recipient N-1's in the shared dicts.
        recipient_deleted: list[DeletedNewsletterItem] = [
            cast(DeletedNewsletterItem, dict(item)) for item in deleted_items
        ]
        recipient_this_week: list[NewsletterRecItem] = [
            cast(NewsletterRecItem, dict(item)) for item in this_week_items
        ]

        for del_item in recipient_deleted:
            # Finding 15: only mint a public re-download token when we have a
            # stable TMDB identifier on the deleted item.  Without one, the
            # public submit endpoint would have to fall back to title lookup,
            # which can enqueue the wrong film/show.  When tmdb_id is missing
            # the template's ``{% if item.redownload_url %}`` guard hides the
            # button rather than render a link that would fail at submit.
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

        for rec_item in recipient_this_week:
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

        html = template.render(
            report_date=report_date,
            storage=storage,
            reclaimed_week=reclaimed_week,
            reclaimed_month=reclaimed_month,
            reclaimed_total=reclaimed_total,
            scheduled_items=scheduled_items,
            deleted_items=recipient_deleted,
            this_week_items=recipient_this_week,
            dashboard_url=base_url,
            dry_run=dry_run,
            base_url=base_url,
            grace_days=grace_days,
            unsubscribe_url=unsub_url,
        )
        try:
            mailgun.send(to=email, subject=subject, html=html)
            successfully_sent.append(email)
            if conn is not None:
                _record_delivery_attempt(
                    conn,
                    scheduled_action_ids=scheduled_action_ids,
                    recipient=email,
                    success=True,
                    error=None,
                )
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            logger.exception("Newsletter send failed for %s — continuing", email)
            if conn is not None:
                _record_delivery_attempt(
                    conn,
                    scheduled_action_ids=scheduled_action_ids,
                    recipient=email,
                    success=False,
                    error=str(exc),
                )

    return successfully_sent
