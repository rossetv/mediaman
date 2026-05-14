"""Outbound service clients for Plex, Sonarr/Radarr, NZBGet, TMDB, OMDb, Mailgun, and OpenAI.

Each sub-package owns one external integration: ``arr`` (Radarr/Sonarr queue
and search), ``downloads`` (NZBGet tracking), ``media_meta`` (Plex, TMDB,
OMDb), ``mail`` (Mailgun + newsletter), ``openai`` (recommendation prompts),
``infra`` (HTTP client, path safety, storage).

Allowed dependencies: ``mediaman.crypto`` (for secret decryption), ``mediaman.db``
(for settings reads), and Python standard library.

Forbidden patterns: do not import from ``mediaman.web`` — services must remain
usable outside the HTTP request context (background jobs, CLI scripts).
"""

from __future__ import annotations
