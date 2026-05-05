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
    :param exclusion_param: Query-string parameter appended to DELETE
        calls to add the item to the import exclusion list.
        Sonarr uses ``"addImportListExclusion"``; Radarr uses
        ``"addImportExclusion"``.
    """

    kind: Literal["movie", "series"]
    exclusion_param: str


#: Spec for Sonarr v3 — series / episode management.
SONARR_SPEC = ArrSpec(
    kind="series",
    exclusion_param="addImportListExclusion",
)

#: Spec for Radarr v3 — movie management.
RADARR_SPEC = ArrSpec(
    kind="movie",
    exclusion_param="addImportExclusion",
)
