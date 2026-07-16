<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: services-downloads

## Purpose

NZBGet integration layer (`mediaman.services.downloads`): builds the merged
NZBGet + Radarr/Sonarr download-queue response for the downloads page, sends
"ready to watch" completion emails via Mailgun, and provides the single
chokepoint for abandoning stuck *arr* searches. Consumed by background jobs
(scanner sync, FastAPI lifespan) and web routes, and deliberately independent of
the HTTP layer. No single entrypoint — four public functions wired into distinct
callers: `check_download_notifications` (post-sync email pipeline),
`build_downloads_response` (downloads-page render), `record_download_notification`
(queue a pending row), `reconcile_stranded_notifications` (startup sweep); plus
`abandon_movie`/`abandon_series`/`abandon_seasons` and the `NzbgetClient` JSON-RPC
wrapper.

## Key files

| File | Role |
|------|------|
| `src/mediaman/services/downloads/__init__.py` | Package docstring only; declares allowed deps (`infra.http`, `crypto`, `db`, `arr`) and the hard rule "do not import from `mediaman.web`" — the package is consumed by background jobs and must stay HTTP-layer-independent. |
| `src/mediaman/services/downloads/notifications.py` | Notification orchestrator. `check_download_notifications()` runs the claim→probe-arr→email→mark pipeline; `record_download_notification()` inserts a pending row (no commit — caller owns the txn). Builds per-tick Sonarr/Radarr library indexes to avoid N+1 HTTP; `_check_arr_availability`/`_check_radarr_movie`/`_sonarr_has_files` do the per-row probe. Re-exports `reconcile_stranded_notifications` + `STRANDED_CLAIM_GRACE_SECONDS`. |
| `src/mediaman/services/downloads/_notification_claims.py` | Atomic claim/release mechanics (`_claim_pending_notifications`: `UPDATE…WHERE notified=0 RETURNING`, with a SELECT+UPDATE-under-`BEGIN IMMEDIATE` fallback), the `ClaimedNotificationRow` dataclass, and `reconcile_stranded_notifications()` startup sweep for rows stuck at `notified=2`. |
| `src/mediaman/services/downloads/_notification_backoff.py` | Process-local backoff state. Keeps two failure modes distinct: exponential arr-failure backoff (`_record_arr_failure`, cap `_BACKOFF_MAX_SECONDS`=1800s) vs a fixed `_POLL_INTERVAL_SECONDS`=60s in-progress poll throttle (`_record_poll_attempt`). Guarded by `_backoff_state_lock` (`threading.Lock`); owns the module-singleton `_NOTIFY_BACKOFF` (`ExponentialBackoff`). |
| `src/mediaman/services/downloads/_notification_email.py` | Email rendering. Module-cached (double-checked-locked) Jinja `Template` (`get_notification_template`), `SuggestionsMeta` boundary type, `gather_email_meta` + `build_email_payload`. All TMDB fields go through `autoescape=True`; no raw HTML string building. |
| `src/mediaman/services/downloads/nzbget.py` | `NzbgetClient` JSON-RPC wrapper (`get_status`/`get_queue`/`is_reachable`) over `SafeHTTPClient`, capped at `_NZBGET_MAX_BYTES`=1 MiB. `NzbgetError` distinguishes protocol errors from an idle queue. `_is_lan_host()` gates a plain-HTTP credential-leak warning. |
| `src/mediaman/services/downloads/abandon.py` | Abandon-search chokepoint. `abandon_movie`/`abandon_series`/`abandon_seasons` unmonitor in Radarr/Sonarr and `clear_throttle` only on full success. `AbandonResult` dataclass; token `0` signals "the movie itself". |
| `src/mediaman/services/downloads/download_format/_classify.py` | State-mapping & classification: `extract_poster_url`, `map_state`, `map_arr_status`, `map_episode_state`, `classify_movie_upcoming`/`classify_series_upcoming`, `compute_movie_released_at`/`compute_series_released_at`. `_MAX_FUTURE_YEARS`=100 filters the TMDB year-9999 sentinel. |
| `src/mediaman/services/downloads/download_format/_parsing.py` | Pure text helpers: `parse_clean_title` (token-strip NZB names), `normalise_for_match` (fuzzy title canonicalisation, strips Unicode `Cf`), `looks_like_series_nzb`, `format_eta`/`format_relative_time`/`format_episode_label`. |
| `src/mediaman/services/downloads/download_format/_render.py` | `build_item` (the `DownloadItem` factory), `build_episode_summary`, and `select_hero` (`_HERO_STATE_PRIORITY`: `downloading`>`almost_ready`>`queued`>`searching`>`upcoming`; higher progress wins within a tier). |
| `src/mediaman/services/downloads/download_format/_types.py` | `DownloadItem` TypedDict, `DOWNLOAD_STATE_LABELS` canonical state→label map, and `state_label()` lookup shared by templates and poll-loop JSON. |
| `src/mediaman/services/downloads/download_queue/__init__.py` | `build_downloads_response()` orchestrator (fetch arr+NZBGet, match, completion-detect, hero-select, subtitle). Holds the module-level completion-detection snapshot (`_previous_queue`, `_previous_initialised`, `_state_lock`) in the package root so tests can patch it; `_enrich_with_tmdb_ids` + `_maybe_record_completions`. |
| `src/mediaman/services/downloads/download_queue/items.py` | `DownloadsResponse` TypedDict, item builders (`build_matched_item`, `build_unmatched_arr_item` + movie/series variants), `nzb_matches_arr` bidirectional substring test, `build_episode_dicts`, `_stuck_seasons_from_episodes`. |
| `src/mediaman/services/downloads/download_queue/queue.py` | Stateless queue sub-functions: `parse_nzb_queue` (normalise raw NZBGet entries with a `_matched` flag), `_find_best_nzb_match` (largest-remaining wins), `build_arr_items`, `add_unmatched_nzb_items`. |
| `src/mediaman/services/downloads/download_queue/classify.py` | UI-string helpers: `build_search_hint` ("Searched 12× · next attempt in ~4h"), `_format_next_attempt` bands, `arr_base_urls` (prefers `*_public_url` over in-cluster `*_url`), `build_arr_link` deep links. |
| `src/mediaman/services/downloads/templates/download_ready.html` | Jinja email template for the completion notification; every interpolated field is autoescaped (XSS defence for TMDB free-text). |
| `src/mediaman/db/schema_definition.py` | Owns the `download_notifications` table (`id`, `email`, `title`, `media_type`, `tmdb_id`, `service`, `notified`, `created_at`, `tvdb_id`, `claimed_at`) and `idx_download_notifications_claimed` (partial index `WHERE notified=2`). |

