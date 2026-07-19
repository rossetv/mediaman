<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: platform

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

The three foundational packages every other layer stands on: `mediaman.core`
(Ring 0 — pure, stdlib-only helpers: clock, formatting, audit writers, backoff,
log-scrub, email/kind validation), `mediaman.crypto` (AES-256-GCM at-rest
encryption + HMAC-signed tokens), and `mediaman.db` (the sole `sqlite3.connect`
monopoly, connection lifecycle, and the single SCHEMA DDL). They form a strict
dependency ring — `core` imports nothing outward, `crypto`/`db` import only
`mediaman.core.time` — so any layer can import them without circular-import or
side-effect risk. All three are package facades imported pervasively across
`scanner/`, `services/`, `web/`, `bootstrap/` and `config.py`. There is no
`__main__` here; the process entrypoint (`mediaman.main` / `app_factory`) sits
above this module and wires it up: `bootstrap.db` calls `init_db(db_path)` to
open/create the DB and apply `SCHEMA`, then `bootstrap.crypto` uses that
connection for `is_canary_valid`; `core.*` are stateless and callable from
anywhere; web routes mint/verify tokens via `crypto/tokens.py`.

## Key files

| File | Role |
|------|------|
| `src/mediaman/core/__init__.py` | Ring 0 contract declaration: no I/O, no `mediaman` imports (except `mediaman.core`), no shared mutable state — safe to import from any layer without circular-import risk. |
| `src/mediaman/core/time.py` | Canonical UTC clock (`now_utc`/`now_iso`) and ISO-8601 parsers: `parse_iso_utc` (tolerant of trailing `Z`, >6-digit fractions, naive-as-UTC) and `parse_iso_strict_utc` (rejects malformed). The single monkeypatch point for the clock in tests. |
| `src/mediaman/core/format.py` | Byte/date/relative-time formatting (`format_bytes`, `format_day_month`, `relative_day_label`, `days_ago`, `ensure_tz`), audit-detail title/rk parsers (`_AUDIT_TITLE_RE`/`_AUDIT_RK_RE`, length-capped at `_AUDIT_TITLE_MAX_INPUT` against backtracking), media-type normalisation (`normalise_media_type`) and badge mapping (`media_type_badge`). One of two Ring-0 modules that import another (`mediaman.core.time`, alongside `audit.py`). |
| `src/mediaman/core/audit.py` | `audit_log` writers: `log_audit` (media actions), `security_event` (best-effort, self-commits, swallows errors), `security_event_or_raise` (fail-closed, caller owns the transaction). `_strip_audit_field` strips CR/LF/NUL from free-form fields to block log injection. |
| `src/mediaman/core/backoff.py` | `ExponentialBackoff`: capped delay with optional deterministic ±jitter derived from a `blake2b` digest of a caller-supplied seed (`deterministic_multiplier`), not `random`/`hash()`, so the rate-limit gate is stable across polls and processes. |
| `src/mediaman/core/scrub_filter.py` | `ScrubFilter` logging filter + module singleton (`install_root_filter`/`register_secret`): replaces registered secret substrings in log records; thread-safe, idempotent attach, `filter()` never raises out. |
| `src/mediaman/core/email_validation.py` | `validate_email_address`: stricter-than-`parseaddr` guard rejecting CR/LF/NUL header injection, whitespace, display-name syntax, multiple `@`, and >320 octets. |
| `src/mediaman/core/scheduled_action_kinds.py` | Domain string constants for `scheduled_actions.action`: `ACTION_PROTECTED_FOREVER` / `ACTION_SNOOZED` / `ACTION_SCHEDULED_DELETION`. |
| `src/mediaman/crypto/__init__.py` | Public crypto facade re-exporting `encrypt_value`/`decrypt_value`/`is_canary_valid` + all token `generate_*`/`validate_*` + `CryptoError`/`CryptoInputError`; deliberately re-exports `import hmac` (F401-suppressed) so tests can monkeypatch `mediaman.crypto.hmac.compare_digest`. |
| `src/mediaman/crypto/aes.py` | `encrypt_value`/`decrypt_value` (AES-256-GCM, `0x02` v2 prefix, 12-byte nonce, key-name-as-AAD row binding) and `is_canary_valid` (verifies the AES key can decrypt a stored canary; seeds it on first run; `on_failure` callback keeps the audit write out of `crypto/`). |
| `src/mediaman/crypto/_aes_key.py` | HKDF-SHA256 key derivation (`_derive_aes_key_hkdf`), per-install 16-byte salt load-or-create (`_load_or_create_salt`, `INSERT OR IGNORE` race-safe) with a single-entry lock-guarded cache (`_salt_cache`), `MEDIAMAN_SECRET_KEY` strength heuristic (`_is_secret_key_strong`), `CryptoError`/`CryptoInputError` classes, GCM/HKDF constants. |
| `src/mediaman/crypto/tokens.py` | HMAC-SHA256 signed tokens: per-purpose domain-separated subkeys (`_derive_token_subkey`, cached by `sha256(secret)`), `payload.signature` encoding, constant-time validation (`_validate_signed`) with pre-HMAC size caps and finite-non-bool-future `exp` check; per-purpose TypedDicts; opaque `generate_session_token`. |
| `src/mediaman/db/__init__.py` | `db` facade re-exporting connection lifecycle + scan/refresh job-run helpers; documents the `sqlite3.connect` monopoly and the crypto-depends-on-db-not-reverse rule. |
| `src/mediaman/db/connection.py` | `sqlite3` connection lifecycle: `_configure_connection` pragmas (WAL / `synchronous=NORMAL` / `busy_timeout` / `foreign_keys`), `init_db` (applies `SCHEMA` idempotently), `get_db` (owning-thread vs thread-local dual path), and generic `_start`/`_finish`/`_heartbeat` job-run helpers behind an allow-listed table name (`_JOB_RUN_TABLES`). |
| `src/mediaman/db/schema_definition.py` | The single `SCHEMA` DDL string: all tables (`settings`, `admin_users`/`admin_sessions`, `media_items`, `scheduled_actions`, `audit_log`, `subscribers`, `suggestions`, `download_notifications`/`recent_downloads`, throttle + token + job-run tables), indexes, and the append-only `audit_log` `BEFORE UPDATE`/`BEFORE DELETE` tamper-evidence triggers. |

