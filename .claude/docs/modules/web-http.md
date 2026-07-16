<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: web-http

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

The HTTP surface of mediaman: every FastAPI route handler plus three cross-cutting
primitives — the canonical JSON envelope (`responses.py`), the session-cookie helpers
with the `Secure`-flag resolver (`cookies.py`), and the security-middleware orchestrator
(`web/__init__.py`). The layer is deliberately thin: handlers parse/validate, enforce
auth + rate limits, then delegate business logic to `mediaman.services.*`, persistence
to `mediaman.web.repository.*`, auth predicates to `mediaman.web.auth.*`, and token
signing to `mediaman.crypto`.

## Key files

| File | Role |
|------|------|
| `src/mediaman/web/__init__.py` | Security-middleware orchestrator. `register_security_middleware()` adds the stack LIFO; `_parse_allowed_hosts()` reads `MEDIAMAN_ALLOWED_HOSTS` and warns when it resolves to `["*"]`. |
| `src/mediaman/web/responses.py` | Single source of truth for the JSON envelope. `respond_ok(data, *, status)` → `{ok: true, **data}`; `respond_err(error, *, status, message, **extra)` → `{ok: false, error, …}`. |
| `src/mediaman/web/cookies.py` | Session-cookie primitives. `SESSION_COOKIE_MAX_AGE` (86400), `set_session_cookie()` (httponly, `samesite=strict`), and the `is_request_secure()` resolver — sole owner of the `Secure`-flag decision. |
| `src/mediaman/web/routes/__init__.py` | Routes-package doc: declares allowed dependencies and the "no cross-route imports" rule; sets the sub-package = one feature area convention. |
| `src/mediaman/web/routes/auth.py` | `GET`/`POST /login`, `POST /api/auth/logout`. Per-IP login `RateLimiter(max_attempts=5, window_seconds=300)`; sanitises attacker-controlled username before audit/log; issues/clears the session cookie. |
| `src/mediaman/web/routes/poster/` | SSRF-safe Plex/Arr poster proxy with on-disk cache. `__init__.py` = route (`/api/poster/{rating_key}`) + auth gate; `fetch.py` = SSRF guards + `SafeHTTPClient` allowlist; `cache.py` = atomic write + LRU sweep + `.mime` sidecar. |
| `src/mediaman/web/routes/download/` | Public email-token download flow. `confirm.py` (`GET /download/{token}`), `submit.py` (`POST /download/{token}`), `_tokens.py` (single-use token store), `status/` (`GET /api/download/status`). |
| `src/mediaman/web/routes/settings/` | Settings UI + JSON API. `api.py` (`GET`/`PUT /api/settings`, `test/{service}`, plex libraries, disk-usage); `secrets.py` owns `SECRET_FIELDS`/`SENSITIVE_KEYS`/`ALL_KEYS` + masking; `core.py`/`testers.py` support them. |
| `src/mediaman/web/routes/users/` | User management + reauth. `crud.py`, `passwords.py`, `sessions.py`, `reauth.py` (`POST /api/auth/reauth`), `rate_limits.py` (shared per-actor `ActionRateLimiter` instances). |
| `src/mediaman/web/routes/library_api/` | Keep/delete/redownload JSON API. `__init__.py` composes `_redownload_router` + `_delete_router`; `_redownload_match`/`_radarr`/`_sonarr` resolve the Arr record to re-grab. |
| `src/mediaman/web/routes/search/` | Arr search UI + API. `page.py` (`/search`, `/api/search`, `/api/search/discover`), `detail.py`, `download.py` (`POST /api/search/download`), `_enrichment.py` annotates TMDB results. |
| `src/mediaman/web/routes/recommended/` | OpenAI recommendation pages + API. `pages.py` (`GET /recommended`), `api.py` (list, per-item download, share-token), `refresh.py` (`POST /api/recommended/refresh` + status). |
| `src/mediaman/web/routes/force_password_change.py` | `GET`/`POST /force-password-change` — funnel for admins flagged must-change; dual per-actor + per-IP `ActionRateLimiter`. |
| `src/mediaman/web/routes/dashboard/__init__.py` | Dashboard page (`GET /`) + stats APIs (`/api/dashboard/stats|scheduled|deleted|reclaimed-chart`); `_data.py` shapes the view model, `_poster_fanout.py` batches poster URLs. |
| `src/mediaman/web/routes/subscribers.py` | Newsletter CRUD + public `GET`/`POST /unsubscribe` (per-IP `_UNSUB_LIMITER`) + admin `POST /api/newsletter/send`; mixes admin-gated and unauthenticated routes. |
| `src/mediaman/web/routes/downloads.py` | Active-download list (`GET /downloads`, `GET /api/downloads`) + `POST /api/downloads/{dl_id:path}/abandon`. |

