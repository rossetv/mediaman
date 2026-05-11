"""Unified Sonarr/Radarr client — shared HTTP helpers, state management, and search-trigger throttle.

Sub-packages: ``fetcher`` (queue fetch + card formatting), ``completion``
(download verification and recent-downloads logging), ``search_trigger``
(throttled search dispatch), ``build`` (client construction from DB settings).

Allowed dependencies: ``mediaman.services.infra.http``, ``mediaman.crypto``,
``mediaman.db``; the ``ArrError`` / ``ArrConfigError`` / ``ArrKindMismatch``
exceptions are re-exported here as the public error surface.

Forbidden patterns: do not import from ``mediaman.web`` or ``mediaman.scanner``
— this package is consumed by both and must sit below them in the dependency
graph.
"""

from mediaman.services.arr._transport import ArrConfigError, ArrError, ArrKindMismatch

__all__ = ["ArrConfigError", "ArrError", "ArrKindMismatch"]
