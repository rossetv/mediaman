"""recommendations package — OpenAI-powered media recommendations.

What: assembles personalised prompts from a user's Plex watch history and
ratings, calls the OpenAI chat API via :mod:`mediaman.services.openai.client`,
validates the LLM response, persists accepted suggestions, and enforces a
per-day refresh cooldown.

Depends on: ``mediaman.services.openai.client`` (HTTP wrapper),
``mediaman.services.media_meta`` (Plex / TMDB lookups),
``mediaman.db`` (settings and suggestions table), ``mediaman.crypto``
(secret decryption).

Forbidden: do not let raw LLM string output escape this package without
passing through the ``_validate_llm_string`` guard in
:mod:`~mediaman.services.openai.recommendations.prompts` — prompt-injection
defence lives there.
"""

from __future__ import annotations

from mediaman.services.openai.recommendations.persist import refresh_recommendations

__all__ = ["refresh_recommendations"]