## Invariants

- **No prefix on mount.** `app_factory.py` imports 17 top-level routers and `include_router()`s each with no prefix; every handler declares its own full path. Sub-package routers (`download`, `search`, `users`, `settings`, `recommended`, `library_api`) are composed via `router.include_router()` in their own `__init__.py` first.
- **One JSON shape.** Every structured JSON response goes through `respond_ok`/`respond_err`; routes must not hand-build `JSONResponse` with ad-hoc keys. Convention: `/api/*` returns the envelope, everything else returns a Jinja2 `TemplateResponse` (accessed via `request.app.state.templates`). The two `/api/*` binary/poll exceptions are the poster proxy (image bytes) and the download token flow.
- **One writer for the session cookie.** `session_token` is only ever set via `set_session_cookie()` and cleared via a delete with pinned attributes (`path=/`, `samesite=strict`, `httponly`, matching `secure`); the `Secure` flag is decided solely by `is_request_secure()`, which defaults to secure.
- **Middleware order is load-bearing.** `add_middleware` is LIFO, so `register_security_middleware()` registers them so the runtime chain is `TrustedHost → SecurityHeaders → BodySizeLimit → Obscure405 → CSRF → ForcePasswordChange`. `SecurityHeaders` must wrap `BodySizeLimit` so even 413/403 responses carry the security headers.
- **Auth is per-handler, never per-prefix.** Admin JSON/page routes `Depends(get_current_admin)` (hard 401); poster and `download/status` use `get_optional_admin` (admin session OR a valid HMAC-signed token); HTML pages use `resolve_page_session`.
- **Sensitive settings writes demand a fresh reauth.** Keys in `SENSITIVE_KEYS` (integration URLs, `base_url`, and all `SECRET_FIELDS`) require a recent-reauth ticket via `has_recent_reauth`; the ticket is bound to the SHA-256 of the session token, so logout/rotation cascades it away and a sibling session does not inherit it.
- **Download tokens are DB-authoritative and single-use.** The table `used_download_tokens` (migration 23) is the sole authority; the in-memory LRU in `download/_tokens.py` is only a fast-path negative cache populated after a DB claim. `_mark_token_used` fails closed (raises → 503) on DB error rather than risk a replay; `submit.py` calls `_unmark_token_used` on every failure path so a failed grab can be retried.
- **Poster proxy is auth-first.** The 401 for an unauthenticated caller is returned before any rating-key validity/existence check, so the endpoint cannot enumerate Plex rating keys. Outbound fetches are HTTPS + port-443 only (`_POSTER_ALLOWED_PORT = 443`), host-allowlisted (`allowed_outbound_hosts` = pinned CDNs + configured integrations), DNS-re-resolved (anti-rebind, via `mediaman.services.infra.url_safety`), and `Content-Type` is coerced by `safe_mime` to a known image type. The DB-stored plex URL is re-validated (`sanitise_plex_url`) on every request.
- **No cross-route imports.** Each sub-package must be independently mountable; shared helpers live in `mediaman.web.cookies`, `mediaman.web.auth.middleware`, or a service module.
- **Rate limiting is pervasive and per-module.** Limiters are module-level (process-scoped, per-worker), applied via the `@rate_limit(limiter, key=...)` decorator or an inline `limiter.check(actor_or_ip)`. Client IP comes from `get_client_ip`, which trusts `X-Forwarded-*` only from trusted-proxy CIDRs.

## Gotchas

