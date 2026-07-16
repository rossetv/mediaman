<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: scanner

## Purpose

Background Plex-library scanner and deletion engine. It fetches every configured
library from Plex, upserts rows into `media_items`, evaluates each movie / TV-season for
deletion eligibility (age + inactivity), schedules eligible items into `scheduled_actions`
with an HMAC keep-token, executes deletions once the grace period elapses (`rm` on disk +
audit + best-effort Radarr/Sonarr unmonitor), prunes orphaned rows, and drives post-scan
follow-ups (AI recommendation refresh + deletion-warning newsletter). It runs as an
in-process APScheduler weekly job plus a lightweight ~30-min library-sync. The sibling
`services/scheduled_actions` package is the shared service layer behind the web "keep"
routes: it validates keep-tokens and applies snooze / keep-forever mutations that cancel a
pending deletion. Primary entrypoint `ScanEngine.run_scan` (`engine.py`), reached via
`run_scan_from_db` (`runner.py`) and wired as the weekly job by `bootstrap/scan_jobs.py`
(`_run_scheduled_scan`); secondaries `ScanEngine.sync_library` (via `run_library_sync`),
`start_scheduler`/`stop_scheduler` (`scheduler.py`), and `recover_stuck_deletions` at boot.

## Key files

| File | Role |
|------|------|
| `src/mediaman/scanner/engine.py` | `ScanEngine` orchestrator: `run_scan` (scan → orphan-cleanup → deletions → follow-ups) and `sync_library`; owns the per-library commit boundary (`_scan_all_libraries`) and `_resolve_added_at` (Arr date > Plex `added_at`, never `updated_at`). |
| `src/mediaman/scanner/_scan_library.py` | Per-library scan body lifted out of engine to keep it under 500 lines: `scan_movie_library` / `scan_tv_library` / `scan_items`, the `ScanDecision` enum, and the batched per-library guard sets (protected / already_scheduled / kept-show). |
| `src/mediaman/scanner/fetch.py` | Plex network-read layer: `PlexFetcher` + `PlexItemFetch` / `FetchedLibrary` dataclasses. Two-phase fetch; **fails closed** on a watch-history error (excludes the item but records its key in `skipped_keys` so orphan removal cannot prune it). |
| `src/mediaman/scanner/_eligibility.py` | Pure predicates `is_old_enough` (min_age_days) and `is_inactive` (never-watched, or last watch older than inactivity_days; an all-null-timestamp history is treated as watched-recently = fail-safe). |
| `src/mediaman/scanner/phases/evaluate.py` | `evaluate_item` — the single age + inactivity rule shared by movies and TV seasons; returns `'skip'` or `'schedule_deletion'`. |
| `src/mediaman/scanner/phases/upsert.py` | DB-mutation phase: `upsert_item` and `schedule_deletion` (the 2-step INSERT → `lastrowid` → HMAC-token → UPDATE that stores only the token hash; returns `'skipped'` on `IntegrityError`). |
| `src/mediaman/scanner/phases/delete.py` | `remove_orphans` + the fail-closed orphan guard (per-library two-consecutive-suspicious-scan confirmation via the `orphan_guard_pending` setting) before atomically deleting `scheduled_actions` + `media_items` rows. |
| `src/mediaman/scanner/deletions.py` | `DeletionExecutor`: two-phase on-disk delete (mark `'deleting'` + commit **before** `rm`), `recover_stuck_deletions` crash reconciliation, allowlist fail-closed, and post-commit best-effort *arr unmonitor. |
| `src/mediaman/scanner/arr_dates.py` | `ArrDateCache` — lazily-built normalised-path → Arr-download-date lookup from Radarr/Sonarr; `normalise_path` strips container root prefixes for cross-container matching. |
| `src/mediaman/scanner/runner.py` | Wiring: `run_scan_from_db` / `run_library_sync` build a `ScanEngine` from settings; module-level Plex-client cache keyed on a settings fingerprint; disk-threshold library filtering; `ScanSummary` TypedDict. |
| `src/mediaman/scanner/scheduler.py` | APScheduler setup: weekly scan + interval sync + maintenance jobs (`cleanup_recent_downloads`, `trigger_pending_searches`, `reconcile_stranded_throttle`); tracks/closes job DB connections; misfire-grace + coalesce. |
| `src/mediaman/scanner/repository/media_items.py` | Pure SQL on `media_items`: `upsert_media_item` (ON CONFLICT refreshes all metadata), monotonic `update_last_watched` (`MAX`), count / fetch ids for the orphan guard, chunked `delete_media_items` (opens no transaction). |
| `src/mediaman/scanner/repository/scheduled_actions.py` | Deletion-lifecycle SQL: `DeletionRow`, `fetch_stuck_deletions` / `fetch_pending_deletions`, `mark_delete_status`, delete helpers, `count_pending_deletions` / `clear_pending_deletions` (audit-in-transaction); re-exports protection names. |
| `src/mediaman/scanner/repository/_protection.py` | Protection / snooze reads: per-item `is_protected` / `is_already_scheduled` / `is_show_kept` plus the batched set-building `fetch_protected_media_ids` / `fetch_already_scheduled_media_ids` / `fetch_kept_show_keys` used by the hot loop; snooze cleanup helpers. |
| `src/mediaman/scanner/repository/settings.py` | `read_setting` and `read_delete_allowed_roots_setting` (DB-beats-env precedence; empty = fail-closed refuse-all-deletions). |
| `src/mediaman/scanner/repository/audit.py` | Paginated `audit_log` reads (`AuditRow`) for the history page/API; security (`sec:*`) vs media-action query paths and the UI filter map. |
| `src/mediaman/scanner/repository/__init__.py` | Re-export barrel for the repository sub-package (pure SQL; must not import crypto or fetch/deletions). |
| `src/mediaman/scanner/__init__.py` | Package docstring declaring allowed dependencies and the forbidden-import rule (no `mediaman.web`). |
| `src/mediaman/services/scheduled_actions/_types.py` | Domain types: `KeepDecision`, `VerifiedKeepAction` dataclass (full `scheduled_actions` + joined `media_items` columns), and pure `resolve_keep_decision` (duration → action / execute_at). |
| `src/mediaman/services/scheduled_actions/_lookup.py` | `token_hash` (SHA-256), `lookup_verified_action` (validate HMAC then look up by token_hash + payload cross-check), `mark_token_consumed` (INSERT OR IGNORE replay guard), `is_keep_token_consumed`. |
| `src/mediaman/services/scheduled_actions/_mutations.py` | `parse_execute_at` / `is_pending_unexpired` predicates and the guarded UPDATEs `apply_keep_snooze` / `apply_keep_forever` (atomic action + delete_status + token_used + execute_at guards; rowcount 0 ⇒ caller returns 409). |
| `src/mediaman/services/scheduled_actions/_display.py` | Pure display formatters `format_expiry` and `format_added_display` (no DB access). |
| `src/mediaman/services/scheduled_actions/__init__.py` | Re-export barrel for the keep-token service; documents the never-commit contract (route owns the transaction). |

