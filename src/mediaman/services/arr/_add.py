"""Add-flow helpers (root folder + quality profile pickers) for *arr clients.

Both Radarr and Sonarr require the caller to nominate a root folder and
a quality profile when adding a new release.  Three near-identical
copies of "GET /rootfolder, take [0], else fall back to '/tv' or
'/movies'" used to live in the per-client modules; the same was true of
the hardcoded ``quality_profile_id=4`` default.  The helpers below
replace those copies so every add path uses the same logic and the same
set of error messages.

The picked values are cached on the instance because every add-flow
touches them and the underlying lists barely change at runtime.
"""

from __future__ import annotations

from typing import cast

from mediaman.services.arr._transport import ArrConfigError
from mediaman.services.arr._types import ArrQualityProfile, ArrRootFolder


class _AddFlowMixin:
    """Cached pickers for the root folder + quality profile.

    Mixed into :class:`~mediaman.services.arr.base.ArrClient`.  The
    caches live on the instance (not class) so two clients pointing at
    different *arr instances cannot leak settings across each other.
    """

    _root_folder_cache: str | None = None
    _quality_profile_cache: int | None = None

    def _choose_root_folder(self) -> str:
        """Return the path of the first configured root folder.

        Cached on the client instance so a burst of adds in a single
        process pays one API call.  Raises :exc:`ArrConfigError` when
        the Arr service has no root folders configured — the previous
        default of ``"/tv"`` / ``"/movies"`` paved over a common
        misconfiguration and led to silent failures downstream.
        """
        if self._root_folder_cache is not None:
            return self._root_folder_cache
        result = self._get("/api/v3/rootfolder")  # type: ignore[attr-defined]
        root_folders = cast(list[ArrRootFolder], result) if isinstance(result, list) else []
        if not root_folders:
            raise ArrConfigError(
                f"{type(self).__name__}: no root folders configured — "
                "set one in the service's UI before adding releases"
            )
        path = root_folders[0].get("path")
        if not isinstance(path, str) or not path:
            raise ArrConfigError(
                f"{type(self).__name__}: first root folder has no 'path' — "
                "the service's response is malformed"
            )
        self._root_folder_cache = path
        return path

    def _choose_quality_profile(self) -> int:
        """Return the id of the lowest-numbered quality profile.

        Used by the add-flow when the caller doesn't pin a specific
        profile.  Cached on the instance.  Raises :exc:`ArrConfigError`
        when no quality profiles are configured (which would otherwise
        have silently picked id ``4`` whether such a profile existed).
        """
        if self._quality_profile_cache is not None:
            return self._quality_profile_cache
        result = self._get("/api/v3/qualityprofile")  # type: ignore[attr-defined]
        profiles = cast(list[ArrQualityProfile], result) if isinstance(result, list) else []
        ids = [int(p["id"]) for p in profiles if isinstance(p.get("id"), int)]
        if not ids:
            raise ArrConfigError(
                f"{type(self).__name__}: no quality profiles configured — "
                "set one in the service's UI before adding releases"
            )
        chosen = min(ids)
        self._quality_profile_cache = chosen
        return chosen
