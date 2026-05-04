"""Spec-driven configuration for *arr API clients.

``ArrSpec`` is a frozen dataclass that captures every value that differs
between Sonarr and Radarr.  Passing a spec to :class:`ArrClient` in
``base.py`` replaces the two parallel subclass hierarchies with one
class whose behaviour is driven entirely by the spec instance.

Module-level constants :data:`SONARR_SPEC` and :data:`RADARR_SPEC` are
the canonical instances.  All callers should import these rather than
constructing their own specs, so endpoint strings live in one file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ArrSpec:
    """Immutable configuration describing one *arr service variant.

    :param kind: Either ``"series"`` (Sonarr) or ``"movie"`` (Radarr).
        Used by :class:`ArrClient` to guard kind-specific methods.
    :param list_endpoint: Path to GET the full library list,
        e.g. ``"/api/v3/series"`` or ``"/api/v3/movie"``.
    :param item_endpoint_template: Path template for a single item,
        e.g. ``"/api/v3/series/{id}"`` or ``"/api/v3/movie/{id}"``.
    :param monitored_field: The JSON field name for the monitored flag.
        Always ``"monitored"`` in current Sonarr/Radarr v3 APIs, but
        pinning it here keeps future changes to one location.
    :param exclusion_param: Query-string parameter appended to DELETE
        calls to add the item to the import exclusion list.
        Sonarr uses ``"addImportListExclusion"``; Radarr uses
        ``"addImportExclusion"``.
    :param label: Human-readable service name used in log messages and
        UI strings, e.g. ``"Sonarr"`` or ``"Radarr"``.
    """

    kind: Literal["movie", "series"]
    list_endpoint: str
    item_endpoint_template: str
    monitored_field: str
    exclusion_param: str
    label: str


#: Spec for Sonarr v3 — series / episode management.
SONARR_SPEC = ArrSpec(
    kind="series",
    list_endpoint="/api/v3/series",
    item_endpoint_template="/api/v3/series/{id}",
    monitored_field="monitored",
    exclusion_param="addImportListExclusion",
    label="Sonarr",
)

#: Spec for Radarr v3 — movie management.
RADARR_SPEC = ArrSpec(
    kind="movie",
    list_endpoint="/api/v3/movie",
    item_endpoint_template="/api/v3/movie/{id}",
    monitored_field="monitored",
    exclusion_param="addImportExclusion",
    label="Radarr",
)
