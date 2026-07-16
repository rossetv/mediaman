<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: web-auth

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

The sole owner of web-facing authentication for mediaman: admin-user CRUD, bcrypt
password hashing/verification/rotation, the password-strength policy, cookie-backed
session persistence + validation with client-fingerprint binding, DB-backed per-username
login lockout, and short-lived password re-authentication (reauth) tickets for
privilege-establishing actions. Per its package docstring (`auth/__init__.py`, §2.7)
this is the **only** package permitted to `import bcrypt` or to read/write the
`admin_users`, `admin_sessions`, `login_failures`, and `reauth_tickets` tables — every
other package treats them as opaque. Route handlers consume auth through the
`middleware.py` dependency functions rather than calling `validate_session` directly, so
UA+IP fingerprint binding is applied uniformly.

## Key files

| File | Role |
|------|------|
| `src/mediaman/web/auth/__init__.py` | Package marker + ownership-contract docstring (only package allowed to import `bcrypt` or touch the four auth tables). No runtime code — not a barrel; consumers import submodules directly. |
| `src/mediaman/web/auth/middleware.py` | FastAPI auth dependencies: `get_current_admin` (→ `str`\|401), `get_optional_admin` (→ `str`\|None), `get_optional_admin_from_token`, `resolve_page_session` (→ `(username, conn)`\|`RedirectResponse`), `is_admin`. `resolve_cached_session` caches one `validate_session` result per request on `request.state`, keyed by raw token; derives UA/IP once via `get_client_ip`. |
| `src/mediaman/web/auth/password_hash/__init__.py` | Re-export barrel for the bcrypt subsystem (promoted from a single module past the 500-line ceiling). Surfaces `authenticate`/`create_user`/`change_password`/`delete_user`/`list_users`/… and `BCRYPT_ROUNDS`. Documents the SHA-256 pre-hash that defeats bcrypt's 72-byte truncation. |
| `src/mediaman/web/auth/password_hash/_authenticate.py` | `authenticate()` + short-circuit/verify helpers. Constant-time: burns a dummy bcrypt cycle on user-not-found; empty-username and locked-account fast paths skip bcrypt, but the locked path still bumps `record_failure` so 5/10/15 escalation stays reachable even for `record_failures=False` callers. |
| `src/mediaman/web/auth/password_hash/_change_password.py` | `change_password()` rotation in one `BEGIN IMMEDIATE` (UPDATE hash + clear must-change + DELETE sessions + DELETE reauth tickets + audit). Pre-tx guard checks the reauth-namespace lockout and old password; TOCTOU via `rowcount == 0` → `_UserVanished` rollback → `return False`. |
| `src/mediaman/web/auth/password_hash/_user_crud.py` | `admin_users` CRUD (create/list/delete/get_user_email/set_user_email/must-change flags) + `UserExistsError`/`UserRecord`. Audit-in-transaction pattern; `delete_user` refuses last-admin (via `_LastUser` sentinel) and self-delete. |
| `src/mediaman/web/auth/_password_hash_helpers.py` | Private bcrypt-input pipeline (NFKC normalise → SHA-256 pre-hash for >72-byte inputs → base64), `BCRYPT_ROUNDS=12` single source of truth, lazy double-checked cached dummy hash, `_UserVanished`/`_LastUser` rollback sentinels, `_sanitise_log_field` (strips CR/LF/control chars from log fields). |
| `src/mediaman/web/auth/session_store/__init__.py` | Session barrel: `create_session`, `validate_session` (read-only fast path; writer lock only on state change), `destroy_session` (audit-in-tx, fail-closed), `destroy_all_sessions_for`, `list_sessions_for`, `SessionMetadata`. `_HARD_EXPIRY_DAYS=1`; `_SESSION_TOKEN_RE=[0-9a-f]{64}`. |
| `src/mediaman/web/auth/session_store/_validate.py` | Ordered `validate_session` phases: `_fetch_session_row`, `_idle_expired` (24h, corrupt-timestamp fail-closed), `_fingerprint_mismatch` (fail-closed on no-request), `_maybe_refresh_last_used` (60s throttle), `_maybe_sweep_expired` (process-wide, ≤1/min, `_cleanup_lock`). Logger bound to `__package__`. |
| `src/mediaman/web/auth/session_store/_writes.py` | Short `BEGIN IMMEDIATE` write helpers. `_delete_session_with_commit` deletes the session row AND its reauth ticket in one tx; `_try_delete_session` swallows only `sqlite3.Error` (non-DB exceptions propagate — fail closed on the eviction path). |
| `src/mediaman/web/auth/_session_fingerprint.py` | Client fingerprint (`UA-hash:IP-prefix`) for session binding. Modes off / loose (default, /24+/64, 16 UA hex chars) / strict (full IP + full UA hash) via `MEDIAMAN_FINGERPRINT_MODE`. |
| `src/mediaman/web/auth/_token_hashing.py` | Shared `hash_token` (SHA-256 hex). Single definition consumed by `session_store` and `reauth` so the at-rest token hash cannot drift between them. |
| `src/mediaman/web/auth/login_lockout.py` | DB-backed per-username lockout (`login_failures` table). Atomic UPSERT under `BEGIN IMMEDIATE`; 5/10/15 failures → 15 min/1 h/24 h; counter keeps climbing while locked but the window only promotes on band escalation (anti-DoS); 24 h decay; `admin_unlock_with_audit`. Username used verbatim (not lowercased). |
| `src/mediaman/web/auth/reauth.py` | Reauth tickets (`reauth_tickets`, keyed on sha256 session token). `grant_recent_reauth`/`has_recent_reauth` (window + cross-session username check), `verify_reauth_password` (feeds failures into `login_lockout` under a `reauth:<user>` namespace), `revoke_*`/cleanup. Window `MEDIAMAN_REAUTH_WINDOW_SECONDS` (default 300, clamped 30..3600). |
| `src/mediaman/web/auth/password_policy.py` | NIST-ish strength policy. `password_issues()`/`is_strong()`/`policy_summary()`: min 12 chars, `MAX_BYTES=1024`, not a username substring, not in the common list, 3-of-4 char classes (waived for 20+ char high-variance passphrases), no trivial repetition/sequences. NFKC-normalises before every check. |
| `src/mediaman/web/auth/user_crud.py` | `find_username_by_user_id` — the single sanctioned reader of `admin_users.username` outside the bcrypt helpers, for route handlers resolving an ID to a username. |
| `src/mediaman/web/auth/cli.py` | `create_user_cli` entry point for the `mediaman-create-user` console script: resolves username/password (flag / `--password-stdin` / interactive, mutually exclusive), enforces policy, preflights the data dir, inits the DB, calls `create_user`. |
| `src/mediaman/web/auth/data/common_passwords.txt` | ~835 KB bundled common-password blocklist, read lazily at runtime by `password_policy` relative to `__file__`. Hard runtime data dependency — must ship with the package. |

