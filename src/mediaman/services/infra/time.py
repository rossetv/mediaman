"""Back-compat shim — canonical module relocated to mediaman.core.time.

New code should import from ``mediaman.core.time`` directly.
"""

from mediaman.core.time import (  # noqa: F401
    now_iso,
    parse_iso_utc,
)
