"""Fetch phase — pull Plex library items and watch history into memory.

This module is a thin re-export shim so the engine can import the fetch
primitives from the phases package rather than directly from
:mod:`mediaman.scanner.fetch`.  No logic lives here; :class:`PlexFetcher`
and :class:`_PlexItemFetch` remain in their canonical location so that
existing callers (tests, back-compat shims) continue to work without
modification.

Public surface
--------------
* :class:`PlexFetcher` — fetches library items from a Plex client.
* :class:`FetchedItem` — alias for :class:`_PlexItemFetch` used throughout
  the phases package.
"""

from __future__ import annotations

from mediaman.scanner.fetch import PlexFetcher, _PlexItemFetch

# Canonical alias used within the phases package.  The leading underscore
# on the original is a convention from before the package existed; we
# expose the type under a cleaner name for new code while keeping the
# original importable for back-compat.
FetchedItem = _PlexItemFetch

__all__ = ["FetchedItem", "PlexFetcher", "_PlexItemFetch"]
