"""Weekly newsletter for subscribers.

Builds the "what's about to be deleted, what was reclaimed, what the AI
recommends" digest and sends it via Mailgun. Called automatically at the
end of every scan and on-demand from the admin UI.

Internal structure
------------------
The public entry point is :func:`send_newsletter`.  The heavy lifting is
split into focused private helpers:

- :func:`_load_mailgun_settings` — reads and validates the four required settings.
- :func:`_load_recipients` — returns the list of email addresses to send to.
- :func:`_load_scheduled_items` — builds the scheduled-deletion card list.
- :func:`_load_deleted_items` — builds the recently-deleted card list.
- :func:`_load_recommendations` — loads the most recent suggestion batch.
- :func:`_load_storage_stats` — builds the storage/reclaimed-space block.
- :func:`_annotate_rec_download_states` — annotates rec items with Arr state.
- :func:`_build_subject` — formats the email subject line.
- :func:`_send_to_recipients` — sends per-recipient and returns sent addresses.
- :func:`_mark_notified` — marks scheduled actions notified=1 after a successful send.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote as _url_quote

from mediaman.services.format import ensure_tz as _ensure_tz
from mediaman.services.format import format_day_month as _format_day_month
from mediaman.services.format import rk_from_audit_detail as _extract_rk_from_detail
from mediaman.services.format import title_from_audit_detail as _extract_title_from_detail

logger = logging.getLogger("mediaman")

# ---------------------------------------------------------------------------
# Module-level Jinja2 environment — built once per process, not per send.
# Re-building ``Environment`` on every call was wasteful: it re-compiled
# templates, re-initialised the filter registry, and re-walked the template
# directory.  The environment is stateless once built so sharing it is safe.
# ---------------------------------------------------------------------------
_TEMPLATE_DIR = Path(__file__).parent.parent / "web" / "templates"

try:
    from jinja2 import Environment, FileSystemLoader
    _JINJA_ENV: "Environment | None" = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True
    )
except Exception:  # pragma: no cover — only fails if Jinja2 is missing
    _JINJA_ENV = None


class NewsletterConfigError(Exception):
    """Raised when the newsletter cannot be sent due to missing configuration.

    Distinct from a transient send failure — callers should not retry
    automatically; the administrator must fix the settings first.
    """


def _parse_days_ago(value: str | None, now: datetime) -> int | None:
    """Parse an ISO datetime string and return the number of days before *now*.

    Returns ``None`` when *value* is empty or cannot be parsed, logging a
    warning (with traceback) on parse failure so silently-wrong timestamps
    don't go unnoticed.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        dt = _ensure_tz(dt)
        return (now - dt).days
    except (ValueError, TypeError):
        logger.warning("Failed to parse days value: %r", value, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_mailgun_settings(
    conn: sqlite3.Connection, secret_key: str
) -> tuple[str, str, str, str]:
    """Read and validate the four required Mailgun settings.

    Returns ``(domain, api_key, from_address, base_url)``.

    Raises :exc:`NewsletterConfigError` when some (but not all) required
    settings are missing.  When *all* four are missing, logs at DEBUG and
    returns empty strings (caller should then return early quietly).
    """
    from mediaman.services.settings_reader import get_string_setting

    domain = get_string_setting(conn, "mailgun_domain", secret_key=secret_key)
    api_key = get_string_setting(conn, "mailgun_api_key", secret_key=secret_key)
    from_address = get_string_setting(conn, "mailgun_from_address", secret_key=secret_key)
    base_url = get_string_setting(conn, "base_url", secret_key=secret_key).rstrip("/")

    missing = [k for k, v in (
        ("mailgun_domain", domain),
        ("mailgun_api_key", api_key),
        ("mailgun_from_address", from_address),
        ("base_url", base_url),
    ) if not v]

    if missing:
        if set(missing) == {"mailgun_domain", "mailgun_api_key", "mailgun_from_address", "base_url"}:
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


def _load_recipients(
    conn: sqlite3.Connection, recipients: list[str] | None
) -> list[str] | None:
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


def _load_scheduled_items(
    conn: sqlite3.Connection,
    secret_key: str,
    base_url: str,
    now: datetime,
    mark_notified: bool,
) -> list[dict]:
    """Query and build the scheduled-deletion card list.

    When *mark_notified* is True (automated send), only unnotified rows are
    included.  When False (manual send), all pending-token rows are included.
    """
    from mediaman.crypto import sign_poster_url

    _COLS = (
        "sa.id, sa.media_item_id, sa.token, sa.is_reentry, "
        "mi.title, mi.media_type, mi.season_number, mi.plex_rating_key, mi.file_size_bytes, "
        "mi.added_at, mi.last_watched_at "
        "FROM scheduled_actions sa "
        "JOIN media_items mi ON sa.media_item_id = mi.id "
        "WHERE sa.action='scheduled_deletion' AND "
    )
    if mark_notified:
        rows = conn.execute("SELECT " + _COLS + "sa.notified=0").fetchall()
    else:
        rows = conn.execute("SELECT " + _COLS + "sa.token_used=0").fetchall()

    items = []
    for row in rows:
        added_days_ago = _parse_days_ago(row["added_at"], now)
        rating_key = row["plex_rating_key"] or ""
        poster_url = (
            f"{base_url}{sign_poster_url(rating_key, secret_key)}"
            if rating_key and base_url
            else ""
        )

        last_watched_info = None
        lw_raw = row["last_watched_at"]
        if lw_raw:
            lw_days = _parse_days_ago(lw_raw, now)
            if lw_days is not None:
                if lw_days == 0:
                    last_watched_info = "Watched today"
                elif lw_days == 1:
                    last_watched_info = "Watched yesterday"
                else:
                    last_watched_info = f"Watched {lw_days} days ago"

        media_type = row["media_type"] or "movie"
        season_num = row["season_number"]
        if media_type in ("tv_season", "season", "tv"):
            type_label = f"TV · Season {season_num}" if season_num else "TV"
        elif media_type in ("anime_season", "anime"):
            type_label = f"Anime · Season {season_num}" if season_num else "Anime"
        else:
            type_label = "Movie"

        items.append({
            "title": row["title"],
            "media_type": media_type,
            "type_label": type_label,
            "poster_url": poster_url,
            "file_size_bytes": row["file_size_bytes"] or 0,
            "added_days_ago": added_days_ago,
            "last_watched_info": last_watched_info,
            "keep_url": f"{base_url}/keep/{row['token']}",
            "is_reentry": bool(row["is_reentry"]),
            "_action_id": row["id"],
        })

    # Sort oldest first (most days ago at the top)
    items.sort(key=lambda x: x.get("added_days_ago") or 0, reverse=True)
    return items


def _load_deleted_items(
    conn: sqlite3.Connection,
    secret_key: str,
    base_url: str,
    now: datetime,
) -> list[dict]:
    """Query and build the recently-deleted card list (last 7 days).

    Items that were re-downloaded after their deletion timestamp are silently
    excluded so the newsletter doesn't ask subscribers to re-download content
    that has already been replaced.
    """
    from mediaman.crypto import sign_poster_url

    week_ago = (now - timedelta(days=7)).isoformat()
    deleted_rows = conn.execute(
        "SELECT al.created_at, al.space_reclaimed_bytes, "
        "mi.title, al.detail, mi.plex_rating_key, mi.media_type "
        "FROM audit_log al "
        "LEFT JOIN media_items mi ON al.media_item_id = mi.id "
        "WHERE al.action='deleted' AND al.created_at >= ? "
        "ORDER BY al.created_at DESC LIMIT 10",
        (week_ago,),
    ).fetchall()

    redownload_rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    redownload_times: dict[str, str] = {}
    for rd in redownload_rows:
        key = rd["media_item_id"].lower()
        if key not in redownload_times or rd["created_at"] > redownload_times[key]:
            redownload_times[key] = rd["created_at"]

    items = []
    for row in deleted_rows:
        title = row["title"] or _extract_title_from_detail(row["detail"])

        last_redownload = redownload_times.get(title.lower())
        if last_redownload and last_redownload > row["created_at"]:
            continue

        days_ago = _parse_days_ago(row["created_at"], now)
        if days_ago is None:
            deleted_date = ""
        elif days_ago == 0:
            deleted_date = "today"
        elif days_ago == 1:
            deleted_date = "yesterday"
        else:
            deleted_date = f"{days_ago} days ago"

        rating_key = row["plex_rating_key"] or _extract_rk_from_detail(row["detail"]) or ""
        poster_url = (
            f"{base_url}{sign_poster_url(rating_key, secret_key)}"
            if rating_key and base_url
            else ""
        )

        items.append({
            "title": title,
            "poster_url": poster_url,
            "deleted_date": deleted_date,
            "file_size_bytes": row["space_reclaimed_bytes"] or 0,
            "media_type": row["media_type"] or "movie",
        })

    return items


def _load_recommendations(conn: sqlite3.Connection) -> list[dict]:
    """Load the most recent suggestion batch if the feature is enabled.

    Returns an empty list when suggestions are disabled or there are no rows.
    Builds explicit dicts with only the fields the template needs, avoiding
    leakage of internal DB columns via ``**dict(row)`` spreading.
    """
    rec_enabled_row = conn.execute(
        "SELECT value FROM settings WHERE key='suggestions_enabled'"
    ).fetchone()
    if rec_enabled_row and rec_enabled_row["value"] == "false":
        return []

    batch_row = conn.execute(
        "SELECT DISTINCT batch_id FROM suggestions WHERE batch_id IS NOT NULL "
        "ORDER BY batch_id DESC LIMIT 1"
    ).fetchone()
    if not batch_row:
        return []

    rows = conn.execute(
        "SELECT id, title, media_type, category, description, reason, "
        "poster_url, tmdb_id, rating, rt_rating "
        "FROM suggestions WHERE batch_id = ? ORDER BY category DESC, id",
        (batch_row["batch_id"],),
    ).fetchall()

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "media_type": r["media_type"],
            "category": r["category"],
            "description": r["description"],
            "reason": r["reason"],
            "poster_url": r["poster_url"],
            "tmdb_id": r["tmdb_id"],
            "rating": r["rating"],
            "rt_rating": r["rt_rating"],
        }
        for r in rows
    ]


