<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../INDEX.md)

# Security

## Facts

### Authentication — passwords, sessions, fingerprints, lockout, reauth

| Item | Value | Source |
|------|-------|--------|
| Password hashing | bcrypt, `BCRYPT_ROUNDS=12`; inputs >72 bytes get a SHA-256 pre-hash (base64) before `hashpw`/`checkpw` so both sides stay symmetric | `web/auth/_password_hash_helpers.py` (`BCRYPT_ROUNDS`) |
| Constant-time auth | User-not-found path burns a dummy bcrypt cycle at the same `BCRYPT_ROUNDS` so a real vs. fake username takes equal wall time | `web/auth/password_hash/_authenticate.py` (`authenticate`) |
| Password policy | Min 12 chars, `MAX_BYTES=1024`, not a username substring, not in the bundled common-password list, 3-of-4 char classes (waived for 20+ char high-variance passphrases), NFKC-normalised | `web/auth/password_policy.py` (`password_issues`) |
| Session cookie | Name `session_token`; `HttpOnly`, `SameSite=Strict`, `Secure` per `is_request_secure()`; `SESSION_COOKIE_MAX_AGE=86400` (24h) | `web/cookies.py` (`set_session_cookie`, `SESSION_COOKIE_MAX_AGE`) |
| Session storage | Raw token never persisted — `sha256(token)` stored in both the `token` PK column and `token_hash` | `web/auth/session_store/__init__.py` (`create_session`) |
| Session hard expiry | `_HARD_EXPIRY_DAYS=1`; idle timeout 24h checked in `_idle_expired`; corrupt/unparseable timestamps fail closed (treated as expired) | `web/auth/session_store/__init__.py` (`_HARD_EXPIRY_DAYS`) · `web/auth/session_store/_validate.py` (`_idle_expired`) |
| Client fingerprint binding | `UA-hash:IP-prefix`; modes `off`/`loose` (default, /24+/64, 16 hex chars)/`strict` (full IP+UA) via `MEDIAMAN_FINGERPRINT_MODE`; no request context ⇒ fail closed | `web/auth/_session_fingerprint.py` |
| Login lockout | DB-backed per-username (`login_failures` table), atomic UPSERT under `BEGIN IMMEDIATE`; escalating bands 5→15min, 10→1h, 15→24h (descending check so the stricter lock wins); counter keeps climbing while locked but `locked_until` never slides forward mid-band (anti-DoS); username used verbatim, never lowercased | `web/auth/login_lockout.py` (`_LOCK_RULES`) |
| Reauth tickets | `reauth_tickets` table keyed on `sha256(session_token)`; window `MEDIAMAN_REAUTH_WINDOW_SECONDS` (default 300s, clamped 30–3600); failures feed `login_lockout` under a `reauth:<username>` namespace (`REAUTH_LOCKOUT_PREFIX`), never the plain-login counter; revoked in lockstep with every session-destruction path | `web/auth/reauth.py` (`REAUTH_LOCKOUT_PREFIX`, `grant_recent_reauth`, `has_recent_reauth`) |
| Sensitive settings gate | Writes to `SENSITIVE_KEYS` (integration URLs, `base_url`, all `SECRET_FIELDS`) require a fresh reauth ticket | `web/routes/settings/secrets.py` (`SENSITIVE_KEYS`) · `web/auth/reauth.py` (`has_recent_reauth`) |
| Force-password-change | `ForcePasswordChangeMiddleware` intercepts cookie-bearing requests from `must_change_password` admins: GET→302 `/force-password-change`, other methods→403 JSON; bypass list `_ALLOWED_PREFIXES` = `/force-password-change`, `/static/`, `/login`, `/api/auth/logout`, `/healthz`, `/readyz`; fails OPEN on `ImportError`/DB `RuntimeError`/invalid session (a masked startup failure would be worse) | `web/middleware/force_password_change.py` (`_ALLOWED_PREFIXES`) |
| Auth ownership | `web/auth/` is the only package permitted to `import bcrypt` or read/write `admin_users`, `admin_sessions`, `login_failures`, `reauth_tickets` | `web/auth/__init__.py` (package docstring) |
| Auth route consumption | Route handlers depend on `middleware.py` predicates (`get_current_admin`, `get_optional_admin`, `resolve_page_session`) — never call `validate_session` directly, or fingerprint binding is skipped | `web/auth/middleware.py` |

