"""Scan-phase sub-modules for the mediaman scanner engine.

Each module implements one discrete phase of the scan pipeline:

* :mod:`fetch`    — pull items + watch history from Plex (network only, no DB).
* :mod:`upsert`   — write fetched items into ``media_items`` (DB only, no network).
* :mod:`evaluate` — decide whether each item is eligible for deletion.
* :mod:`delete`   — remove orphaned ``media_items`` rows whose Plex rating key
                    no longer exists.

The engine imports from these modules and remains a thin orchestrator.
"""

from __future__ import annotations