## Invariants

- **Fail-closed on corrupt timestamps.** An unparseable/empty `expires_at` or `last_used_at` → the session is treated as expired and deleted; an unparseable non-empty `locked_until` → the account is treated as STILL LOCKED. An immortal session or an escapable lock is the worst outcome, so ambiguity always resolves to deny.
- **Fail-closed fingerprint binding.** When mode ≠ `off` and the row has a stored fingerprint but validation runs with no request context (`request_supplied=False`), the session is rejected — a stolen cookie replayed through a no-`Request` path cannot bypass binding. With a real request, comparison runs only when both UA and IP are present (in-request tolerance preserved).
- **Constant-time authentication.** `authenticate()` burns a dummy bcrypt cycle on the user-not-found path so a real-username and a fake-username probe take the same wall time; the shared dummy hash uses the same `BCRYPT_ROUNDS` as real hashes to close the timing channel.
- **Lockout key is the username VERBATIM** (never lowercased) so it aligns with the case-sensitive `admin_users.username` lookup; lowercasing would desync the counter and enable a cross-case lockout-DoS.
- **bcrypt input is symmetric.** Both `hashpw` and `checkpw` MUST route through `_prepare_bcrypt_input` so the >72-byte SHA-256 pre-hash matches on both sides; a mismatch would lock every user out. Inputs ≤72 bytes pass through unchanged so pre-existing hashes still verify (no migration).
- **Audit-in-transaction / fail-closed.** Every state change that takes `audit_actor` writes its `sec:*` audit row inside the same `BEGIN IMMEDIATE` as the data change (create/delete user, `change_password`, `set_user_email`, `destroy_session`, `admin_unlock`); if the audit insert fails the whole operation rolls back — never a data change without its audit trail.
- **Session token is never persisted raw.** `create_session` stores `sha256(token)` in BOTH the `token` PK column and `token_hash`; the raw token exists only in the cookie.
- **Reauth tickets die in lockstep with their session.** Every session-destruction path (logout, idle-expiry, fingerprint mismatch, expired sweep, password change, bulk purge) revokes the matching ticket — in the same transaction where the delete is transactional (`revoke_reauth_by_hash_in_tx`).
- **Lockout escalation stays reachable while locked.** Subsequent failures keep bumping the counter (so 5 → 10 → 15 promotion fires) but do NOT slide `locked_until` forward within the same severity band — otherwise an attacker could keep an admin permanently locked (DoS).
- **`validate_session` takes no writer lock on the happy path** — a read-only SELECT; `BEGIN IMMEDIATE` fires only on genuine state change (eviction, `last_used` refresh throttled to 60 s, expired sweep throttled to ≤1/min process-wide).
- **Reauth failures feed a separate counter.** They feed `login_lockout` under a `reauth:<username>` namespace, NOT the plain-login counter — a stolen cookie cannot lock the real user out of normal login.
- **`record_failure` serialises the read-modify-write** with an atomic UPSERT inside `BEGIN IMMEDIATE` so concurrent failed logins cannot lose increments around the 5/10/15 threshold.

