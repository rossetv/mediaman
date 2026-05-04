"""Back-compat shim — canonical module relocated to mediaman.core.backoff.

New code should import from ``mediaman.core.backoff`` directly.
"""

from mediaman.core.backoff import ExponentialBackoff  # noqa: F401
