"""Subscriber resolution and per-recipient delivery loop."""

from __future__ import annotations

import logging
import sqlite3
from urllib.parse import quote as _url_quote

from mediaman.crypto import generate_download_token, generate_unsubscribe_token

logger = logging.getLogger("mediaman")


def _mask_email(email: str) -> str:
    """Return a masked representation of *email* for log output.

    Exposes only the first character of the local part plus the total length,
    e.g. ``"a...@example.com (len=17)"``.  The domain is retained so operators
    can still triage delivery failures without exposing the full address.
    """
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return f"(len={len(email)})"
    first = local[0] if local else "?"
    return f"{first}...@{domain} (len={len(email)})"


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


def _send_to_recipients(
    *,
    recipient_emails: list[str],
    scheduled_items: list[dict],
    deleted_items: list[dict],
    this_week_items: list[dict],
    storage: dict,
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
) -> list[str]:
    """Render and send the newsletter to each recipient.

    Returns the list of email addresses to which sending succeeded.
    Each recipient gets a unique unsubscribe URL and per-item download tokens.
    """
    successfully_sent: list[str] = []
    for email in recipient_emails:
        unsub_token = generate_unsubscribe_token(email=email, secret_key=secret_key)
        # The email is encoded inside the signed token — no need to expose it
        # as a query parameter (finding 36).
        unsub_url = (
            f"{base_url}/unsubscribe?token={_url_quote(unsub_token, safe='')}" if base_url else ""
        )
        logger.debug("newsletter.unsub_url_minted recipient=%s", _mask_email(email))

        # Build per-recipient shallow copies so token URLs don't bleed between recipients.
        # Without this, recipient N's tokens overwrite recipient N-1's in the shared dicts.
        recipient_deleted = [dict(item) for item in deleted_items]
        recipient_this_week = [dict(item) for item in this_week_items]

        for item in recipient_deleted:
            if base_url:
                token = generate_download_token(
                    email=email,
                    action="redownload",
                    title=item["title"],
                    media_type=item.get("media_type", "movie"),
                    tmdb_id=None,
                    recommendation_id=None,
                    secret_key=secret_key,
                )
                item["redownload_url"] = f"{base_url}/download/{token}"
            else:
                item["redownload_url"] = ""

        for item in recipient_this_week:
            if base_url:
                token = generate_download_token(
                    email=email,
                    action="download",
                    title=item["title"],
                    media_type=item["media_type"],
                    tmdb_id=item.get("tmdb_id"),
                    recommendation_id=item.get("id"),
                    secret_key=secret_key,
                )
                item["download_url"] = f"{base_url}/download/{token}"
            else:
                item["download_url"] = ""

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
        except Exception:
            logger.exception("Newsletter send failed for %s — continuing", email)

    return successfully_sent
