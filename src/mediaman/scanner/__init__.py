"""Plex library scanner — detects new media, evaluates deletion eligibility, and schedules actions.

Orchestrates the full scan pipeline: ``engine`` drives per-library fetch →
upsert → evaluate → schedule; ``runner`` wraps that in a Plex-client cache and
library-sync helpers; ``scheduler`` registers the weekly APScheduler job.

Allowed dependencies: ``mediaman.db``, ``mediaman.services.media_meta``,
``mediaman.services.arr``, ``mediaman.crypto``, ``mediaman.scanner.repository``,
``mediaman.services.mail``, and ``mediaman.services.openai.recommendations``.

Forbidden patterns: do not import ``mediaman.web`` from within this package —
the scanner is a background service and must remain independent of the HTTP
layer.
"""

from __future__ import annotations