## Invariants

- **No upward import.** The scanner MUST NOT import `mediaman.web` — it is a background service and stays independent of the HTTP layer (stated in `scanner/__init__.py`).
- **Import-cycle direction.** `repository` imports nothing from `fetch` / `deletions`; `deletions` may import `repository`; `fetch` may import `repository`; `engine` orchestrates all three.
- **The repository package is pure SQL** — no crypto imports. HMAC keep-token generation lives only in `phases.upsert.schedule_deletion`.
- **Per-library commit boundary.** `engine._scan_all_libraries` commits after each library, so a SIGKILL mid-scan can only roll back the in-flight library, never an earlier successful upsert.
- **Network I/O never overlaps an open SQLite write transaction** — strict two-phase fetch (no DB) → write (one commit per library).
- **Fetch fails closed on a watch-history error** — the item is excluded from evaluation **and** its key goes into `skipped_keys`, which the engine unions into `seen_keys` so orphan removal never prunes a still-present item (R7-H1).
- **Deletion fails closed when `delete_allowed_roots` is unconfigured** — every pending deletion is refused (checked once before the loop).
- **Two-phase delete.** Mark the row `'deleting'` and commit **before** the on-disk `rm`; `recover_stuck_deletions` reconciles crashed rows — file present → `pending`; file gone AND path within roots → `deleted`; file gone but path outside roots → `pending` (never fabricate a `'deleted'` audit).
- **Orphan removal is fail-closed.** A suspicious item-count drop needs two consecutive suspicious scans of that same library (`orphan_guard_pending`) before pruning; pruning removes only tracking rows, never media files.
- **`update_last_watched` is monotonic** via SQL `MAX(...)` — the watch clock is only advanced, never rewound (a rewind would re-qualify an item for deletion).
- **Keep-tokens are stored only as SHA-256 hashes** — `schedule_deletion` nulls the raw token column after computing the hash.
- **`dry_run` semantics.** Upserts still run (catalogue stays current) but **no** deletion-state writes: `schedule_deletion`, orphan removal, on-disk `rm`, snooze cleanup, newsletter and recommendation refresh are all skipped; a `dry_run_skip` audit row is written instead.
- **`added_at` eligibility source.** Arr download date preferred, then Plex `added_at`; Plex `updated_at` is deliberately **never** used (a metadata refresh would mask eligibility) and must agree with the persisted `media_items.added_at`.
- **`services/scheduled_actions` helpers never call `conn.commit()`** — the transaction boundary belongs to the route, so one HTTP request = one transaction.
- **Protection boundary is inclusive** (`execute_at >= now` in `is_protected` / `fetch_protected_media_ids`) while snooze cleanup uses strict `<`, so an exact-now snooze is still active and the two never overlap.

## Gotchas