## Invariants

- **No import from `mediaman.web`** anywhere in this package — it is consumed by background jobs and must remain independent of the HTTP layer (stated in `__init__.py`). Allowed deps: `infra.http`, `crypto`, `db`, `arr`.
- **Notification lifecycle** `notified=0` (pending) → `2` (claimed) → `1` (sent). Claiming is atomic via `UPDATE…WHERE notified=0 RETURNING`; on older SQLite it falls back to SELECT+UPDATE inside `BEGIN IMMEDIATE`. Any failure releases the row back to `0` so a later tick retries.
- **Claim materialises before commit.** The atomic-claim path builds the local row list **before** `conn.commit()` so a Python exception between commit and return cannot strand rows at `notified=2` (M7). The fallback path matches this commit-then-return ordering deliberately.
- **The SELECT-then-UPDATE fallback is correct only** because `db/connection.py` uses the default `isolation_level` (legacy mode), so `with conn:` does not auto-`BEGIN` and the explicit `BEGIN IMMEDIATE` is the sole transaction start. Switching `connection.py` to `isolation_level=None` would break it (B5).
- **`record_download_notification` does not call `conn.commit()`** — callers manage their own transactions.
- **Radarr rows match on TMDB id; Sonarr rows match on TVDB id** (falling back to TMDB when both are present). Each id lives in its own column so the completion checker (`_check_radarr_movie` / `_sonarr_has_files`) matches the right field per service.
- **Two failure modes are kept strictly separate** (B1): genuine arr unreachability applies exponential backoff (cap 30 min), while an item that is merely still downloading uses a fixed 60s poll throttle that does not accumulate.
- **One fetch per service per tick.** The Sonarr series list and Radarr movie library are each fetched at most once and reused for every row (O(1) lookups), only when a runnable row needs that service (M1 / R4-H1). On a fetch failure the index stays `None` and probes fall back to per-row HTTP.
- **Completion detection assumes a single worker.** `_previous_queue` is an in-process snapshot; a restart resets it and the startup `reconcile_stranded_notifications` covers the gap. `_state_lock` is held only for the snapshot swap — all arr HTTP I/O happens outside the lock.
- **Abandon clears the search throttle row only when nothing failed**, so partial-failure callers retain retry/search-count context. `abandon_seasons` raises `ValueError` on an empty season list (the endpoint should return 400, not silently no-op).
- **The XSS trust boundary is the template.** Email rendering routes every TMDB/user-sourced field through Jinja `autoescape=True`; the Python helpers never build raw HTML.

## Gotchas

