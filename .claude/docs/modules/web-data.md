<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: web-data

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

The persistence-and-validation layer for the web/API tier — the two sides of the
boundary between `mediaman.web.routes.*` and the SQLite database. `repository/`
owns every web-specific SQL operation (reads and writes) and returns frozen
dataclasses (or deliberately template-ready dicts) so route handlers orchestrate
rather than query. `models/` holds the Pydantic request-body models that harden
inbound API payloads — per-field length caps, CR/LF/NUL rejection, value
allowlists, `extra="forbid"` — before any value reaches the repository.

## Key files

### `repository/` — the read/write side

| File | Role |
|------|------|
| `src/mediaman/web/repository/__init__.py` | Package docstring only — names the tables the web layer owns (subscribers, suggestions, media_items/scheduled_actions/audit_log, delete_intents). No re-exports; callers import each submodule directly. |
| `src/mediaman/web/repository/library_api.py` | Transactional keep/delete/redownload writes for the library JSON API. `NotFound`, `MediaDeleteSnapshot`; `apply_keep_in_tx`, `snapshot_media_for_delete`, `finalise_delete_in_tx`, `record_redownload`. `BEGIN IMMEDIATE` + a single `with conn:` so the audit row and the mutation share one commit. |
| `src/mediaman/web/repository/kept.py` | Kept/protected queries and writes over `scheduled_actions` + `kept_shows`. `resolve_show_rating_key` closes an IDOR (drops the old `show_title` fallback); batched IN-clause fetches avoid N+1; `set_protected_state` does a batched update+insert. |
| `src/mediaman/web/repository/settings.py` | The settings-table encrypt-on-write / decrypt-on-read boundary. `load_settings` (raises `ConfigDecryptError` on decrypt failure vs missing), `write_settings` (atomic, optional audit in the same tx), `fetch_encrypted_key_set`; `SECRET_FIELDS` / `INTERNAL_KEYS` / `SECRET_PLACEHOLDER` / `SECRET_CLEAR_SENTINEL`. |
| `src/mediaman/web/repository/dashboard.py` | Dashboard reads over `scheduled_actions`/`media_items`/`audit_log`. Frozen dataclasses (`ScheduledDeletionRow`, `DeletedAuditRow`, …) + `fetch_*` funcs. Redownload scan bounded to `_REDOWNLOAD_WINDOW_DAYS` (365). |
| `src/mediaman/web/repository/delete_intents.py` | Delete-intent durability log (open before the Arr call, close after DB cleanup) plus the startup reconciler `reconcile_pending_delete_intents` (wired in `mediaman.app_factory`). Each reconcile is per-intent try/except so one failure never crashes boot. |
| `src/mediaman/web/repository/download.py` | Download-token single-use store (`claim_download_token` uses `INSERT OR IGNORE` for an atomic claim; `release_download_token`, `purge_expired_download_tokens`), the `recent_downloads` fallback cache, and suggestion-enrichment reads for the confirm page. |
| `src/mediaman/web/repository/poster.py` | Poster-route reads spanning two table-groups: Arr ids (`fetch_arr_ids`) and Plex credentials (`fetch_plex_credentials`). Returns the Plex token *ciphertext* + encrypted flag; decryption stays in the caller. |
| `src/mediaman/web/repository/recommended.py` | `suggestions`-table reads for the recommended route: `fetch_recommendations` (all) and `fetch_recommendations_page` (LIMIT/OFFSET). Returns `list[dict]`, not dataclasses — sanctioned template-feeding boundary (§9.5). |
| `src/mediaman/web/repository/search.py` | `ratings_cache` reads/writes (`fetch_ratings_cache` via a tuple-IN clause, `upsert_ratings_cache`) for OMDb rating enrichment of TMDB search results. |
| `src/mediaman/web/repository/subscribers.py` | `subscribers` CRUD. `try_add_subscriber` closes the concurrent-insert race with `BEGIN IMMEDIATE` + unique-index fallback, returning an `AddSubscriberOutcome` enum. |
| `src/mediaman/web/repository/library_query/__init__.py` | Re-export barrel for the library-page query, split when it crossed ~300 LOC into `_query`/`_display`/`_stats`. Preserves `from …library_query import X`. |
| `src/mediaman/web/repository/library_query/_query.py` | Core library query pipeline: constants (`VALID_SORTS`, `VALID_TYPES`, `*_SEASON_TYPES`, `MAX_SEARCH_TERM_LEN` = 200), CTE SQL builder, paginated count+SELECT, protection-map loader, and public `fetch_library`. |
| `src/mediaman/web/repository/library_query/_display.py` | Pure display-formatting helpers (`days_ago`, `type_css`, `protection_label`, `_shape_rows`) that turn raw rows into the 18-key dicts the `library.html` template consumes. No SQL. |
| `src/mediaman/web/repository/library_query/_stats.py` | Single-query COUNT/SUM helpers for the stats bar (`count_movies`, `count_tv_shows`, `count_anime_shows`, `count_stale`, `sum_total_size_bytes`). `count_stale` groups seasons per show to match display-item counting. |

