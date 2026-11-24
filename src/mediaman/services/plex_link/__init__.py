"""Linking a person's own Plex account, and reading that account's plays.

A person proves the account is theirs by signing in at Plex. The sign-in token is used to learn
who they are, revoked, and never stored, logged or sent to the browser. Their plays feed their
own recommendations only: positions, watched marks and cleanup never read them.

Allowed dependencies: ``mediaman.core``, ``mediaman.services.infra``,
``mediaman.services.media_meta``, ``mediaman.services.rate_limit``.

Forbidden patterns: do not import from ``mediaman.web`` or ``mediaman.scanner``.
"""
