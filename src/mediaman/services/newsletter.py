"""Weekly newsletter for subscribers.

Builds the "what's about to be deleted, what was reclaimed, what the AI
recommends" digest and sends it via Mailgun. Called automatically at the
end of every scan and on-demand from the admin UI.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("mediaman")


def _ensure_tz(dt: datetime) -> datetime:
    """Return *dt* in UTC, treating naive datetimes as local time.

    Matches the helper used elsewhere in the scanner — PlexAPI emits
    naive datetimes via ``fromtimestamp`` (which means local time), so
    ``astimezone`` is the correct coercion, not ``replace(tzinfo=UTC)``.
    """
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.astimezone(timezone.utc)
    return dt


def _extract_title_from_detail(detail: str | None) -> str:
    """Extract a media title from an ``audit_log.detail`` string.

    Handles ``"Deleted: Title [rk:X]"`` and ``"Deleted 'Title' by user [rk:X]"``.
    """
    if not detail:
        return "Unknown"
    m = re.match(r"^Deleted[: ]+['\"]?(.+?)['\"]?(?:\s+by\s+.+?)?(?:\s+\[rk:.*\])?$", detail)
    return m.group(1) if m else detail


def _extract_rk_from_detail(detail: str | None) -> str | None:
    """Extract the ``plex_rating_key`` from an ``[rk:...]`` tag in a detail string."""
    if not detail:
        return None
    m = re.search(r"\[rk:([^\]]+)\]", detail)
    return m.group(1) if m else None


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
    from jinja2 import Environment, FileSystemLoader

    from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
    from mediaman.services.arr_state import (
        build_radarr_cache,
        build_sonarr_cache,
        compute_download_state,
    )
    from mediaman.services.mailgun import MailgunClient
    from mediaman.services.settings_reader import get_string_setting
    from mediaman.web.routes.poster import sign_poster_url

    # ── Load Mailgun settings ────────────────────────────────────────────────
    domain = get_string_setting(conn, "mailgun_domain", secret_key=secret_key)
    api_key = get_string_setting(conn, "mailgun_api_key", secret_key=secret_key)
    from_address = get_string_setting(conn, "mailgun_from_address", secret_key=secret_key)
    base_url = get_string_setting(conn, "base_url", secret_key=secret_key).rstrip("/")

    if not domain or not api_key:
        logger.debug("Newsletter skipped — Mailgun not configured")
        return

    # ── Recipients ───────────────────────────────────────────────────────────
    if recipients:
        recipient_emails = recipients
    else:
        subscribers = conn.execute(
            "SELECT email FROM subscribers WHERE active=1"
        ).fetchall()
        if not subscribers:
            logger.debug("Newsletter skipped — no active subscribers")
            return
        recipient_emails = [row["email"] for row in subscribers]

    # ── Scheduled items ─────────────────────────────────────────────────────
    # Manual sends include all scheduled items; automated sends only unnotified
    if mark_notified:
        scheduled_rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.token, sa.is_reentry, "
            "mi.title, mi.media_type, mi.season_number, mi.plex_rating_key, mi.file_size_bytes, "
            "mi.added_at, mi.last_watched_at "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.action='scheduled_deletion' AND sa.notified=0"
        ).fetchall()
    else:
        scheduled_rows = conn.execute(
            "SELECT sa.id, sa.media_item_id, sa.token, sa.is_reentry, "
            "mi.title, mi.media_type, mi.season_number, mi.plex_rating_key, mi.file_size_bytes, "
            "mi.added_at, mi.last_watched_at "
            "FROM scheduled_actions sa "
            "JOIN media_items mi ON sa.media_item_id = mi.id "
            "WHERE sa.action='scheduled_deletion' AND sa.token_used=0"
        ).fetchall()

    # Check if there are recommendations to include even without scheduled items
    has_recommendations = False
    rec_check = conn.execute("SELECT COUNT(*) AS n FROM suggestions").fetchone()
    rec_enabled_check = conn.execute(
        "SELECT value FROM settings WHERE key='suggestions_enabled'"
    ).fetchone()
    if (not rec_enabled_check or rec_enabled_check["value"] != "false") and rec_check and rec_check["n"] > 0:
        has_recommendations = True

    if not scheduled_rows and not has_recommendations:
        logger.debug("Newsletter skipped — nothing to report")
        return

    now = datetime.now(timezone.utc)

    scheduled_items = []
    for row in scheduled_rows:
        added_at_raw = row["added_at"]
        try:
            added_dt = datetime.fromisoformat(str(added_at_raw))
            added_dt = _ensure_tz(added_dt)
            added_days_ago = (now - added_dt).days
        except Exception:
            added_days_ago = None

        rating_key = row["plex_rating_key"] or ""
        poster_url = (
            f"{base_url}{sign_poster_url(rating_key, secret_key)}"
            if rating_key and base_url
            else ""
        )

        # Format last watched info from DB
        last_watched_info = None
        lw_raw = row["last_watched_at"]
        if lw_raw:
            try:
                lw_dt = datetime.fromisoformat(str(lw_raw))
                lw_dt = _ensure_tz(lw_dt)
                lw_days = (now - lw_dt).days
                if lw_days == 0:
                    last_watched_info = "Watched today"
                elif lw_days == 1:
                    last_watched_info = "Watched yesterday"
                else:
                    last_watched_info = f"Watched {lw_days} days ago"
            except (ValueError, TypeError):
                pass

        # Build human-readable type label with season number
        media_type = row["media_type"] or "movie"
        season_num = row["season_number"]
        if media_type in ("tv_season", "season", "tv"):
            type_label = f"TV · Season {season_num}" if season_num else "TV"
        elif media_type in ("anime_season", "anime"):
            type_label = f"Anime · Season {season_num}" if season_num else "Anime"
        else:
            type_label = "Movie"

        scheduled_items.append({
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
    scheduled_items.sort(key=lambda x: x.get("added_days_ago") or 0, reverse=True)

    # ── Recently deleted items ───────────────────────────────────────────────
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

    # Fetch re-download timestamps to filter out re-downloaded items
    redownload_rows = conn.execute(
        "SELECT media_item_id, created_at FROM audit_log "
        "WHERE action IN ('re_downloaded', 'downloaded')"
    ).fetchall()
    redownload_times: dict[str, str] = {}
    for rd in redownload_rows:
        key = rd["media_item_id"].lower()
        if key not in redownload_times or rd["created_at"] > redownload_times[key]:
            redownload_times[key] = rd["created_at"]

    deleted_items = []
    for row in deleted_rows:
        title = row["title"] or _extract_title_from_detail(row["detail"])

        # Skip if re-downloaded after this deletion
        last_redownload = redownload_times.get(title.lower())
        if last_redownload and last_redownload > row["created_at"]:
            continue

        try:
            del_dt = datetime.fromisoformat(str(row["created_at"]))
            del_dt = _ensure_tz(del_dt)
            days_ago = (now - del_dt).days
            if days_ago == 0:
                deleted_date = "today"
            elif days_ago == 1:
                deleted_date = "yesterday"
            else:
                deleted_date = f"{days_ago} days ago"
        except Exception:
            deleted_date = ""

        rating_key = row["plex_rating_key"] or _extract_rk_from_detail(row["detail"]) or ""
        poster_url = (
            f"{base_url}{sign_poster_url(rating_key, secret_key)}"
            if rating_key and base_url
            else ""
        )

        deleted_items.append({
            "title": title,
            "poster_url": poster_url,
            "deleted_date": deleted_date,
            "file_size_bytes": row["space_reclaimed_bytes"] or 0,
            "media_type": row["media_type"] or "movie",
        })

    # ── Recommendations (most recent batch if enabled) ────────────────────
    rec_enabled_row = conn.execute(
        "SELECT value FROM settings WHERE key='suggestions_enabled'"
    ).fetchone()
    this_week_items = []
    if not rec_enabled_row or rec_enabled_row["value"] != "false":
        batch_row = conn.execute(
            "SELECT DISTINCT batch_id FROM suggestions WHERE batch_id IS NOT NULL "
            "ORDER BY batch_id DESC LIMIT 1"
        ).fetchone()
        if batch_row:
            rows = conn.execute(
                "SELECT id, title, media_type, category, description, reason, "
                "poster_url, tmdb_id, rating, rt_rating "
                "FROM suggestions WHERE batch_id = ? ORDER BY category DESC, id",
                (batch_row["batch_id"],)
            ).fetchall()
            this_week_items = [dict(r) for r in rows]

    # ── Storage stats ────────────────────────────────────────────────────────
    # Aggregate file_size_bytes per media_type from media_items, normalised
    # to the keys the email template expects (movie, show, anime).
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

    # Real disk usage across all mount points under /media
    from mediaman.services.storage import get_aggregate_disk_usage
    total_bytes = used_bytes
    free_bytes = 0
    try:
        disk = get_aggregate_disk_usage("/media")
        total_bytes = disk["total_bytes"]
        used_bytes = disk["used_bytes"]
        free_bytes = disk["free_bytes"]
    except Exception:
        pass

    storage = {
        "total_bytes": total_bytes,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "by_type": by_type,
    }

    # ── Space reclaimed stats ────────────────────────────────────────────────
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

    # ── Render template ──────────────────────────────────────────────────────
    template_dir = Path(__file__).parent.parent / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("email/newsletter.html")

    report_date = now.strftime("%-d %B %Y")

    # ── Send per-recipient (each gets a unique unsubscribe link) ───────────
    from mediaman.crypto import generate_download_token
    from mediaman.web.routes.subscribers import generate_unsubscribe_token

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
    if dry_run:
        subject = f"[DRY RUN] {subject}"

    dashboard_url = base_url or "http://mediaman"
    mailgun = MailgunClient(domain, api_key, from_address)

    # ── Mark download state on recommendation items ─────────────────────────
    # Populates ``item["download_state"]`` (``in_library`` / ``partial`` /
    # ``downloading`` / ``queued``) by consulting Radarr and Sonarr.
    rec_items = this_week_items
    if rec_items:
        radarr_client = build_radarr_from_db(conn, secret_key)
        sonarr_client = build_sonarr_from_db(conn, secret_key)
        try:
            radarr_cache = build_radarr_cache(radarr_client)
        except Exception:
            radarr_cache = build_radarr_cache(None)
        try:
            sonarr_cache = build_sonarr_cache(sonarr_client)
        except Exception:
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

    successfully_sent: list[str] = []
    for email in recipient_emails:
        unsub_token = generate_unsubscribe_token(email, secret_key)
        # URL-encode both values so a mail address containing ``&``,
        # ``+``, ``#`` (all RFC-legal in local-parts) doesn't shred the
        # query string and confuse the unsubscribe handler.
        from urllib.parse import quote as _url_quote
        unsub_url = (
            f"{base_url}/unsubscribe?email={_url_quote(email, safe='@')}"
            f"&token={_url_quote(unsub_token, safe='')}"
        ) if base_url else ""

        # Generate per-recipient download tokens for deleted items
        for item in deleted_items:
            item["redownload_url"] = "{}/download/{}".format(
                base_url,
                generate_download_token(
                    email=email,
                    action="redownload",
                    title=item["title"],
                    media_type=item.get("media_type", "movie"),
                    tmdb_id=None,
                    recommendation_id=None,
                    secret_key=secret_key,
                ),
            ) if base_url else ""

        # Generate per-recipient download tokens for this week's recommendations
        for item in this_week_items:
            item["download_url"] = "{}/download/{}".format(
                base_url,
                generate_download_token(
                    email=email,
                    action="download",
                    title=item["title"],
                    media_type=item["media_type"],
                    tmdb_id=item.get("tmdb_id"),
                    recommendation_id=item.get("id"),
                    secret_key=secret_key,
                ),
            ) if base_url else ""

        html = template.render(
            report_date=report_date,
            storage=storage,
            reclaimed_week=reclaimed_week,
            reclaimed_month=reclaimed_month,
            reclaimed_total=reclaimed_total,
            scheduled_items=scheduled_items,
            deleted_items=deleted_items,
            this_week_items=this_week_items,
            dashboard_url=dashboard_url,
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

    # ── Mark as notified (only for automated sends, only if anything got out) ──
    if mark_notified and successfully_sent:
        action_ids = [item["_action_id"] for item in scheduled_items]
        if action_ids:
            placeholders = ",".join("?" * len(action_ids))
            conn.execute(
                f"UPDATE scheduled_actions SET notified=1 WHERE id IN ({placeholders})",
                action_ids,
            )
            conn.commit()

    logger.info(
        "Newsletter sent to %d/%d subscriber(s) — %d scheduled, %d deleted",
        len(successfully_sent),
        len(recipient_emails),
        len(scheduled_items),
        len(deleted_items),
    )