## Gotchas

- **DEFECT (stale citation):** `login_lockout.py`'s module docstring cites `:mod:\`mediaman.web.auth.rate_limit\`` as the existing per-IP in-memory limiter, but that module does not exist. The per-IP, in-memory (`dict[str, list[float]]` + `threading.Lock`) rate limiter lives at `mediaman.services.rate_limit` (`get_client_ip` is re-exported from its `__init__`; the limiter itself is `ActionRateLimiter`). Doc-only, no runtime effect — fix the reference.
- `admin_sessions.token` (the PK column) stores the SHA-256 token HASH, not the raw token — `create_session` writes the same hash into both `token` and `token_hash`. The `token` name and duplicate `token_hash` column are legacy; `schema_definition.py` carries a TODO to drop `token` and promote `token_hash` to PK once migration completes. Any code reading `admin_sessions.token` gets the hash.
- Pervasive lazy (function-body) imports exist ONLY to break real import cycles: `reauth` ↔ `password_hash` (`change_password` imports `REAUTH_LOCKOUT_PREFIX`; `verify_reauth_password` imports `authenticate`) and `session_store` ↔ `reauth` (destroy paths import `revoke_reauth_by_hash_in_tx`). Do not hoist them to module level.
- `password_policy` reads `data/common_passwords.txt` at runtime relative to `__file__` (`lru_cache`, first use). The ~835 KB data file is a hard packaging dependency — if it is not shipped alongside the module, `password_issues` raises on first call. It also exposes `_COMMON_PASSWORDS` via module `__getattr__` purely for test back-compat.
- Env-var knobs: `MEDIAMAN_FINGERPRINT_MODE` (off/loose/strict, default loose; unknown → loose) and `MEDIAMAN_REAUTH_WINDOW_SECONDS` (default 300, clamped to 30..3600; blank/non-numeric → default). No config-file equivalent.
- `_ensure_table` in `login_lockout` and `reauth` is a per-connection (`id(conn)`) backstop DDL, run at most once per connection — production schema comes from `init_db`; the `CREATE TABLE IF NOT EXISTS` is only for tests/legacy bare connections. The `id(conn)` set is intentionally unbounded-but-small.
- `_maybe_sweep_expired` stamps `_last_cleanup_at` when the sweep FINISHES (not when `validate_session` was entered) so a slow sweep can't defeat the once-per-minute throttle; the state is process-global under `_cleanup_lock`.
- `change_password` and `delete_user` use private Exception sentinels (`_UserVanished`, `_LastUser`) raised inside `with conn:` purely to force a rollback and map to `return False`; callers must never see these — the public contract is a `bool`.
- Best-effort cleanups (reauth revocation after `delete_user` / `destroy_all_sessions_for`, `last_used` refresh, counter cleanup after `change_password`) catch ONLY `sqlite3.Error` — a non-DB exception is treated as a real bug and propagates; on the security-critical eviction path (`_try_delete_session`) this is deliberate fail-closed behaviour.

## Extension points

- **New password rule:** add it inside `password_policy.password_issues` (and reflect it in `policy_summary`); it is the single strength gate for both CRUD and the CLI.
- **New session-destruction path:** it MUST revoke the matching reauth ticket in the same transaction — call `revoke_reauth_by_hash_in_tx` (transactional path) so a ticket cannot outlive its session.
- **New privilege-establishing action:** gate it on `has_recent_reauth`; grant the ticket with `grant_recent_reauth`. The ticket is bound to `sha256(session_token)`, so logout/rotation cascades it away.
- **New lockout band:** edit the descending `_LOCK_RULES` table in `login_lockout.py` (kept descending so the stricter lock wins when the count crosses both).
- **New route needing auth:** depend on a `middleware.py` function (`get_current_admin` / `get_optional_admin` / `resolve_page_session`) — never call `validate_session` directly, or the UA+IP fingerprint binding is skipped.

## Related

- HTTP consumers: [web-http](web-http.md) — routes depend on `middleware.py` predicates; `web/cookies.py` owns the `session_token` cookie whose raw value this module hashes at rest.
- Frontend surface: [web-frontend](web-frontend.md) — login / force-password-change / user-management pages.
- Client IP + per-IP limiter: `mediaman.services.rate_limit` (`get_client_ip`, `ActionRateLimiter`) — the fingerprint IP source.
- Cross-package deps: `mediaman.core.time` (canonical `+00:00` timestamps, strict fail-closed parser), `mediaman.core.audit.security_event_or_raise` (fail-closed audit), `mediaman.crypto.generate_session_token` (64-hex token), `mediaman.db` (`get_db`/`init_db`), `mediaman.core.email_validation.validate_email_address`, `mediaman.bootstrap.data_dir` + `mediaman.config` (cli only).
- Owned tables (schema in `mediaman.db.schema_definition`): `admin_users`, `admin_sessions`, `login_failures`, `reauth_tickets`.
