"""NZBGet integration — download queue tracking, abandon logic, and notifications.

Sub-modules: ``nzbget`` (queue fetch and history), ``notifications``
(completion event dispatch), ``download_format`` (queue-item classification and
format helpers shared with the download-status and submit routes).

Allowed dependencies: ``mediaman.services.infra.http``, ``mediaman.crypto``,
``mediaman.db``, and ``mediaman.services.arr``.

Forbidden patterns: do not import from ``mediaman.web`` — this package is
consumed by background jobs and must remain independent of the HTTP layer.
"""
