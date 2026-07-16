<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../INDEX.md)

# Configuration

<!-- One concern: how mediaman is configured — bootstrap env vars, the
DB-backed `settings` table, and where each value is validated. Deployment
mechanics (image, compose, CI) live in DEPLOYMENT.md, not here. -->

There are two independent configuration surfaces that must not be confused:

1. **Bootstrap env vars** — read once at process start (or lazily cached
   thereafter), never stored in the DB. `src/mediaman/config.py`'s frozen
   `Config` dataclass covers five of them; the rest are read directly from
   `os.environ` at their point of use (see Facts below).
2. **DB-backed settings** — the `settings` SQLite table (service
   credentials, URLs, scheduling). Never read from env at runtime; managed
   exclusively through the admin Settings UI / `PUT /api/settings`.

## Facts — bootstrap env vars (`Config` dataclass)

| Var | Field | Default | Validated by |
|-----|-------|---------|--------------|
| `MEDIAMAN_SECRET_KEY` | `secret_key` | none — **required** | `load_config()`: presence + `crypto._aes_key._is_secret_key_strong` entropy check (64+ hex chars ≥10 unique, or 43+ URL-safe base64 decoding to 32+ bytes ≥18 unique) |
| `MEDIAMAN_PORT` | `port` | `8282` | `load_config()`: `int()` parse, range 1–65535 |
| `MEDIAMAN_DATA_DIR` | `data_dir` | `/data` | `load_config()`: non-empty after `.strip()` |
| `MEDIAMAN_BIND_HOST` | `bind_host` | `""` (unset) | Not validated here — empty string means "let `main._resolve_bind_host()` decide" (`0.0.0.0` in-container, `127.0.0.1` bare metal) |
| `MEDIAMAN_TRUSTED_PROXIES` | `trusted_proxies` | `""` | Not validated in `config.py` — sanitised later by `bootstrap.validators.sanitise_trusted_proxies` (drops wildcard tokens with a CRITICAL log, drops non-CIDR entries with a WARNING) before being passed to uvicorn's `proxy_headers` |

`load_config()` raises `ConfigError` on any bootstrap failure — `main.cli_main()` catches it for a clean CLI exit before uvicorn is imported. `MEDIAMAN_SECRET_KEY` is the only variable with no default; every other `Config` field has one.

## Facts — other env vars (read directly, NOT on `Config`)

These are consumed at their point of use rather than threaded through `Config`. None of them abort startup on a bad value — each falls back to a safe default and (mostly) logs a warning.