### CSRF

| Item | Value | Source |
|------|-------|--------|
| Protected methods | `_CSRF_PROTECTED_METHODS` = POST/PUT/PATCH/DELETE/TRACE/CONNECT | `web/middleware/csrf.py` (`_CSRF_PROTECTED_METHODS`) |
| Defence | Origin/Referer host-match against the request; IPv6 + default-port stripping via `_normalise_origin` | `web/middleware/csrf.py` (`_normalise_origin`) |
| Exempt routes | Explicit `(method, regex)` allowlist for HMAC-token routes: `POST /download/{token}`, `POST /keep/{token}`, `/unsubscribe` — each authorised by a signed token in the URL, not the session cookie | `web/middleware/csrf.py` (`_CSRF_EXEMPT_ROUTES`) |
| Fail-closed | An unresolvable host (empty netloc) or a missing Origin AND Referer, with a `session_token` cookie present, is rejected 403 | `web/middleware/csrf.py` |
| Scheme check | HOST-only comparison by default (prior scheme+host hardening broke reverse-proxy deployments); scheme is additionally required only when a session cookie is present AND the request itself resolves `https` | `web/middleware/csrf.py` |
| Cookie key | Both CSRF and ForcePasswordChange key off the cookie named exactly `session_token` | `web/middleware/csrf.py`, `web/middleware/force_password_change.py` |

### Security headers