### `models/` — the validate side

| File | Role |
|------|------|
| `src/mediaman/web/models/__init__.py` | Re-export barrel preserving the historical `from mediaman.web.models import X` surface, including private `_API_KEY_RE` and `_reject_crlf` used by callers/tests. Split from a former single `models.py`. |
| `src/mediaman/web/models/_common.py` | Shared primitives: length caps (`_MAX_PASSWORD_LEN`, `_MAX_EMAIL_LEN`, `_URL_MAX`, `_SECRET_MAX`, `_HOST_MAX`), regexes (`_EMAIL_RE`, `_API_KEY_RE`, `_CRLF_RE`), `VALID_KEEP_DURATIONS`, and validators `_reject_crlf` / `_validate_api_key`. No Pydantic models. |
| `src/mediaman/web/models/settings.py` | The full admin settings schema: `SettingsUpdate` (`extra=forbid`; per-field URL/secret/plain validators, timezone, `openai_model` allowlist, `library_sync_interval` bounds, nested `disk_thresholds` validator) plus the `DiskThresholds` helper and `_validate_url`. |
| `src/mediaman/web/models/auth.py` | `LoginRequest` (`extra=forbid`, length caps) and `KeepRequest` (`duration` constrained to `VALID_KEEP_DURATIONS`). |
| `src/mediaman/web/models/users.py` | User-management bodies: `CreateUserBody`, `UpdateEmailBody`, `ChangePasswordBody`, `ReauthBody`. |
| `src/mediaman/web/models/subscribers.py` | `SubscriberCreate`: normalises + regex-validates email, mirroring the subscribers route's hand-rolled validator. |

## Invariants

- **Repository purity.** Route handlers orchestrate; repositories do all SQL (`CODE_GUIDELINES.md` §2.7, forbidden pattern 1). Every repository function takes an explicit `sqlite3.Connection` as its first argument — no module acquires its own connection.
- **Atomic write + audit.** Multi-statement writes run inside a single `with conn:` block, usually opened with `BEGIN IMMEDIATE`, so the audit row and the business mutation land in one commit or roll back together (§9.7) — see `apply_keep_in_tx`, `finalise_delete_in_tx`, `write_settings`, `try_add_subscriber`.
- **Secrets never leave the repository as plaintext.** `settings.py` encrypts on write and decrypts on read at the storage boundary (§9.9); `poster.py` returns Plex token ciphertext + an encrypted flag and leaves decryption to the caller. `load_settings` raises `ConfigDecryptError` so callers can distinguish "decrypt failed" from "never set".
- **No user value is ever interpolated into SQL text.** Every dynamic IN-clause is built solely from `','.join('?' * len(...))` placeholders; user values always go through bind params. `WHERE`/`ORDER` fragments in `library_query` are constant identifiers or dict-resolved with a hardcoded default.
- **Hardened models reject CR/LF/NUL on every string field** (header-injection defence via `_reject_crlf`) and cap length per field; `extra="forbid"` blocks field-injection (e.g. `is_admin`, `unsubscribed`).
- **Search term is truncated before it is escaped.** The user-supplied LIKE term is cut to `MAX_SEARCH_TERM_LEN` (200) *before* escaping — the order is deliberate so a metacharacter at the cap boundary cannot split mid-escape.
- **Delete intents survive a crash.** `delete_intents` opens its intent row before the external Arr call and closes it after local cleanup, so a crash in between is reconciled at startup; each reconcile is isolated so one failure cannot crash boot.

