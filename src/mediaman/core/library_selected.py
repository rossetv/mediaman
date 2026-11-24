"""The SQL condition for an item whose Plex library is ticked in Settings."""

from __future__ import annotations

# True while the Plex library of ``mi`` (``media_items``) is ticked in Settings. Nothing is
# ticked when the setting is not saved yet or cannot be read.
LIBRARY_SELECTED = (
    "CAST(mi.plex_library_id AS TEXT) IN ("
    "  SELECT CAST(lib.value AS TEXT) FROM settings s, json_each("
    "    CASE WHEN json_valid(s.value) THEN "
    "      CASE json_type(s.value) WHEN 'array' THEN s.value ELSE '[]' END "
    "    ELSE '[]' END) lib "
    "  WHERE s.key = 'plex_libraries')"
)