def _load_storage_stats(conn: sqlite3.Connection, now: datetime) -> tuple[dict, int, int, int]:
    """Build storage stats and reclaimed-space totals.

    Returns ``(storage_dict, reclaimed_week, reclaimed_month, reclaimed_total)``.
    """
    from mediaman.services.storage import get_aggregate_disk_usage

    type_rows = conn.execute(
        "SELECT media_type, SUM(file_size_bytes) AS total "
        "FROM media_items GROUP BY media_type"
    ).fetchall()
    raw_types: dict[str, int] = {r["media_type"]: (r["total"] or 0) for r in type_rows}
    by_type: dict[str, int] = {
        "movie": raw_types.get("movie", 0),
        "show": (raw_types.get("tv_season", 0) + raw_types.get("tv", 0)
                 + raw_types.get("season", 0)),
        "anime": (raw_types.get("anime_season", 0) + raw_types.get("anime", 0)),
    }
    used_bytes = sum(by_type.values())
    total_bytes = used_bytes
    free_bytes = 0
    try:
        disk = get_aggregate_disk_usage("/media")
        total_bytes = disk["total_bytes"]
        used_bytes = disk["used_bytes"]
        free_bytes = disk["free_bytes"]
    except Exception:
        logger.warning("Failed to fetch disk usage for newsletter", exc_info=True)

    storage = {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "by_type": by_type,
    }

    def _reclaimed_since(since_iso: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(space_reclaimed_bytes), 0) AS total "
            "FROM audit_log WHERE action='deleted' AND created_at >= ?",
            (since_iso,),
        ).fetchone()
        return row["total"] if row else 0

    week_start = (now - timedelta(days=7)).isoformat()
    month_start = (now - timedelta(days=30)).isoformat()
    reclaimed_week = _reclaimed_since(week_start)
    reclaimed_month = _reclaimed_since(month_start)
    reclaimed_total_row = conn.execute(
        "SELECT COALESCE(SUM(space_reclaimed_bytes), 0) AS total "
        "FROM audit_log WHERE action='deleted'"
    ).fetchone()
    reclaimed_total = reclaimed_total_row["total"] if reclaimed_total_row else 0

    return storage, reclaimed_week, reclaimed_month, reclaimed_total


