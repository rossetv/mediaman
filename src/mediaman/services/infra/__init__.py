"""Shared infrastructure — SSRF-safe HTTP client, rate-limit infrastructure, path safety, and storage.

Sub-packages: ``http`` (DNS-pinning, retry, streaming, SafeHTTPClient),
``storage`` (_safe_rmtree TOCTOU-hardened deletion), ``path_safety``
(allowlist parsing and containment checks), ``settings_reader``
(settings-row fetch + decryption helpers), ``url_safety`` (outbound URL
validation guard).

Allowed dependencies: Python standard library, ``mediaman.crypto``; must NOT
import from ``mediaman.web``, ``mediaman.scanner``, or ``mediaman.services.arr``.

Forbidden patterns: do not add business logic here — this package supplies
primitives that every other service package depends on.

Public surface
--------------
§1.7 says the package ``__init__.py`` is the public surface.  All callers
**must** import from ``mediaman.services.infra`` directly:

    >>> from mediaman.services.infra import get_string_setting, SafeHTTPError

Reaching past this surface into sub-modules
(``mediaman.services.infra.settings_reader``, ``mediaman.services.infra.http``,
etc.) couples callers to implementation detail and is a code-review blocker.
The only exception is a site with an explicit ``# rationale:`` comment
explaining why the public surface cannot satisfy the requirement.
"""

from mediaman.services.infra.http import (
    SafeHTTPClient,
    SafeHTTPError,
)
from mediaman.services.infra.path_safety import (
    disk_usage_allowed_roots,
    parse_delete_roots_env,
    resolve_safe_path,
)
from mediaman.services.infra.settings_reader import (
    ConfigDecryptError,
    get_bool_setting,
    get_int_setting,
    get_media_path,
    get_setting,
    get_string_setting,
)
from mediaman.services.infra.storage import (
    DeletionRefused,
    delete_path,
    get_aggregate_disk_usage,
    get_directory_size,
)
from mediaman.services.infra.url_safety import (
    PINNED_EXTERNAL_HOSTS,
    SSRFRefused,
    allowed_outbound_hosts,
    is_safe_outbound_url,
    resolve_safe_outbound_url,
)

__all__ = [
    "PINNED_EXTERNAL_HOSTS",
    "ConfigDecryptError",
    "DeletionRefused",
    "SSRFRefused",
    "SafeHTTPClient",
    "SafeHTTPError",
    "allowed_outbound_hosts",
    "delete_path",
    "disk_usage_allowed_roots",
    "get_aggregate_disk_usage",
    "get_bool_setting",
    "get_directory_size",
    "get_int_setting",
    "get_media_path",
    "get_setting",
    "get_string_setting",
    "is_safe_outbound_url",
    "parse_delete_roots_env",
    "resolve_safe_outbound_url",
    "resolve_safe_path",
]