- `respond_ok` merges with `if data is not None` (not a truthy check) precisely so falsy-but-meaningful payloads like `{"count": 0}` or `{"libraries": []}` survive the envelope.
- `cookies._secure_cookie_override` is `@lru_cache(maxsize=1)`: `MEDIAMAN_FORCE_SECURE_COOKIES` is read once per process. Tests that mutate the env mid-process MUST call `_secure_cookie_override.cache_clear()` or `is_request_secure` uses the stale value.
- `is_request_secure` defaults to `True` even for a plaintext HTTP request — a plaintext loopback dev deployment must set `MEDIAMAN_FORCE_SECURE_COOKIES=false`, otherwise the `Secure` cookie is never sent over http and login appears to silently fail.
- `_parse_allowed_hosts` maps empty/unset `MEDIAMAN_ALLOWED_HOSTS` to `["*"]` (accept ANY Host header) with only a startup warning — Host-header poisoning is wide open until an operator pins a hostname. When a real allowlist is set it always appends `localhost`/`127.0.0.1` so the Docker healthcheck (`Host: localhost`) is not rejected.
- `download/_tokens.py` silently falls back to in-memory-only mode when the DB is uninitialised (`get_db` raises) — intended for the `_tokens` unit tests, but in that mode the in-memory LRU is the ONLY replay authority. `reset_used_tokens()` clears only the in-memory LRU, not the DB table.
- `poster/__init__.py` imports several private `fetch.py` names (`_POSTER_HTTP`, `_fetch_arr_poster`, `_is_allowed_poster_host`, `_safe_mime`) solely so tests can monkeypatch them at `mediaman.web.routes.poster.<name>`; they are intentionally NOT in `__all__`. `_make_poster_client` returns the patched `_POSTER_HTTP` stub unchanged if it is no longer a real `SafeHTTPClient`, so a test double bypasses the per-request allowlist rebuild.
- `download/confirm.py` keeps a 30s process-local Arr-state TTL cache (`_RADARR_CACHE`/`_SONARR_CACHE`, `_ARR_CACHE_TTL_SECONDS = 30.0`, double-checked locking, `_ARR_CACHE_MAX_ENTRIES = 20`) because a naive render issued 4 outbound Arr calls × 30 req/min/IP = a request-amplification vector against the operator's home Arr boxes. It is per-worker, so multi-worker deploys re-fetch per worker (accepted).
- `settings/secrets.has_sensitive_key_changes` deliberately treats a secret written back as the masking placeholder or `""` as a no-op (no reauth demanded) but treats the explicit `SECRET_CLEAR_SENTINEL` as a sensitive change — deleting a stored credential still requires reauth.
- `POST /api/downloads/{dl_id:path}/abandon` uses the `:path` converter so a slash-bearing download id is captured whole — the converter must be preserved verbatim.
- `auth.login_submit` sanitises the attacker-controlled username (control bytes, length cap 64) before it reaches the logger, the audit `actor` column, AND the audit `detail` blob — the detail blob is rendered into the history page UI, so this is an XSS/log-forging boundary, not just log hygiene.

## Extension points

- **New route module:** create a sub-package with a module-level `router = APIRouter()`, declare full paths on each handler, then import + `include_router()` it in `app_factory.py` (the import list is inside the registration function to preserve lazy import cost).
- **New JSON field:** add it to the `respond_ok`/`respond_err` call — never hand-build a `JSONResponse`.
- **New sensitive settings key:** add it to `SENSITIVE_KEYS` (or `SECRET_FIELDS`) in `settings/secrets.py` so writes inherit the reauth gate.
- **New outbound poster host:** extend `allowed_outbound_hosts` (poster/`fetch.py`) — the `SafeHTTPClient` allowlist is the sole authority for which hosts may be reached.
- **New middleware:** add it in `register_security_middleware()`, minding the LIFO order so its runtime position is correct relative to `SecurityHeaders`.

## Related

- Auth predicates: `src/mediaman/web/auth/` — `get_current_admin`, `get_optional_admin`, `resolve_page_session`, `has_recent_reauth`
- Middleware classes: `src/mediaman/web/middleware/` (registered by `web/__init__.py`, defined there)
- Persistence: `src/mediaman/web/repository/`
- Business logic + SSRF/rate-limit primitives: `src/mediaman/services/` (`rate_limit`, `arr`, `downloads`, `infra`, `media_meta`)
- Token signing / crypto: `src/mediaman/crypto`
- Request-body models: `src/mediaman/web/models/` and `src/mediaman/web/models/users`