## Gotchas

- **`DiskThresholds` is a dead/divergent model.** Defined in `models/settings.py` and re-exported from `models/__init__`, but it has NO consumer anywhere in `src/mediaman`. The live path uses `SettingsUpdate.disk_thresholds` — a `dict[str, Any]` with a nested `{path, threshold}` shape validated by `validate_disk_thresholds` — which diverges from `DiskThresholds`' flat `{path: int}` shape.
- **`apply_keep_in_tx` mislabels the audit action.** It writes the `action` argument into `scheduled_actions` but hardcodes the audit-log action to the literal `"snoozed"` regardless of `action`, so a `protected_forever` (keep-forever) decision is recorded in `audit_log` as a `snoozed` event.
- **Stale docstring lies about the cap.** `SettingsUpdate.validate_api_key_fields`'s docstring says "max 200 chars", but the enforced cap is 1024 (`_API_KEY_RE` = `^[\x20-\x7E]{1,1024}$`, and field-level `_SECRET_MAX` = 1024).
- **Template-feeding boundary leaks column names.** `recommended.py` and `library_query` deliberately return `list[dict]` rather than dataclasses (sanctioned by §9.5), so raw DB column names flow straight onto the Jinja templates — a column rename is a template-coupling change.
- **`write_settings` no-ops on empty/placeholder secrets.** For `SECRET_FIELDS`, an empty string and the `****` placeholder (`SECRET_PLACEHOLDER`) are treated as no-ops; deleting a stored secret requires the explicit `__CLEAR__` sentinel (`SECRET_CLEAR_SENTINEL`). Non-secret empty strings ARE written.
- **Two email validators are kept in sync by hand.** `_EMAIL_RE` in `models/_common.py` mirrors the subscribers route's hand-rolled `_validate_email`; `SubscriberCreate` lowercases+strips before matching. The correspondence is asserted only in comments.
- **`openai_model` is a hardcoded allowlist.** `validate_openai_model` restricts to `{"gpt-5.5", "gpt-5.4-mini"}`, so adding a model requires editing this validator, not just the settings `<select>`.

## Extension points

- **New request-body field:** add it to the relevant `models/` submodule; if callers use the flat `from mediaman.web.models import X` surface, add the re-export to `models/__init__.py`. `extra="forbid"` means the field must be declared or the request is rejected.
- **New allowed keep duration:** add it to `VALID_KEEP_DURATIONS` in `models/_common.py` — `KeepRequest` bounds against that set.
- **New OpenAI model:** extend the allowlist in `validate_openai_model` (`models/settings.py`); the `<select>` alone is not enough.
- **New secret field:** add the key to `SECRET_FIELDS` in `repository/settings.py` so it inherits encrypt-on-write / mask-on-read and the placeholder no-op semantics.
- **New web-owned query:** add a `repository/` submodule (import it directly — the package deliberately has no re-export barrel), take `sqlite3.Connection` as the first arg, and return a frozen dataclass unless the template boundary sanctions a dict (§9.5).
- **New library sort/type:** extend `VALID_SORTS` / `VALID_TYPES` in `library_query/_query.py` — unknown values fall through to the hardcoded default rather than being interpolated.

## Related

- Consumers (route handlers): `src/mediaman/web/routes/` (dashboard, download, kept, kept_show, library, library_api, poster, recommended, search, settings, subscribers, users) — see [web-http](web-http.md)
- Startup wiring: `mediaman.app_factory` calls `reconcile_pending_delete_intents` at boot — see [app-entry](app-entry.md)
- Crypto boundary: `mediaman.crypto` (`encrypt_value`, `decrypt_value`, `CryptoInputError`) and `cryptography.exceptions.InvalidTag`
- Audit: `mediaman.core.audit` (`log_audit`, `security_event_or_raise`)
- Settings-decrypt error type: `mediaman.services.infra` (`ConfigDecryptError`, `get_int_setting`)