- `map_state()` only ever returns `searching` \| `downloading` \| `almost_ready`. The `ready` and `queued` states come exclusively from per-episode `map_episode_state()`; `DOWNLOAD_STATE_LABELS` still lists them because episode dicts and hero-priority ranking use them.
- `_check_arr_availability` returns its `_ArrProbeOutcome` enum sentinels (`UNREACHABLE` / `UNKNOWN_SERVICE`) in the **same tuple slot** normally occupied by the `RadarrMovie` payload; callers must compare with `is`, not truthiness. An unknown service value **drains** the row (`notified=1`) rather than spinning forever.
- `_backoff_state` is a process-local module dict cleared only on a successful send (`_clear_backoff`). Rows that are deferred or drained by a non-email path are not explicitly cleared — growth is bounded by row count but the dict is not otherwise reaped.
- The NZBGet plain-HTTP credential warning logs only `urlparse(url).hostname`, never `self._url`, because the URL may embed a `user:pass@` component (§7.4). `_is_lan_host` returns `False` for non-literal hostnames it cannot resolve (conservative — it still warns); only literal IPs are classified via the `ipaddress` module.
- `build_downloads_response` passes `maybe_trigger_search` explicitly into `build_arr_items`, and the completion-detection state lives in the `download_queue` package `__init__` (not a submodule), specifically so the test suite can monkeypatch those names at the package level.
- The insert in `record_download_notification` hard-codes the column order (`email, title, media_type, tmdb_id, tvdb_id, service, notified=0, created_at`) — it must stay in sync with the `download_notifications` table definition in `db/schema_definition.py`.
- `_maybe_record_completions` swaps the queue snapshot **before** verifying completions via HTTP, deliberately: a concurrent poll then sees the new state and won't re-report the same completion (the reverse ordering would keep a stale snapshot for the whole I/O window).
- Poster URLs are upgraded `/w300` and `/w200` → `/w500` for emails (`build_email_payload`); `_MAX_FUTURE_YEARS`=100 filters TMDB's year-9999 unreleased sentinel out of release-date computations.
- `NzbgetError` exists specifically to distinguish a broken connection / auth failure (JSON-RPC `error` field) from a genuinely idle queue — the old code silently returned `{}` for both.
- `_find_best_nzb_match` prefers the matching NZB with the **largest** remaining size (a partially-downloaded pack) over an older completed entry still in the queue; `looks_like_series_nzb` stops a movie-kind arr item from greedily claiming a TV-episode NZB via loose substring matching.

## Extension points

- **New download service** (beyond `radarr`/`sonarr`): extend the service switch in `_check_arr_availability` (`notifications.py`) and the completion path in `download_queue/__init__.py`; add the id column to `download_notifications` in `db/schema_definition.py`.
- **New user-facing download state**: add it to `DOWNLOAD_STATE_LABELS` (`download_format/_types.py`), teach `map_state` / `map_episode_state` to emit it, and rank it in `_HERO_STATE_PRIORITY` (`download_format/_render.py`).
- **New email metadata field**: add it to `SuggestionsMeta` (`_notification_email.py`), the `SELECT` in `_fetch_suggestions_batch` (`notifications.py`), the template context in `build_email_payload`, and `templates/download_ready.html` (autoescaped — never `\|safe`).
- **New abandon target**: route it through the `abandon.py` chokepoint so the unmonitor + throttle-clear semantics stay in one place.

## Related

- Consumed by: `mediaman.scanner.runner` (`check_download_notifications` after each library sync); `mediaman.web.routes.downloads` (`build_downloads_response`, `abandon_movie`/`abandon_series`/`abandon_seasons`); `mediaman.web.routes.download.submit`, `mediaman.web.routes.recommended.api`, `mediaman.web.routes.search.download`, `mediaman.web.repository.library_api` (`record_download_notification`); `mediaman.app_factory` lifespan (`reconcile_stranded_notifications` once at startup); `mediaman.services.arr.auto_abandon` (`abandon_movie`/`abandon_seasons`).
- Consumes: `mediaman.services.arr` (`ArrClient`/`ArrError`, `RadarrMovie`/`SonarrSeries`, `build_radarr/sonarr/nzbget_from_db`, `detect_completed`/`record_verified_completions`/`fetch_and_sync_recent_downloads`, `fetch_arr_queue`/`ArrCard`, `maybe_trigger_search`/`clear_throttle`/`get_search_info`, `LazyArrClients`); `mediaman.services.infra` (`SafeHTTPClient`, `get_string_setting`, `ConfigDecryptError`); `mediaman.services.mail.mailgun.MailgunClient` (email transport); `mediaman.core` (`now_utc`/`now_iso`/`parse_iso_utc`, `format_bytes`/`format_day_month`, `ExponentialBackoff`); `jinja2`, `requests`.
- `NzbgetClient` is constructed via `mediaman.services.arr.build.build_nzbget_from_db`.
- Modules: [services-arr](services-arr.md) (client, queue-fetch, completion, throttle — the layer below), [services-mail](services-mail.md) (`MailgunClient`).
- SQLite tables: `download_notifications` (owned here, DDL in `db/schema_definition.py`); reads `suggestions`; writes verified completions to `recent_downloads` (via `services.arr.completion`).
- Decisions: none yet.