- **Batched guard sets.** `_scan_library` builds protected / already_scheduled / kept-show sets once per library instead of per-item SELECTs; `already_scheduled` is **mutated mid-loop** when an item is freshly scheduled, to mirror the old uncommitted-view behaviour of the per-item query.
- **`scan_items` catches only `KeyError` / `TypeError` / `ValueError` / `RuntimeError` / `sqlite3.Error`** per item (malformed Plex data / transient DB); genuine bugs (`AttributeError`, `NameError`, …) deliberately propagate rather than being masked as an item error.
- **`_resolve_added_at`.** An Arr cache hit with an unparseable date used to silently become `datetime.now(UTC)`, granting permanent deletion immunity — it now logs and falls through to the Plex `added_at` chain.
- **`is_inactive`.** Empty history ⇒ inactive ("never watched"), but history with entries that are **all** missing `viewed_at` ⇒ **not** inactive (fail-safe: avoids deleting off an unusable history).
- **`run_library_sync` builds a `dry_run=True` engine with min_age / inactivity / grace all 0** — it only upserts + removes orphans, never evaluates for deletion; it also fires download-completion notifications.
- **Plex client is cached at module scope in `runner.py`** keyed on a fingerprint of the raw `plex_url` + raw **encrypted** `plex_token` (no decrypt needed); the cache is cleared whenever settings change or Plex is unconfigured.
- **Scheduler `misfire_grace_time=3600s` + `coalesce=True`.** An outage longer than 60 minutes **drops** the missed weekly-scan tick instead of catching up; a routine restart still fires once the process returns.
- **`schedule_deletion` returns `'skipped'` (not an error)** when the partial unique index raises `IntegrityError` — a concurrent scan already inserted an active deletion for the same media_id.
- **`fetch_stuck_deletions` swallows `sqlite3.OperationalError`** and returns `[]` when the `delete_status` column has not been migrated (older DB schema).
- **`read_delete_allowed_roots_setting` uses unusual precedence** — the DB settings row **wins** over the `MEDIAMAN_DELETE_ROOTS` env var (the admin-UI value must not be overridden by a stale container env).
- **`_coerce_lib_ids` raises `ValueError` on a malformed library id** (e.g. `'all'`); callers catch it per-library and skip only that library's orphan removal rather than aborting the whole scan.
- **`normalise_path` strips the first path component** and additionally the generic roots `data` / `media` / `share` for cross-container path matching between Plex/Radarr/Sonarr mounts.
- **`DeletionRow` queries (pending vs stuck) select the same uniform column set** even though each path ignores some columns (`action` on the pending path, the *arr ids on the stuck path).
- **`is_show_kept` keeps a legacy "ask + clean" contract** (reads, then sweeps an expired row on the not-kept path); the batched TV-scan path instead sweeps once up front via `cleanup_expired_show_snoozes`, then uses the pure `fetch_kept_show_keys` set.
- **`cleanup_expired_snoozes` is scoped to `token_used = 0` only.** A consumed (`token_used = 1`) expired snooze is the sole re-entry signal read by `has_expired_snooze` and must be preserved so a re-scheduled item is flagged `is_reentry`.
- **Stale compiled artefacts with no source file exist** — `scanner/repository/__pycache__/library_query.cpython-312.pyc` and `scanner/__pycache__/_post_scan.cpython-312.pyc` are dead `.pyc` leftovers from removed modules (cleanup candidates, not live code).
- **Stale docstring in `_resolve_added_at`.** The method's docstring still claims Plex `updated_at` "is used only as a last resort"; the implementation body (and its inline comment) never reads `updated_at` — the docstring contradicts the code and is a documentation defect.

## Extension points

- **New deletion-eligibility rule** → change the single `evaluate_item` (`phases/evaluate.py`), shared by movies and TV seasons, and/or the pure predicates in `_eligibility.py` — do not fork per-type logic.
- **New per-library scan behaviour** → `_scan_library.py` (`scan_movie_library` / `scan_tv_library` / `scan_items`), keeping the batched guard-set pattern rather than per-item SELECTs.
- **New scheduled/maintenance job** → register it in `scheduler.py` (`start_scheduler`), tracking + closing its DB connection like the existing jobs.
- **New keep mutation or keep-route service** → `services/scheduled_actions` (add a guarded UPDATE to `_mutations.py`, a type to `_types.py`); helpers stay commit-free so the route owns the transaction.
- **New repository query** → the `repository` sub-package as pure SQL — never import crypto, `fetch`, or `deletions` from it.

## Related

- Modules: [services-arr](services-arr.md) (Radarr/Sonarr clients, date cache source, *arr unmonitor), [services-infra](services-infra.md) (SSRF guard, `delete_path` / delete-root parsing, settings), [services-mail](services-mail.md) (`send_newsletter` follow-up), [services-downloads](services-downloads.md) (`check_download_notifications` from the sync job), [platform](platform.md) (`mediaman.db`, `mediaman.crypto` keep-tokens, `mediaman.core` time/format/audit), [app-entry](app-entry.md) (`bootstrap/scan_jobs.py` wiring), [web-http](web-http.md) (routes `scan`, `keep`, `kept`, `kept_show`, `library_api`, `history` callers).
- DB tables: `media_items`, `scheduled_actions`, `kept_shows`, `settings`, `audit_log`, `keep_tokens_used`.