| Var | Default | Read/validated in |
|-----|---------|--------------------|
| `MEDIAMAN_DELETE_ROOTS` | `""` (deletions refused) | `scanner/repository/settings.py` (`read_delete_allowed_roots_setting`, env is the fallback when the `delete_allowed_roots` DB row is empty) and `services/infra/path_safety.py` (`disk_usage_allowed_roots`); both parse via `services.infra.path_safety.parse_delete_roots_env` (`:` canonical separator, `,` deprecated) |
| `MEDIAMAN_MAX_REQUEST_BYTES` | `8388608` (8 MiB) | `web/middleware/body_size.py` (`_resolve_max_request_bytes`); non-integer or negative falls back to the default with a WARNING; `<= 0` after parsing means unlimited |
| `MEDIAMAN_ALLOWED_HOSTS` | `""` (any host) | `web/__init__.py` (`_parse_allowed_hosts`, feeds Starlette's `TrustedHostMiddleware`); logs a WARNING at startup when left unset |
| `MEDIAMAN_HSTS_ENABLED` | `false` | `web/middleware/security_headers.py`; must be exactly `"true"` (case-insensitive) to emit HSTS, and only when the request already resolves as HTTPS |
| `MEDIAMAN_HSTS_PRELOAD` | `false` | `web/middleware/security_headers.py`; only meaningful when HSTS is enabled |
| `MEDIAMAN_FORCE_SECURE_COOKIES` | unset (auto-detect) | `web/cookies.py` (`_secure_cookie_override`, `@lru_cache`) and `web/middleware/security_headers.py`; `"false"` is a hard override that also disables HSTS regardless of `MEDIAMAN_HSTS_ENABLED` |
| `MEDIAMAN_EAGER_APP` | unset | `main.py`; `"1"` instantiates the module-level ASGI `app` at import time (only needed for `uvicorn mediaman.main:app`-style entrypoints) |
| `MEDIAMAN_STRICT_EGRESS` | `false` | `services/infra/_url_safety_blocks.py` (`_strict_egress_enabled`); `true` additionally blocks loopback/RFC1918 outbound targets |
| `MEDIAMAN_CLOUDFLARE_PROXIES` | `""` | `services/rate_limit/ip_resolver.py` (`_parse_proxy_env`); separate allowlist from `MEDIAMAN_TRUSTED_PROXIES` — a literal `*` triggers refuse-all with a CRITICAL log |
| `MEDIAMAN_MEDIA_PATH` | `/media` | `services/infra/settings_reader.py` (`get_media_path`); read at call time, not cached, so tests can override mid-process |
| `MEDIAMAN_WORKERS` / `UVICORN_WORKERS` / `WORKERS` | unset | `bootstrap/validators.py` (`enforce_single_worker`); any of the three parsing to an integer `> 1` raises `RuntimeError` at startup — an unparseable value (e.g. `"auto"`) logs a WARNING and is treated as unset |
| `TZ` | `UTC` | Consumed by the container/Python runtime for scheduler timezone context; not read by mediaman's own code as `TZ` — the scheduler's actual timezone is the DB-backed `scan_timezone` setting, not this var |

**`.env.example` gap**: it documents only `MEDIAMAN_SECRET_KEY`, `MEDIAMAN_PORT`, `MEDIAMAN_BIND_HOST`, `MEDIAMAN_DATA_DIR`, `TZ`, `MEDIAMAN_TRUSTED_PROXIES`, and `MEDIAMAN_DELETE_ROOTS`. Every var in the table above it omits — `MEDIAMAN_MAX_REQUEST_BYTES`, `MEDIAMAN_ALLOWED_HOSTS`, `MEDIAMAN_HSTS_ENABLED`, `MEDIAMAN_HSTS_PRELOAD`, `MEDIAMAN_FORCE_SECURE_COOKIES`, `MEDIAMAN_EAGER_APP`, `MEDIAMAN_STRICT_EGRESS`, `MEDIAMAN_CLOUDFLARE_PROXIES`, `MEDIAMAN_MEDIA_PATH`, `MEDIAMAN_WORKERS`/`UVICORN_WORKERS`/`WORKERS` — are documented only in `README.md`'s Configuration table (which does list most of them) or not documented for the operator at all (`MEDIAMAN_STRICT_EGRESS`, `MEDIAMAN_CLOUDFLARE_PROXIES`, `MEDIAMAN_MEDIA_PATH` appear in neither `.env.example` nor `README.md`).

## Facts — DB-backed `settings` table

| Item | Value | Source |
|------|-------|--------|
| Schema | `key TEXT PRIMARY KEY, value TEXT NOT NULL, encrypted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL` | `db/schema_definition.py` (`SCHEMA`, `-- === Settings / encrypted KV ===`) |
| Non-secret value encoding | `json.dumps(...)` for `list`/`dict`/`bool`, else `str(value)`; decoded back with `json.loads` (falls through to the raw string on decode failure) | `web/repository/settings.py` (`write_settings`), `services/infra/settings_reader.py` (`get_setting`) |
| Secret value encoding | AES-256-GCM ciphertext (see Encrypted secrets below), `encrypted=1` | `web/repository/settings.py` (`SECRET_FIELDS`) |
| Secret field names | `plex_token`, `sonarr_api_key`, `radarr_api_key`, `nzbget_password`, `mailgun_api_key`, `tmdb_api_key`, `tmdb_read_token`, `openai_api_key`, `omdb_api_key` | `web/repository/settings.py` (`SECRET_FIELDS`) |
| Internal plumbing rows (never shown in the UI) | `aes_kdf_salt`, `aes_kdf_canary` | `web/repository/settings.py` (`INTERNAL_KEYS`) |
| Full writable key set | `SECRET_FIELDS` ∪ every URL/scheduling/feature-flag key the settings page persists | `web/routes/settings/secrets.py` (`ALL_KEYS`) |
| Keys requiring recent reauth to write | Every URL field, `nzbget_username`, `mailgun_domain`, `mailgun_from_address`, `base_url`, plus all of `SECRET_FIELDS` | `web/routes/settings/secrets.py` (`SENSITIVE_KEYS`) |

## Encrypted secrets: AES, `__CLEAR__`, and `****`

| Concept | Behaviour | Source |
|---------|-----------|--------|
| Cipher | AES-256-GCM, key derived via HKDF-SHA256 from `MEDIAMAN_SECRET_KEY` + a per-install 16-byte salt (`aes_kdf_salt` row); ciphertext is `0x02`-prefixed (v2), base64url-encoded | `crypto/aes.py` (`encrypt_value`/`decrypt_value`), `crypto/_aes_key.py` (`_derive_aes_key_hkdf`) |
| Row binding | The settings-table key name is passed as GCM AAD on every encrypt/decrypt, so a ciphertext moved to another row fails authentication | `crypto/aes.py` (`encrypt_value` call sites pass `aad=key.encode()`) |
| Canary | `aes_kdf_canary` row proves the configured `MEDIAMAN_SECRET_KEY` can still decrypt existing secrets; checked at boot, gates scheduler startup (fail-closed) but not the web UI | `crypto/aes.py` (`is_canary_valid`), `bootstrap/crypto.py` (`bootstrap_crypto`) |
| `SECRET_PLACEHOLDER = "****"` | Sentinel the UI/API emit for an already-configured secret; sending `"****"` back on a write is a no-op (preserves the stored value); GET never decrypts just to mask — it reads the `encrypted` flag instead | `web/repository/settings.py` (`SECRET_PLACEHOLDER`), `web/routes/settings/secrets.py` (`mask_encrypted_keys`, used by `api_get_settings`) |
| `SECRET_CLEAR_SENTINEL = "__CLEAR__"` | Explicit "delete this secret" sentinel — the only way to remove a stored secret via the API (an empty string is a no-op, not a delete) | `web/repository/settings.py` (`SECRET_CLEAR_SENTINEL`, `write_settings`) |
| Decrypt failure handling | `get_setting`/`load_settings` distinguish "never set" (returns default / key absent) from "present but undecryptable" (`ConfigDecryptError`, surfaced to the caller rather than silently substituted) — a rotated `MEDIAMAN_SECRET_KEY` must not look like "no secrets configured" | `services/infra/settings_reader.py` (`get_setting`, `ConfigDecryptError`), `web/repository/settings.py` (`load_settings`) |

## Which settings are validated where

| Setting(s) | Validated in | When | Notes |
|------------|---------------|------|-------|
| `MEDIAMAN_SECRET_KEY`, `MEDIAMAN_PORT`, `MEDIAMAN_DATA_DIR` | `config.py` (`load_config`) | Process start | Fatal `ConfigError` on failure |
| Every `SettingsUpdate` field (URLs, secrets, scheduling, feature flags) | `web/models/settings.py` (`SettingsUpdate` + its `@field_validator`s) | `PUT /api/settings` request parse | Pydantic `extra="forbid"`; CR/LF rejection on every string field; length caps per field |
| URL fields — SSRF/reachability | `web/routes/settings/core.py` (`validate_url_fields`, calling `services.infra.is_safe_outbound_url`) | `PUT /api/settings`, after Pydantic parse, before persistence | Separate from `SettingsUpdate`'s own `_validate_url` (scheme/host/length only) because the SSRF check needs a live DNS resolution `SettingsUpdate` can't perform in a pure field validator |
| `openai_model` | `web/models/settings.py` (`validate_openai_model`) | `PUT /api/settings` | Hardcoded allowlist `{"gpt-5.5", "gpt-5.4-mini"}` — defence-in-depth behind the settings-page `<select>` |
| `scan_day`, `scan_time`, `scan_timezone`, `library_sync_interval` | **Twice**: `web/models/settings.py` (shape/bounds, at save time) AND `bootstrap/validators.py` (`validate_scan_day`/`validate_scan_time`/`validate_scan_timezone`/`validate_sync_interval`, via `scan_jobs._read_scheduler_config`) | Save time, and again every time the scheduler (re)starts | The scheduler re-validates fresh from the DB rather than trusting a prior save — a malformed value raises inside `bootstrap_scheduling`'s cold-start handler rather than reaching APScheduler |
| `disk_thresholds` | `web/models/settings.py` (`validate_disk_thresholds`) | `PUT /api/settings` | Nested `{lib_id: {"path": str, "threshold": 0–100}}`; empty values permitted (library selected, path not yet typed) |
| `MEDIAMAN_TRUSTED_PROXIES` | `bootstrap/validators.py` (`sanitise_trusted_proxies`) | Process start, before uvicorn's `proxy_headers` is enabled | Wildcards dropped with CRITICAL log, non-CIDR entries dropped with WARNING |
| `MEDIAMAN_WORKERS` / `UVICORN_WORKERS` / `WORKERS` | `bootstrap/validators.py` (`enforce_single_worker`) | Process start (`main.cli_main`) | Only an integer `> 1` raises; unparseable values are logged and ignored |
| `MEDIAMAN_MAX_REQUEST_BYTES` | `web/middleware/body_size.py` (`_resolve_max_request_bytes`) | Lazily, first request after process start | Soft-fails to the 8 MiB default on parse error or negative value |
| `MEDIAMAN_ALLOWED_HOSTS` | `web/__init__.py` (`_parse_allowed_hosts`) | Process start (`register_security_middleware`) | No validation beyond comma-split; an unset value is accepted (`["*"]`) with a logged warning, not rejected |
| `MEDIAMAN_DELETE_ROOTS` / `delete_allowed_roots` DB row | `services/infra/path_safety.py` (`parse_delete_roots_env`) + `services/infra/storage/_delete_roots.py` (`_FORBIDDEN_ROOTS` check at deletion time) | Read time (path parsing) and again at each deletion (forbidden-root + symlink checks) | Empty/unparseable input fails closed — deletions refused, not defaulted to a guess |

## Failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| App exits immediately with a `MEDIAMAN_SECRET_KEY` error at boot | Missing, or fails `_is_secret_key_strong` (too short / low unique-char count) | Generate with `python -c "import secrets; print(secrets.token_hex(32))"` and set it in `.env` / the container env |
| `/readyz` returns 503 with a crypto-canary reason | `MEDIAMAN_SECRET_KEY` changed since encrypted settings were saved — `is_canary_valid` fails | Re-enter every encrypted setting (API keys, tokens) on the Settings page under the new key, or restore the old key |
| A saved API key / token silently "disappears" from the Settings page | `ConfigDecryptError` on a stale key would previously have looked like "never configured"; current code raises instead and the settings page surfaces the decrypt-failure banner | Confirm `MEDIAMAN_SECRET_KEY` matches the key that encrypted the value; re-enter the secret if the key was rotated |
| Sending an empty string to clear a stored secret does nothing | Empty string is treated as a no-op write (`write_settings`), not a delete | Send the `__CLEAR__` sentinel (`SECRET_CLEAR_SENTINEL`) instead |
| `PUT /api/settings` 403 `reauth_required` | The payload touches a `SENSITIVE_KEYS` entry without a recent-reauth ticket | Re-authenticate (password re-entry) then retry the write |
| Scheduler fails to start with a `scan_time`/`scan_day`/`scan_timezone`/`library_sync_interval` error even though the Settings page accepted the value | The web-side `SettingsUpdate` validator and the scheduler-side `bootstrap/validators.py` check are not identical in every edge case | Compare the two validator sets; fix the DB row directly or re-save via the Settings page with a value both layers accept |
| Deletions always refused (`delete_allowed_roots is not configured`) | Neither the `delete_allowed_roots` DB row nor `MEDIAMAN_DELETE_ROOTS` produced any roots | Set `MEDIAMAN_DELETE_ROOTS` (colon-separated) or configure roots via the admin UI; check logs for the specific parse failure |
| A reverse-proxy `X-Forwarded-For` header is ignored | `MEDIAMAN_TRUSTED_PROXIES` unset, or reduced to empty by `sanitise_trusted_proxies` (wildcard/non-CIDR entries dropped) | Set it to the actual proxy IP/CIDR — never a wildcard |

## Related

- [DEPLOYMENT.md](DEPLOYMENT.md) — image build, compose, CI; `.env.example` is the operator starter file referenced there
- [modules/app-entry.md](modules/app-entry.md) — `Config`, `load_config()`, startup order, single-worker enforcement
- [modules/platform.md](modules/platform.md) — AES-256-GCM encryption, HKDF salt, canary, HMAC tokens
- [modules/web-data.md](modules/web-data.md) — `settings` repository (`load_settings`/`write_settings`), `SettingsUpdate` model, the `DiskThresholds` dead-model gotcha
- [modules/services-infra.md](modules/services-infra.md) — `get_setting`/`get_string_setting` family, `ConfigDecryptError`, path-safety allowlist parsing
- [modules/web-middleware.md](modules/web-middleware.md) — `MEDIAMAN_MAX_REQUEST_BYTES`, `MEDIAMAN_ALLOWED_HOSTS`, `MEDIAMAN_HSTS_*`, `MEDIAMAN_FORCE_SECURE_COOKIES` consumers
- `README.md` (`## Configuration`) — operator-facing env-var table
- `.env.example` — starter file for bootstrap env vars (see the gap noted above)
