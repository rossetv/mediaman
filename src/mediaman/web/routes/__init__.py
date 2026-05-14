"""FastAPI route modules — authentication, dashboard, downloads, library browsing, and settings.

Each sub-package owns one feature area: ``dashboard`` (stats + deletion list),
``download`` (confirm + submit + status), ``library_api`` (keep/delete/redownload
JSON API), ``search`` (Arr queue + detail + download trigger), ``settings``
(configuration UI and API), ``recommended`` (OpenAI pick pages and refresh),
``poster`` (SSRF-safe proxy and cache).

Allowed dependencies: ``mediaman.web.auth``, ``mediaman.web.middleware``,
``mediaman.services.*``, ``mediaman.db``, ``mediaman.crypto``.

Forbidden patterns: do not add cross-route imports — each sub-package should
be independently mountable; shared helpers belong in ``mediaman.web.cookies``
(session-cookie utilities), ``mediaman.web.auth.middleware`` (auth predicates),
or the relevant service module.
"""

from __future__ import annotations
