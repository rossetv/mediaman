"""Re-export shim for :mod:`mediaman.services.openai_recommendations`.

All implementation has moved to:

- :mod:`mediaman.services.openai_client` — shared HTTP client and low-level
  ``call_openai`` helper.
- :mod:`mediaman.services.recommendations` — prompt construction, enrichment,
  and persistence.

This module re-exports every previously-public symbol so existing imports
continue to work without modification.
"""

from __future__ import annotations

from mediaman.services.openai_client import (
    _DEFAULT_MODEL,
    _OPENAI_CLIENT,
)
from mediaman.services.openai_client import (
    call_openai as _call_openai,
)
from mediaman.services.openai_client import (
    get_openai_key as _get_openai_key,
)
from mediaman.services.openai_client import (
    get_openai_model as _get_openai_model,
)
from mediaman.services.openai_client import (
    is_web_search_enabled as _is_web_search_enabled,
)
from mediaman.services.openai_client import (
    validate_web_search_title as _validate_web_search_title,
)
from mediaman.services.recommendations.enrich import (
    enrich_recommendations as _enrich_recommendations,
)
from mediaman.services.recommendations.persist import refresh_recommendations
from mediaman.services.recommendations.prompts import (
    _PLEX_BLOCK_MAX_BYTES,
    _PLEX_STRING_MAX_LEN,
    _RESPONSE_FORMAT,
)
from mediaman.services.recommendations.prompts import (
    generate_personal as _generate_personal,
)
from mediaman.services.recommendations.prompts import (
    generate_trending as _generate_trending,
)
from mediaman.services.recommendations.prompts import (
    parse_recommendations as _parse_recommendations,
)
from mediaman.services.recommendations.prompts import (
    sanitise_plex_string as _sanitise_plex_string,
)
from mediaman.services.recommendations.prompts import (
    strip_season_suffix as _strip_season_suffix,
)

__all__ = [
    "refresh_recommendations",
    "_DEFAULT_MODEL",
    "_OPENAI_CLIENT",
    "_call_openai",
    "_get_openai_key",
    "_get_openai_model",
    "_is_web_search_enabled",
    "_validate_web_search_title",
    "_enrich_recommendations",
    "_sanitise_plex_string",
    "_strip_season_suffix",
    "_generate_personal",
    "_generate_trending",
    "_parse_recommendations",
    "_PLEX_BLOCK_MAX_BYTES",
    "_PLEX_STRING_MAX_LEN",
    "_RESPONSE_FORMAT",
]
