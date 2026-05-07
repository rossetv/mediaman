"""Plex library scanner — detects new media, evaluates deletion eligibility, and schedules actions.

Orchestrates the full scan pipeline: ``engine`` drives per-library fetch →
upsert → evaluate → schedule; ``runner`` wraps that in a Plex-client cache and
library-sync helpers; ``scheduler`` registers the weekly APScheduler job.

Allowed dependencies: ``mediaman.db``, ``mediaman.services.media_meta``,
``mediaman.services.arr``, ``mediaman.crypto``, and ``mediaman.scanner.repository``.

Forbidden patterns: do not import ``mediaman.web`` from within this package —
the scanner is a background service and must remain independent of the HTTP
layer.
"""
