<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: web-middleware

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

The FastAPI app's security perimeter: focused HTTP/ASGI middleware, one narrowly-scoped
concern per sub-module — body-size DoS cap, Origin/Referer CSRF defence, forced-password-change
funnelling, 405→401 method-enumeration obscuring, and security response headers + per-request
CSP nonce. The classes are wired, not self-registering: `register_security_middleware()` in
`src/mediaman/web/__init__.py` imports and mounts them (see [web-http](web-http.md)). The package
also holds the `@rate_limit` route-handler decorator, which is **not** a middleware but lives
here because it is FastAPI-coupled.

## Key files

| File | Role |
|------|------|
| `src/mediaman/web/middleware/__init__.py` | Package docstring only (no code). Documents the deliberate debt that most middleware still use Starlette `BaseHTTPMiddleware` despite Starlette recommending pure ASGI, and that only `body_size` is pure ASGI by necessity. |
| `src/mediaman/web/middleware/body_size.py` | Pure-ASGI `BodySizeLimitMiddleware` — the only non-`BaseHTTPMiddleware` class here. Fast-path on declared `Content-Length` (`_declared_length_over_cap`), then a streaming byte-count that short-circuits mid-flight with a 413 (`_send_413`). Cap from `MEDIAMAN_MAX_REQUEST_BYTES` (default 8 MiB) via `_resolve_max_request_bytes`; `max_bytes=None` defers the env read to the first request and caches it. |
| `src/mediaman/web/middleware/csrf.py` | `CSRFOriginMiddleware` — Origin/Referer host-match on state-changing methods (`_CSRF_PROTECTED_METHODS`: POST/PUT/PATCH/DELETE/TRACE/CONNECT). Explicit `(method, regex)` allowlist `_CSRF_EXEMPT_ROUTES` for HMAC-token routes (`/download/{t}`, `/keep/{t}`, `/unsubscribe`). `_normalise_origin` handles IPv6 + default-port stripping. |
| `src/mediaman/web/middleware/force_password_change.py` | `ForcePasswordChangeMiddleware` — intercepts cookie-bearing requests from admins flagged `must_change_password`; GET → 302 to `/force-password-change`, other methods → 403 JSON. `_ALLOWED_PREFIXES` bypass list. Lazy-imports `get_db`, `resolve_cached_session`, `user_must_change_password`. |
| `src/mediaman/web/middleware/obscure_405.py` | `Obscure405Middleware` — on `/api/*` only, rewrites a 405 Method-Not-Allowed to a generic 401 `{"detail":"Not authenticated"}` and drops the `Allow` header. HTML routes keep genuine 405s. |
| `src/mediaman/web/middleware/security_headers.py` | `SecurityHeadersMiddleware` — mints a per-request CSP nonce (`secrets.token_urlsafe(16)`) onto `request.state.csp_nonce`, weaves it into a per-request CSP (`_build_csp`), and applies `_STATIC_HEADERS`, `Server` banner, `Cache-Control: no-store` (except `/static/`), and HSTS — all via `setdefault`. |
| `src/mediaman/web/middleware/rate_limit.py` | `rate_limit(limiter, key='actor'|'ip')` decorator for FastAPI handlers (**not** ASGI middleware). Validates the handler signature at decoration time; at call time resolves the key via `inspect.signature.bind_partial`, runs `limiter.check()`, returns `respond_err(..., status=429)` when throttled, logs `rate_limit.throttled scope=… actor=…`. |

## Invariants