def _annotate_rec_download_states(
    rec_items: list[dict],
    conn: sqlite3.Connection,
    secret_key: str,
) -> None:
    """Annotate recommendation items in place with their Arr download state.

    Populates ``item["download_state"]`` (``in_library`` / ``partial`` /
    ``downloading`` / ``queued``) by consulting Radarr and Sonarr caches.
    Silently skips items with no ``tmdb_id``.
    """
    from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
    from mediaman.services.arr_state import (
        build_radarr_cache,
        build_sonarr_cache,
        compute_download_state,
    )

    radarr_client = build_radarr_from_db(conn, secret_key)
    sonarr_client = build_sonarr_from_db(conn, secret_key)
    try:
        radarr_cache = build_radarr_cache(radarr_client)
    except Exception:
        logger.warning("Failed to build Radarr cache for newsletter; skipping download states", exc_info=True)
        radarr_cache = build_radarr_cache(None)
    try:
        sonarr_cache = build_sonarr_cache(sonarr_client)
    except Exception:
        logger.warning("Failed to build Sonarr cache for newsletter; skipping download states", exc_info=True)
        sonarr_cache = build_sonarr_cache(None)

    caches = {**radarr_cache, **sonarr_cache}
    for item in rec_items:
        tmdb_id = item.get("tmdb_id")
        if not tmdb_id:
            continue
        state = compute_download_state(
            item.get("media_type") or "movie", tmdb_id, caches,
        )
        if state is not None:
            item["download_state"] = state


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
    from mediaman.crypto import generate_download_token, generate_unsubscribe_token

    successfully_sent: list[str] = []
    for email in recipient_emails:
        unsub_token = generate_unsubscribe_token(email=email, secret_key=secret_key)
        unsub_url = (
            f"{base_url}/unsubscribe?email={_url_quote(email, safe='@')}"
            f"&token={_url_quote(unsub_token, safe='')}"
        ) if base_url else ""

        for item in deleted_items:
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

        for item in this_week_items:
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
            deleted_items=deleted_items,
            this_week_items=this_week_items,
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


