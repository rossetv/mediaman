"""Post-scan follow-up tasks: recommendation refresh and newsletter dispatch.

Split from :mod:`mediaman.scanner.engine` so the orchestrator stays
focused on the per-library iteration loop.  The helpers below run after
every scan completes (unless ``dry_run`` is set) and never touch the
scan summary counters — they observe the scan and report side-effects
out of band (DB writes, outbound email).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mediaman.services.media_meta.plex import PlexClient

from mediaman.services.infra.settings_reader import get_bool_setting as _get_bool_setting
from mediaman.services.mail.newsletter import send_newsletter as _send_newsletter
from mediaman.services.openai.recommendations.persist import (
    refresh_recommendations as _refresh_recommendations,
)

logger = logging.getLogger(__name__)


def run_post_scan_followups(
    *,
    conn: sqlite3.Connection,
    plex_client: PlexClient,
    secret_key: str,
    dry_run: bool,
    grace_days: int,
) -> None:
    """Refresh AI recommendations and send the deletion-warning newsletter.

    Recommendations refresh runs FIRST so the newsletter reflects this
    week's picks rather than last week's stale batch — the cards are
    loaded from the ``suggestions`` table that
    :func:`_refresh_recommendations` rewrites.  Both calls are wrapped
    in exception handlers because either failing should not abort a
    successful scan.  Skipped entirely in dry-run mode.
    """
    if dry_run:
        logger.info("engine.run_scan.dry_run skipping newsletter + recommendations refresh")
        return
    if _get_bool_setting(conn, "suggestions_enabled", default=True):
        try:
            _refresh_recommendations(conn, plex_client, secret_key=secret_key)
        # rationale: §6.4 site 2 (scheduler-job-runner) — post-scan side
        # effect; never allow a recommendations bug to mark the scan failed.
        except Exception:
            logger.exception("Recommendation generation failed — scan results unaffected")
    try:
        _send_newsletter(
            conn=conn,
            secret_key=secret_key,
            dry_run=dry_run,
            grace_days=grace_days,
        )
    # rationale: §6.4 site 2 (scheduler-job-runner) — post-scan side effect;
    # mailgun/SMTP transient outages must not cascade into a failed scan.
    except Exception:
        logger.exception("Newsletter sending failed — scan results unaffected")
