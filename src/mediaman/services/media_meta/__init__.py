"""Metadata clients for Plex, TMDB, and OMDb — library data enrichment and external lookups.

Sub-modules: ``plex`` (PlexClient, library and watch-history fetching),
``_plex_session`` (per-request session helpers), ``tmdb`` (TmdbClient, poster
and movie lookups), ``omdb`` (OmdbClient, additional movie metadata).

Allowed dependencies: ``mediaman.services.infra.http`` for outbound HTTP;
``mediaman.crypto`` for stored-token decryption; ``mediaman.db`` for settings
reads.

Forbidden patterns: do not import from ``mediaman.web`` or ``mediaman.scanner``
— metadata clients are shared across the scanner pipeline and web routes.
"""
