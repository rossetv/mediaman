"""Back-compat shim — canonical module relocated to mediaman.core.format.

New code should import from ``mediaman.core.format`` directly.
``parse_iso_utc`` is re-exported here because it was previously importable
from this module and callers may depend on that path.
"""

from mediaman.core.format import (  # noqa: F401
    _AUDIT_RK_RE,
    _AUDIT_TITLE_MAX_INPUT,
    _AUDIT_TITLE_RE,
    _ENGLISH_MONTH_ABBR,
    _ENGLISH_MONTH_FULL,
    days_ago,
    ensure_tz,
    format_bytes,
    format_day_month,
    media_type_badge,
    normalise_media_type,
    rk_from_audit_detail,
    safe_json_list,
    title_from_audit_detail,
)
from mediaman.core.time import parse_iso_utc as parse_iso_utc
