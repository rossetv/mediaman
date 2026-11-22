"""Shared redownload helper: audit-ID selection.

Used by both the Radarr and Sonarr branches, so it lives in a dependency-free module
to avoid an import cycle with .redownload.
"""

from __future__ import annotations

from mediaman.services.infra import SafeHTTPError


class AddRefused(Exception):
    """An Arr add call failed; wraps the HTTP error so only adds can read as duplicates."""

    def __init__(self, cause: SafeHTTPError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _redownload_audit_id(
    *,
    media_type: str,
    tmdb_id: int | None,
    tvdb_id: int | None,
) -> str:
    """Pick a stable audit-log media_item_id for a redownload.

    Prefers tmdb:<id> (universal across both Arrs), then tvdb:<id>. The
    prefix makes the column self-describing so the dashboard cannot match an opaque integer
    against unrelated UUIDs.
    """
    if tmdb_id is not None:
        return f"tmdb:{tmdb_id}"
    if tvdb_id is not None:
        return f"tvdb:{tvdb_id}"
    # Should never happen — caller guarantees at least one stable id is
    # present before we reach here.
    return f"redownload:{media_type}"