## Invariants

- **Ring 0 (`core/`) is pure.** No I/O — no network/FS/DB/subprocess — and the only intra-`mediaman` import permitted is `mediaman.core.*`. Verified by grep: only `format.py` and `audit.py` import `mediaman.core.time`; nothing in `core/` imports outward.
- **Dependency ring.** `crypto/` and `db/` import ONLY `mediaman.core.time`. `crypto/` must never import `mediaman.db` and `db/` must never import `mediaman.crypto` — `crypto` operates on DB rows via a passed-in `sqlite3.Connection`, so it depends on `db`'s data, not its module (verified by grep).
- **`crypto/` is a leaf that must not import `mediaman.core.audit`.** `is_canary_valid` takes an `on_failure` callback so the security-event audit row is written by the caller (`bootstrap.crypto`), preserving the leaf invariant.
- **Single-worker design (CODE_GUIDELINES §1.12).** The salt cache (`_salt_cache`, single-entry), the token subkey cache (`_subkey_cache`, bounded at `_SUBKEY_CACHE_MAX_ENTRIES`, clear-on-overflow), and the connection registry (`_owning_conn`/`_owning_thread`/`_db_path`, mutated lock-free) all rely on one-process-one-truth.
- **Audit writers never `conn.commit()` — except `security_event`.** The audit row must land in the same transaction as the business mutation it records; `security_event_or_raise` propagates INSERT failures so the caller's transaction aborts. `security_event` self-commits because it is used for low-stakes events outside any wider transaction.
- **HKDF salt is exactly 16 bytes.** Any other decoded length (or undecodable base64) is treated as DB tampering and raises `CryptoError` rather than proceeding (`_load_or_create_salt`).
- **`audit_log` is append-only.** `BEFORE UPDATE` (`audit_log_no_update`) and `BEFORE DELETE` (`audit_log_no_delete`) triggers `RAISE(ABORT)`; dropping them is visible in `sqlite_master`. INSERT is unrestricted.
- **Every ciphertext is v2-prefixed (`0x02`) and row-bound.** The row's key name is passed as GCM AAD, so moving a ciphertext to another settings row fails authentication.
- **Tokens are per-purpose domain-separated.** A distinct HMAC subkey per purpose label; validation requires `exp` to be a finite, non-bool `int`/`float` strictly in the future (`bool` and `Infinity`/`NaN` explicitly rejected in `_validate_signed`).
- **Session/scheduled-action/admin-session tokens use hashed lookup, not HMAC.** `generate_session_token` is opaque random (`secrets.token_hex(32)`); there is deliberately no `validate_session_token`.

## Gotchas