| Header | Value | Source |
|--------|-------|--------|
| CSP | Per-request nonce (`secrets.token_urlsafe(16)`); `script-src 'self' 'nonce-…'` — **no** `'unsafe-inline'`; `style-src` nonce + separate `style-src-attr 'unsafe-inline'` for inline `style=` attrs (Chromium requirement) | `web/middleware/security_headers.py` (`_build_csp`) |
| Static headers | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` | `web/middleware/security_headers.py` (`_STATIC_HEADERS`) |
| HSTS | Fail-closed: needs `MEDIAMAN_HSTS_ENABLED=true` AND request scheme `https`; `MEDIAMAN_FORCE_SECURE_COOKIES=false` is a hard override that disables it regardless; `MEDIAMAN_HSTS_PRELOAD=true` adds `preload` | `web/middleware/security_headers.py` (`_should_emit_hsts`) |
| Cache-Control | `no-store, private` via `setdefault` on every response except `/static/` | `web/middleware/security_headers.py` |
| Application semantics | All headers applied via `response.headers.setdefault` — a handler that set its own value wins | `web/middleware/security_headers.py` |
| Server fingerprinting | uvicorn always runs `server_header=False`, `date_header=False` | `main.py` (`cli_main`) |
| Host header | `MEDIAMAN_ALLOWED_HOSTS` unset/empty → accept ANY Host (`["*"]`) with only a startup warning; when set, `localhost`/`127.0.0.1` are always appended so the Docker healthcheck isn't rejected | `web/__init__.py` (`_parse_allowed_hosts`) |
| Middleware mount order (LIFO) | `register_security_middleware()` adds `ForcePasswordChange, CSRFOrigin, Obscure405, BodySizeLimit, SecurityHeaders, TrustedHost`, yielding runtime chain `TrustedHost → SecurityHeaders → BodySizeLimit → Obscure405 → CSRFOrigin → ForcePasswordChange`; `SecurityHeaders` must wrap `BodySizeLimit` so even a 413 carries security headers | `web/__init__.py` (`register_security_middleware`) |
| Method-enumeration obscuring | On `/api/*` only, a 405 is rewritten to a generic 401 `{"detail":"Not authenticated"}` with the `Allow` header dropped; HTML routes keep genuine 405s | `web/middleware/obscure_405.py` (`Obscure405Middleware`) |

### Body-size limits

| Item | Value | Source |
|------|-------|--------|
| Cap | `MEDIAMAN_MAX_REQUEST_BYTES`, default `8 * 1024 * 1024` (8 MiB) | `web/middleware/body_size.py` (`_DEFAULT_MAX_REQUEST_BYTES`) |
| Enforcement | Pure-ASGI `BodySizeLimitMiddleware` (only non-`BaseHTTPMiddleware` class in the stack — `BaseHTTPMiddleware` buffers the whole body first, which defeats a streaming cap); fast-path on declared `Content-Length`, then a streaming byte-count that short-circuits with a 413 | `web/middleware/body_size.py` |
| Opt-out | `max_bytes <= 0` (including 0) means UNLIMITED, not "block everything" | `web/middleware/body_size.py` |

### Rate limiting

| Limiter | Shape | Source |
|---------|-------|--------|
| Login | Per-IP `RateLimiter(max_attempts=5, window_seconds=300)` | `web/routes/auth.py` (`_limiter`) |
| Unsubscribe | Per-IP `RateLimiter(max_attempts=20, window_seconds=60)` | `web/routes/subscribers.py` (`_UNSUB_LIMITER`) |
| Force-password-change | Dual per-actor + per-IP `ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=20)` | `web/routes/force_password_change.py` (`_FORCE_CHANGE_LIMITER`, `_FORCE_CHANGE_IP_LIMITER`) |
| Poster proxy (public) | `RateLimiter(max_attempts=60, window_seconds=60)` | `services/rate_limit/instances.py` (`POSTER_PUBLIC_LIMITER`) |
| Newsletter send | `ActionRateLimiter(max_in_window=3, window_seconds=300, max_per_day=10)` | `services/rate_limit/instances.py` (`NEWSLETTER_LIMITER`) |
| Settings write / test | `ActionRateLimiter(max_in_window=20, window_seconds=60, max_per_day=200)` / `(10, 60, 60)` | `services/rate_limit/instances.py` (`SETTINGS_WRITE_LIMITER`, `SETTINGS_TEST_LIMITER`) |
| Subscriber write | `ActionRateLimiter(max_in_window=5, window_seconds=60, max_per_day=50)` | `services/rate_limit/instances.py` (`SUBSCRIBER_WRITE_LIMITER`) |
| Scan trigger | `ActionRateLimiter(max_in_window=3, window_seconds=60, max_per_day=20)` | `services/rate_limit/instances.py` (`SCAN_TRIGGER_LIMITER`) |
| Bucketing | `RateLimiter` buckets IPv4 by /24, IPv6 by /64 (otherwise per-IP limits are IPv6-bypassable), LRU-capped at 10k buckets; `ActionRateLimiter` keys per-actor username with a **sliding** 24h window (not calendar-day, to close the midnight double-quota bug) | `services/rate_limit/limiters.py` (`RateLimiter`, `ActionRateLimiter`) |
| Client IP resolution | Trust hierarchy peer-trust → `cf-connecting-ip` (only when peer ∈ `MEDIAMAN_CLOUDFLARE_PROXIES`) → XFF chain → `x-real-ip` → peer fallback; a literal `*` in either proxy env var triggers refuse-all-proxies (CRITICAL log) | `services/rate_limit/ip_resolver.py` (`get_client_ip`, `_parse_proxy_env`) |
| Application point | Enforced at the route handler (the trust boundary) before any DB lookup that could leak timing; a handler that does work first is a finding | `CODE_GUIDELINES.md` §10.7 |

### Secrets — AES-GCM encryption + key derivation

| Item | Value | Source |
|------|-------|--------|
| Cipher | AES-256-GCM, `0x02` v2-prefix, 12-byte nonce, key-name-as-AAD row binding (moving a ciphertext to another settings row fails authentication) | `crypto/aes.py` (`encrypt_value`, `decrypt_value`) |
| Key derivation | HKDF-SHA256 over `MEDIAMAN_SECRET_KEY` + a per-install 16-byte salt (`aes_kdf_salt` settings row, `INSERT OR IGNORE` race-safe, single-entry cache) | `crypto/_aes_key.py` (`_derive_aes_key_hkdf`, `_load_or_create_salt`) |
| Salt integrity | Any decoded length other than 16 bytes (or undecodable base64) is treated as DB tampering → `CryptoError` | `crypto/_aes_key.py` (`_load_or_create_salt`) |
| Secret-key strength | `MEDIAMAN_SECRET_KEY` presence + entropy checked at boot via `_is_secret_key_strong`; process refuses to start on failure | `crypto/_aes_key.py` (`_is_secret_key_strong`) · `config.py` (`load_config`) |
| Canary | `is_canary_valid` detects a `MEDIAMAN_SECRET_KEY` mismatch at boot against the `aes_kdf_canary` settings row; does NOT abort boot (admin must still be able to log in) but the scheduler refuses to start (`SchedulerStartupRefused`) while `canary_ok` is false | `crypto/aes.py` (`is_canary_valid`) · `bootstrap/crypto.py`, `bootstrap/scan_jobs.py` |
| Decrypt failure classes | `CryptoInputError` (empty/>64 KiB/non-base64), `ValueError` (v2-shaped but no salt source), `InvalidTag` (auth failure — also raised for a non-v2 blob) | `crypto/aes.py` (`decrypt_value`) |
| Storage boundary | Encrypt-on-write / decrypt-on-read happens only in `repository/settings.py`; plaintext never lands on disk, ciphertext never escapes the repository | `web/repository/settings.py` (`load_settings`, `write_settings`) · `CODE_GUIDELINES.md` §9.9 |
| Only plaintext secret | `MEDIAMAN_SECRET_KEY` (env var); every integration credential (Plex, Sonarr/Radarr, NZBGet, Mailgun, TMDB, OMDb, OpenAI) is encrypted at rest | `CODE_GUIDELINES.md` §10.3 |

### Signed tokens

| Purpose | Domain-separation label | Lifecycle | Source |
|---------|--------------------------|-----------|--------|
| Keep (snooze/keep-forever) | `_TOKEN_PURPOSE_KEEP` | Single-use — consumption recorded in `keep_tokens_used`; stored as `token_hash` only | `crypto/tokens.py` (`generate_keep_token`, `validate_keep_token`) · `scanner/phases/upsert.py` (`schedule_deletion`) |
| Download confirmation | `_TOKEN_PURPOSE_DOWNLOAD` | Single-use, DB-authoritative via `used_download_tokens`; in-memory LRU is a fast-path negative cache only; recipient email embedded reversibly in the URL (documented, non-confidential) | `crypto/tokens.py` (`generate_download_token`) · `db/schema_definition.py` (`used_download_tokens`) |
| Unsubscribe | `_TOKEN_PURPOSE_UNSUBSCRIBE` | Carries the subscriber email inside the signed token, never as a query param, so it never lands in access logs | `crypto/tokens.py` (`generate_unsubscribe_token`) · `services/mail/newsletter/subscribers.py` |
| Poster proxy | `_TOKEN_PURPOSE_POSTER` | Default TTL 180 days; validated with `hmac.compare_digest` (proved by a test patching `mediaman.crypto.hmac.compare_digest`) | `crypto/tokens.py` (`generate_poster_token`, `validate_poster_token`) |
| Poll (download status) | `_TOKEN_PURPOSE_POLL` | 600s TTL is the ONLY replay defence — no server-side `poll_tokens_used` table, so any holder can replay until `exp` (accepted, documented) | `crypto/tokens.py` (`generate_poll_token`) |
| Session | N/A — opaque, not HMAC | `secrets.token_hex(32)`; deliberately no `validate_session_token` (validated via DB lookup of the hash instead) | `crypto/tokens.py` (`generate_session_token`) |
| Signing scheme | HMAC-SHA256; per-purpose subkey via `_derive_token_subkey` (cached by `sha256(secret)`); `exp` must be a finite, non-bool `int`/`float` strictly in the future (`bool`/`Infinity`/`NaN` rejected) | `crypto/tokens.py` (`_derive_token_subkey`, `_validate_signed`) |
| At-rest storage | Every persisted token is `SHA-256(token)` — never raw; `_token_hashing.py`'s `hash_token` is the single shared definition for session store + reauth | `web/auth/_token_hashing.py` (`hash_token`) · `CODE_GUIDELINES.md` §10.2 |

### SSRF defence

| Item | Value | Source |
|------|-------|--------|
| Client | `SafeHTTPClient` — every outbound call (Sonarr, Radarr, NZBGet, TMDB, OMDb, Mailgun, OpenAI, poster proxy, Plex via `_SafePlexSession`) enforces, in order: (1) SSRF re-validation per call, (2) DNS pin, (3) `allow_redirects=False`, (4) split timeout (connect 5s, read 30s), (5) size cap (8 MiB default; per-integration overrides — 64 MiB Arr, 1 MiB NZBGet, 16 MiB Plex), (6) retry only on idempotent methods | `services/infra/http/client/_core.py` (`SafeHTTPClient`) |
| Deny-list (always on) | Metadata IPs/hostnames (`169.254.169.254`, `100.100.100.200`, `fd00:ec2::254`, `metadata.google.internal`), unspecified/wildcard, link-local, IPv6 ULA, Teredo, 6to4, multicast, broadcast, CGNAT | `services/infra/_url_safety_blocks.py` (`_METADATA_IPS`, `_METADATA_HOSTNAMES`, `_ip_is_blocked`) |
| Loopback / RFC1918 | Allowed by default; refused only under `MEDIAMAN_STRICT_EGRESS=1` or `strict_egress=True` | `services/infra/_url_safety_blocks.py` (`_STRICT_BLOCKED_V4_NETS`, `_STRICT_BLOCKED_V6_NETS`) |
| Resolution | A non-resolving hostname is refused (fail-closed — cannot be proven safe); `getaddrinfo` bounded to 5s on a one-shot thread; every returned address checked; IPv4-mapped-IPv6 unwrapped before checks | `services/infra/_url_safety_blocks.py` (`_resolve_all`, `_ip_is_blocked`) |
| DNS-rebind defence | DNS pinning — the validated IP is pinned for the request's duration via a process-global `socket.getaddrinfo` monkeypatch, re-verified every request; `pin()` is the only supported install path | `services/infra/http/dns_pinning.py` (`pin`, `ensure_hook_installed`) |
| Decompression-bomb defence | Any non-identity `Content-Encoding` is rejected unconditionally on every read; `SafeHTTPClient` sends `Accept-Encoding: identity` | `services/infra/http/streaming.py` (`_read_capped`) |
| Allowlist | `PINNED_EXTERNAL_HOSTS` (static: `api.themoviedb.org`, `image.tmdb.org`, `www.omdbapi.com`, `api.mailgun.net`, `api.eu.mailgun.net`, `api.openai.com`) + `allowed_outbound_hosts(conn)` (adds the configured `plex_url`/`radarr_url`/`sonarr_url`/`nzbget_url` hostnames); fails closed to the pinned-only set on any `sqlite3.Error` reading integration rows | `services/infra/url_safety.py` (`PINNED_EXTERNAL_HOSTS`, `allowed_outbound_hosts`) |
| Allowlist enforcement status | Opt-in per call via `allowed_hosts=`, not yet mandatory at the `SafeHTTPClient` layer — the deny-list still applies unconditionally either way | `CODE_GUIDELINES.md` §10.6 |
| Docker bridge exemption | `host.docker.internal`/`gateway.docker.internal` are exempt from the `.internal`-suffix block and the resolved-IP block (re-resolved at request time); non-strict mode only; does NOT skip the allowlist gate | `services/infra/_url_safety_blocks.py` (`_ALLOWED_DOCKER_HOSTNAMES`) |
| Poster proxy | Auth-first (401 before any rating-key check, closing an enumeration path); HTTPS + port-443-only (`_POSTER_ALLOWED_PORT=443`); host-allowlisted; DNS-re-resolved; `Content-Type` coerced by `safe_mime`; the DB-stored Plex URL is re-validated (`sanitise_plex_url`) on every request | `web/routes/poster/fetch.py` (`_POSTER_ALLOWED_PORT`) |
| Plex client | `PlexClient` injects `_SafePlexSession` into `plexapi`'s `PlexServer` (never an un-hardened session); constructor raises `SSRFRefused` on a failing URL; installs `defusedxml.defuse_stdlib()` at import | `services/media_meta/plex.py` (`PlexClient`) · `services/media_meta/_plex_session.py` (`_SafePlexSession`) |
| XML parsing | NZBGet XML-RPC parsed via `defusedxml` — `xml.etree.ElementTree.parse` on untrusted input is forbidden (XXE) | `CODE_GUIDELINES.md` §10.14 |

### Path safety & deletion

| Item | Value | Source |
|------|-------|--------|
| Deletion gate | `delete_path` requires a non-empty `allowed_roots`; target must be absolute and a **strict** descendant of a validated root (never equal to a root); symlink targets refused; same-device-pinned; walks with `os.fwalk(follow_symlinks=False)` | `services/infra/storage/deletion.py` (`delete_path`, `_safe_rmtree`) |
| TOCTOU closure | Pre-deletion symlink check uses atomic `O_NOFOLLOW \| O_DIRECTORY` | `services/infra/storage/_delete_roots.py` (`_check_symlink_via_nofollow`) |
| Forbidden roots | `_FORBIDDEN_ROOTS` refuses configuring a delete root at any system dir (`/`, `/bin`, `/boot`, `/data`, `/dev`, `/etc`, `/home`, `/lib`, `/lib32`, …, including resolved macOS `/private/{tmp,var,etc}` and mediaman's own `/media`/`/data` mounts) | `services/infra/storage/_delete_roots.py` (`_FORBIDDEN_ROOTS`) |
| Fail-closed config | `MEDIAMAN_DELETE_ROOTS` unset ⇒ zero allowed roots ⇒ every deletion fails `DeletionRefused` (a misconfigured no-op is recoverable; a misconfigured `/etc` delete is not) | `CODE_GUIDELINES.md` §10.9 |
| Read-only path resolution | `resolve_safe_readonly_path` (per-component symlink walk) is for READ-ONLY (stat-only) callers only — carries a documented, accepted TOCTOU window; destructive callers MUST use the fd-based `O_NOFOLLOW` check instead | `services/infra/path_safety.py` (`resolve_safe_readonly_path`) |
| File I/O boundary | Production code reads/writes only under `MEDIAMAN_DATA_DIR` and the configured delete roots; a new `open()` against `/tmp` or `/etc` in `src/mediaman/` is a review blocker | `CODE_GUIDELINES.md` §8.3 |

### Audit log

| Item | Value | Source |
|------|-------|--------|
| Append-only enforcement | `BEFORE UPDATE`/`BEFORE DELETE` triggers `RAISE(ABORT)`; dropping either trigger is visible in `sqlite_master`; INSERT is unrestricted | `db/schema_definition.py` (`audit_log_no_update`, `audit_log_no_delete`) |
| Writers | `log_audit` (media actions); `security_event` (best-effort, self-commits, swallows errors — for low-stakes events outside a wider transaction); `security_event_or_raise` (fail-closed, caller owns the transaction — propagates INSERT failure so the whole operation rolls back) | `core/audit.py` (`log_audit`, `security_event`, `security_event_or_raise`) |
| Log-injection defence | `_strip_audit_field` strips CR/LF/NUL from free-form audit fields before they are written | `core/audit.py` (`_strip_audit_field`) |
| Atomicity | Every state change that takes `audit_actor` writes its `sec:*` row inside the same `BEGIN IMMEDIATE` as the data change — never a data change without its audit trail | `web/auth/middleware.py` invariants · `CODE_GUIDELINES.md` §9.7 |
| Required events | Authentication (login success/failure, password change, session revocation), authorisation (admin promotion, forced password change), destructive actions (manual delete, scheduled deletion executed), signed-token state changes (keep snooze/forever, download confirmation), subscriber lifecycle | `CODE_GUIDELINES.md` §7.5, §10.10 |
| Not required | Read-only routes, scan steps with no state change, internal recovery loops | `CODE_GUIDELINES.md` §7.5 |
| Auto-abandon ordering | `security_event` is emitted BEFORE the destructive Arr abandon call, so a compromised-settings attack stays discoverable even if Radarr/Sonarr is unreachable | `services/arr/auto_abandon.py` (`_abandon_movie_with_audit`) |
| Attacker-controlled fields sanitised | Login username is length-capped (64) and control-byte-stripped before it reaches the logger, the audit `actor` column, AND the audit `detail` blob (the detail blob renders into the history page UI — an XSS/log-forging boundary) | `web/routes/auth.py` (`login_submit`) |
| Fields recorded | Actor, action, target, timestamp, source IP; anonymous routes (keep tokens, download confirmations) log against the token's identity, never the IP alone; user-agent deliberately not recorded (single-operator threat model) | `CODE_GUIDELINES.md` §10.10 |

## Procedures

1. **Rotate `MEDIAMAN_SECRET_KEY`** — every encrypted setting and every signed token becomes unreadable/unverifiable; the AES canary will fail at next boot (`bootstrap_crypto`) and `/readyz` stays unready until the key is corrected or settings are re-entered. There is no re-encryption tool — rotating the key without a matching plan to re-enter integration credentials is destructive.
2. **Recover a locked-out admin** — use `admin_unlock_with_audit` (writes an audit row) rather than a manual DB edit, so the unlock is traceable. `web/auth/login_lockout.py` (`admin_unlock_with_audit`).
3. **Add a new outbound integration host** — add it to `PINNED_EXTERNAL_HOSTS` (`services/infra/url_safety.py`) in the same PR that introduces the call; review `CODE_GUIDELINES.md` §10.6 first — this is a security-reviewed change, not a routine one.
4. **Add a new CSRF-exempt route** — add a `(method, re.Pattern)` entry to `_CSRF_EXEMPT_ROUTES` (`web/middleware/csrf.py`) ONLY for a route whose authorisation is a URL token, never the session cookie.
5. **Add a new secret field** — add the key to `SECRET_FIELDS` (`web/repository/settings.py`) so it inherits encrypt-on-write / mask-on-read and the placeholder no-op semantics; do not read/write it directly.

## Failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Admin login silently fails over plain HTTP | `is_request_secure()` defaults `True`, so the `Secure` cookie attribute is set even on a loopback dev deployment and the browser never sends it back | Set `MEDIAMAN_FORCE_SECURE_COOKIES=false` for plaintext-HTTP dev deployments |
| Any Host header is accepted (Host-header poisoning) | `MEDIAMAN_ALLOWED_HOSTS` unset/empty maps to `["*"]` with only a startup warning | Set `MEDIAMAN_ALLOWED_HOSTS` to the real hostname(s) |
| Scheduler refuses to start; `/readyz` 503 | AES canary mismatch (`MEDIAMAN_SECRET_KEY` changed or wrong) sets `canary_ok=False`; `bootstrap_scheduling` raises `SchedulerStartupRefused` | Restore the correct `MEDIAMAN_SECRET_KEY`, or accept degraded mode until it is fixed — the web UI stays up so an admin can still log in |
| Per-IP rate limits bypassable / forged client IP in audit log | `X-Forwarded-For`/`cf-connecting-ip` honoured from an untrusted peer, or a wildcard configured in `MEDIAMAN_TRUSTED_PROXIES`/`MEDIAMAN_CLOUDFLARE_PROXIES` | A literal `*` is already refused (CRITICAL log) by `_parse_proxy_env` — pin the env vars to the real proxy CIDR, never leave them wildcarded |
| Reauth-gated settings write unexpectedly demands reauth again | `has_sensitive_key_changes` treats an explicit `SECRET_CLEAR_SENTINEL` as a sensitive change even though it looks like "deleting", by design — clearing a credential still requires reauth | Expected behaviour, not a bug |
| Decrypt raises `ConfigDecryptError` instead of returning a default | `get_setting`/`load_settings` distinguish "never set" from "decrypt failed" (wrong key, tampered ciphertext, tampered AAD) | Investigate `MEDIAMAN_SECRET_KEY` correctness before assuming the setting is simply unset |
| Poll token replayed after first use | Poll tokens have no server-side replay table — the 600s TTL is the only defence, by design | Not a bug; documented on `PollTokenPayload` |

## Related

- Law: [`CODE_GUIDELINES.md`](../../CODE_GUIDELINES.md) §10 (Security) — canonical; every fact above traces to a rule there. §7.4/§7.5 (logging/audit), §9.9 (encrypted columns), §1.11 (fail closed, fail loud).
- Human doc: [`SECURITY.md`](../../SECURITY.md) — vulnerability-reporting policy and scope; this file is the internal fact sheet, that one is the public policy.
- Modules: [web-auth](modules/web-auth.md), [web-middleware](modules/web-middleware.md), [web-http](modules/web-http.md), [web-data](modules/web-data.md), [services-infra](modules/services-infra.md), [platform](modules/platform.md) (crypto/db), [app-entry](modules/app-entry.md) (bootstrap fail-closed gates).
- Decisions: none recorded yet.