- **Mount order is LIFO and load-bearing.** `register_security_middleware()` (`web/__init__.py`) calls `add_middleware` in the order `ForcePasswordChange, CSRFOrigin, Obscure405, BodySizeLimit, SecurityHeaders, TrustedHost`, yielding the execution chain `TrustedHost → SecurityHeaders → BodySizeLimit → Obscure405 → CSRFOrigin → ForcePasswordChange`. `SecurityHeaders` is mounted second-to-last so it wraps `BodySizeLimit`: the raw-ASGI 413 emitted by `BodySizeLimit` (and inner-middleware 403s) is captured by `BaseHTTPMiddleware` and still receives the security headers before egress.
- **`BodySizeLimit` must stay pure ASGI.** `BaseHTTPMiddleware` buffers the whole body before invoking the handler, so it cannot cap bytes as they stream in — the whole point of this middleware.
- **CSP `script-src` carries NO `'unsafe-inline'`.** Wave 7 externalised every inline `<script>`; `_build_csp` emits `script-src 'self' 'nonce-…'`. `style-src` uses a nonce for `<style>` blocks plus a **separate** `style-src-attr 'unsafe-inline'` for inline `style=` attributes, because Chromium blocks inline style attributes when `style-src` has a nonce.
- **The CSP nonce is per-request and single-use.** `SecurityHeaders.dispatch` sets `request.state.csp_nonce` BEFORE `call_next` and weaves the same nonce into the CSP header AFTER.
- **Every security header is applied via `response.headers.setdefault`.** A handler that deliberately set its own value (e.g. the poster proxy's `Cache-Control`) wins.
- **CSRF and ForcePasswordChange key off the cookie named exactly `session_token`.**
- **CSRF fails closed.** An unresolvable request host (empty netloc, H4) or a missing Origin AND Referer, when a `session_token` cookie is present, is rejected with 403 — two empty strings must never compare equal as a match.
- **ForcePasswordChange deduplicates session validation.** `resolve_cached_session` caches the session on `request.state` so the downstream route dependency reuses it instead of running a second `validate_session` (H6 fingerprint-eviction race). Both call sites must feed identical fingerprint inputs (UA `or None`).

## Gotchas

- **`rate_limit.py` is a route DECORATOR, not a middleware**, despite living in `middleware/` (moved from `services/` because it imports `web/`). Its `wrapper` is a plain `def` (synchronous): all current `@rate_limit` call sites are sync handlers, but decorating an `async def` handler would return an un-awaited coroutine and silently break — a real constraint for future callers.
- **`BodySizeLimit`: `max_bytes <= 0` (including 0) means UNLIMITED** (operator opt-out), not "block everything". A malformed `Content-Length` is treated as `-1` (not over cap) so the streaming counter still enforces.
- **`BodySizeLimit`: an in-flight oversize on a streamed upload cannot be substituted.** If the inner app already started its response when a later chunk exceeds the cap, the wrapper stops forwarding bytes and closes the connection via a forced `http.disconnect`; the clean post-app 413 (`_send_413`) only fires when oversize AND no response was started.
- **`Obscure405`'s replacement 401 does NOT itself carry security headers or an `Allow` header** — it deliberately relies on the outer `SecurityHeadersMiddleware` to re-apply headers, and intentionally drops `Allow` to hide the method surface.
- **`ForcePasswordChange` fails OPEN by design** on `ImportError`, `get_db` `RuntimeError`, or an invalid/`None` session — it does not 500; masking a startup failure with a misleading error would be worse.
- **CSRF comparison is HOST-ONLY by default.** A prior scheme+host hardening was reverted because it broke reverse-proxy deployments where uvicorn sees `http` but the browser is on `https`. Scheme is only additionally required (H5) when a session cookie is present AND the request itself already resolves as `https`.
- **HSTS is fail-closed (`_should_emit_hsts`).** Needs `MEDIAMAN_HSTS_ENABLED=true` AND `request.url.scheme == "https"`; `MEDIAMAN_FORCE_SECURE_COOKIES=false` is a hard override that disables it even when enabled. Rationale: HSTS `max-age` is 2 years and a one-way door. `MEDIAMAN_HSTS_PRELOAD=true` adds `preload`.
- **`Cache-Control: no-store, private` is applied (via `setdefault`) to EVERY response except paths under `/static/`** — `StaticFiles` owns its own caching headers.
- **No dedicated `body_size` test file.** `BodySizeLimitMiddleware` is exercised inside `tests/unit/web/test_security_headers.py`. Other middleware tests live under `tests/unit/web/middleware/` (`test_csrf`, `test_obscure_405`, `test_session_validation_dedup`) and `tests/unit/web/` (`test_security_headers`, `test_force_password_change`).

## Extension points

- **New middleware:** add it in `register_security_middleware()` (`web/__init__.py`), minding the LIFO order so its runtime position relative to `SecurityHeaders` is correct. Prefer pure ASGI for new classes; migrating an existing `BaseHTTPMiddleware` must preserve every behaviour and test (package `__init__.py`).
- **New CSRF-exempt route:** add a `(method, re.Pattern)` entry to `_CSRF_EXEMPT_ROUTES` (`csrf.py`) — only for routes whose authorisation is a URL token, not the session cookie. The explicit list is grep-able and reviewable by design.
- **New always-on header:** add it to `_STATIC_HEADERS` (`security_headers.py`); it inherits the `setdefault` semantics.
- **New CSP source:** edit `_CSP_STATIC_DIRECTIVES` / `_build_csp` (`security_headers.py`) — the img/frame/connect allowlists live there.
- **New forced-change bypass path:** add a prefix to `_ALLOWED_PREFIXES` (`force_password_change.py`).
- **New rate-limited handler:** apply `@rate_limit(limiter, key=…)`; the handler must expose a `request` parameter (and `admin` for `key="actor"`). Keep it a sync `def`.

## Related

- HTTP surface, orchestrator (`web/__init__.py`), auth predicates, `respond_err`: [web-http](web-http.md)
- Rate-limit primitives (`RateLimiter`, `ActionRateLimiter`, `get_client_ip`): [services-infra](services-infra.md)
- App factory that calls `register_security_middleware`: [app-entry](app-entry.md)
- `request.state.csp_nonce` consumers (Jinja templates / route handlers for inline `<script nonce>` / `<style nonce>`): [web-frontend](web-frontend.md)
- Env vars: `MEDIAMAN_MAX_REQUEST_BYTES`, `MEDIAMAN_HSTS_ENABLED`, `MEDIAMAN_HSTS_PRELOAD`, `MEDIAMAN_FORCE_SECURE_COOKIES`, `MEDIAMAN_ALLOWED_HOSTS`