- **Stale bytecode with no source.** `src/mediaman/core/__pycache__/url_safety.cpython-{312,314}.pyc` and `src/mediaman/crypto/__pycache__/_aes_migrate.cpython-312.pyc` exist but `url_safety.py` and `_aes_migrate.py` do NOT — dead compiled artefacts, not live modules. Do not cite them as code.
- **`backoff`: when `jitter > 0` a `seed` is MANDATORY or `delay()` raises `ValueError`.** Determinism is load-bearing — the gate is re-evaluated on every `/api/downloads` poll, so a fresh random roll would leak searches; `blake2b` (not `hash()`, which `PYTHONHASHSEED` salts) keeps the multiplier identical across processes.
- **`decrypt_value` has three distinct, documented failure classes** callers must distinguish: `CryptoInputError` (empty / >64 KiB / non-base64 input), `ValueError` (v2-shaped but no salt source — a config/programming error), `InvalidTag` (authentication failure). A non-v2 blob also raises `InvalidTag`.
- **Poll tokens carry a random nonce but have NO server-side replay defence** — no `poll_tokens_used` table, so any holder can replay a valid token until `exp`. Replay resistance is the 600s TTL only (documented on `PollTokenPayload`).
- **Download-token payload embeds the recipient email in reversible base64 JSON in the URL** — anyone with the token (forwarded email, log, referer leak) can read it back. Documented as acceptable for the current threat model, not a confidentiality boundary (`generate_download_token`).
- **Salt cache is intentionally disabled for `:memory:` DBs.** `_get_db_path` returns `""` for in-memory (empty `PRAGMA database_list` file column) and callers gate on `if cache_key:`, so tests get a fresh salt per connection.
- **`_start_job_run` issues `BEGIN IMMEDIATE` directly** (to grab a reserved write lock and avoid `SQLITE_BUSY`); calling `start_scan_run`/`start_refresh_run` inside an existing transaction raises `sqlite3.OperationalError` and leaves the outer transaction undefined.
- **`get_db` has a dual path.** If the calling thread == the bootstrap-registered owning thread it returns `_owning_conn`; otherwise a lazily-opened thread-local connection. Cross-thread access REQUIRES `init_db` with a real file path — a bare `set_connection` (no known `_db_path`) makes cross-thread `get_db` raise `RuntimeError`.
- **Job-run liveness is a heartbeat lease.** A run blocks new runs only while `finished_at IS NULL AND heartbeat_at` is within the 5-min stale window; a crashed run (stale heartbeat) silently stops blocking. Interval (`_JOB_HEARTBEAT_INTERVAL_SECONDS = 60`) is `assert`-checked `<` stale threshold (`_JOB_HEARTBEAT_STALE_SECONDS = 5 * 60`). `owner_id` is informational only, never compared.
- **Naive-datetime convention: naive == UTC.** `ensure_tz` (`format.py`) and `parse_iso_utc` (`time.py`) both treat naive datetimes as UTC — a prior `ensure_tz` treated them as LOCAL; the unified rule is naive=UTC. `parse_iso_strict_utc` rejects strings that `parse_iso_utc` would coerce.
- **`format.py` uses hardcoded English month-name tables** (`_ENGLISH_MONTH_FULL`/`_ENGLISH_MONTH_ABBR`, not `strftime %b`/`%B`) to dodge `LC_TIME` locale drift, and `format_day_month` builds the day component manually to avoid the `%-d` `ValueError` on Windows/BSD — required for deterministic newsletter output.
- **`crypto/__init__.py`'s `import hmac` is a deliberate re-export** (ruff F401 suppressed), NOT dead code: `tests/unit/web/test_poster.py` patches `mediaman.crypto.hmac.compare_digest` to prove `validate_poster_token` uses constant-time comparison. `tokens.py` has its own separate `import hmac`.
- **`admin_sessions.token` (PRIMARY KEY) and `scheduled_actions.token` (UNIQUE) are legacy columns** retained for rows not yet migrated to `token_hash`-only storage — schema `TODO` comments mark both for removal once the raw-token fallback lookup path is retired.

## Extension points

- **New token type:** add one purpose constant (`_TOKEN_PURPOSE_*`) plus one `generate_*`/`validate_*` pair in `tokens.py`, each wrapping `_encode_signed`/`_validate_signed`; add a per-purpose `TypedDict` when the shape is known.
- **New encrypted setting:** call `encrypt_value(plaintext, secret_key, conn=..., aad=key_name.encode())` and store the returned base64; decryption re-supplies the same key name as AAD.
- **New table / index / trigger:** add the `CREATE … IF NOT EXISTS` statement to the `SCHEMA` string in `schema_definition.py` (applied idempotently on every `init_db`); a fresh DB needs no migration step here.
- **New job-run kind:** extend `_JOB_RUN_TABLES` and add the matching `is_*`/`start_*`/`finish_*`/`heartbeat_*` shims in `connection.py` (they delegate to the generic `_*_job_run` helpers).
- **New Ring-0 helper:** add a stdlib-only module under `core/` — it may import `mediaman.core.*` and nothing else outward.
- **New runtime secret to redact:** call `mediaman.core.scrub_filter.register_secret(value)` after `install_root_filter` has run once at startup.

## Related

- Bootstrap wiring: `mediaman.bootstrap.db` (calls `init_db`), `mediaman.bootstrap.crypto` (calls `is_canary_valid` with an `on_failure` closure that writes a `security_event` row). Process entrypoint `mediaman.main` / `app_factory` sits above.
- Consumed by: pervasively across `mediaman.scanner`, `mediaman.services`, `mediaman.web`, `mediaman.bootstrap`, and `config.py`.
- External deps: `cryptography` (`AESGCM` from `hazmat.primitives.ciphers.aead`; `HKDF` from `hazmat.primitives.kdf.hkdf`; `hashes.SHA256`; `exceptions.InvalidTag`); stdlib `sqlite3` (the only package permitted to call `sqlite3.connect`); stdlib crypto/util `hmac`, `hashlib` (`sha256`/`blake2b`), `secrets`, `base64`, `binascii`; stdlib `json`, `logging`, `threading`, `socket`, `os`, `math`, `re`, `time`, `datetime`, `email.utils`, `contextlib`.
- SQLite tables: `schema_definition.SCHEMA` is the sole owner of the entire DDL — every table, index and trigger the whole app uses is defined here.
- Decisions: none yet.
- Specs: none yet.