def _mark_notified(conn: sqlite3.Connection, scheduled_items: list[dict]) -> None:
    """Mark scheduled action rows as notified=1.

    Asserts all ids are integers before building the parameterised query so a
    non-integer id (e.g. from a corrupt row) surfaces as a clear error rather
    than silently passing a string through to the SQL engine.
    """
    action_ids = [int(item["_action_id"]) for item in scheduled_items]
    if not action_ids:
        return
    placeholders = ",".join("?" * len(action_ids))
    conn.execute(
        f"UPDATE scheduled_actions SET notified=1 WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are '?' only; ids asserted int above
        action_ids,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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
    from mediaman.services.mailgun import MailgunClient

    domain, api_key, from_address, base_url = _load_mailgun_settings(conn, secret_key)
    if not domain:
        # All four missing — already logged at DEBUG; skip silently.
        return

    recipient_emails = _load_recipients(conn, recipients)
    if recipient_emails is None:
        return

    # Check if there is content worth sending.
    scheduled_items = _load_scheduled_items(conn, secret_key, base_url, datetime.now(timezone.utc), mark_notified)
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

    if _JINJA_ENV is not None:
        env = _JINJA_ENV
    else:
        from jinja2 import Environment, FileSystemLoader  # pragma: no cover
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
    )

    if mark_notified and successfully_sent:
        _mark_notified(conn, scheduled_items)

    logger.info(
        "Newsletter sent to %d/%d subscriber(s) — %d scheduled, %d deleted",
        len(successfully_sent),
        len(recipient_emails),
        len(scheduled_items),
        len(deleted_items),
    )
