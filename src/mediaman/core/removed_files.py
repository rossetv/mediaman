"""The SQL condition for a deletion that has already removed every file it had to."""

from __future__ import annotations

_RECORD = (
    "CASE WHEN json_valid(sa.removed_files) THEN "
    "  CASE WHEN json_type(sa.removed_files, '$.paths') = 'array'"
    "    AND json_type(sa.removed_files, '$.bytes') IN ('integer', 'true', 'false') "
    "  THEN sa.removed_files ELSE '{}' END "
    "ELSE '{}' END"
)
_REMOVED = f"SELECT r.value FROM json_each({_RECORD}, '$.paths') r WHERE r.type = 'text'"
_TRACKED = (
    "CASE WHEN json_valid(mi.tracked_files) THEN "
    "  CASE json_type(mi.tracked_files) WHEN 'array' THEN mi.tracked_files ELSE '[]' END "
    "ELSE '[]' END"
)
# Every tracked file has a path; with none tracked any more, what the last pass left decides.
_IS_TRACKED = (
    f"(json_array_length({_TRACKED}) > 0 AND NOT EXISTS ("
    f"  SELECT 1 FROM json_each({_TRACKED}) e WHERE "
    "    CASE WHEN e.type = 'object' AND json_type(e.value, '$.path') = 'text' "
    "    THEN json_extract(e.value, '$.path') = '' ELSE 1 END))"
)
_PATH = "CASE WHEN e.type = 'object' THEN json_extract(e.value, '$.path') END"
# A record holds its paths without doubled or trailing slashes.
_PLAIN_PATH = f"rtrim(replace(replace(replace({_PATH}, '//', '/'), '//', '/'), '/./', '/'), '/')"

# True while the record of ``sa`` (``scheduled_actions``) holds every file of ``mi``
# (``media_items``): the rule of is_fully_removed, for a query to ask.
FILES_GONE = (
    "(sa.removed_files IS NOT NULL "
    f"AND EXISTS ({_REMOVED}) "
    f"AND NOT EXISTS (SELECT 1 FROM json_each({_TRACKED}) e "
    f"  WHERE {_IS_TRACKED} AND {_PLAIN_PATH} NOT IN ({_REMOVED})) "
    f"AND NOT EXISTS (SELECT 1 FROM json_each({_RECORD}, '$.left') l "
    f"  WHERE NOT {_IS_TRACKED} AND json_type({_RECORD}, '$.left') = 'array' "
    f"  AND l.type = 'text' AND l.value NOT IN ({_REMOVED})))"
)
