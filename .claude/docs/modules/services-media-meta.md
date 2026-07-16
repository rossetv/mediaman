<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: services-media-meta

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

Two sibling service packages that together own all external-metadata and LLM
enrichment for mediaman. **`services/media_meta/`** wraps Plex (library / season
scan, watch history, user ratings, accounts), TMDB (search / details + pure
card / detail shaping), and OMDb (IMDb / RT / Metascore ratings), and provides
the in-place item-enrichment helpers used by the download and re-download flows.
**`services/openai/`** wraps the OpenAI Responses API and drives the
recommendations pipeline: assemble prompts from Plex watch-history / ratings,
call the LLM, validate output against prompt-injection, enrich each item via
TMDB + OMDb, persist into the `suggestions` table, and enforce a manual-refresh
cooldown.

Both packages are the shared metadata layer consumed by the scanner pipeline and
web routes; neither may import from `mediaman.web` or `mediaman.scanner`.

## Key files

| File | Role |
|------|------|
| `services/media_meta/plex.py` | `PlexClient` — plexapi wrapper: `get_libraries` / `get_movie_items` / `get_show_seasons` / `get_watch_history` / `get_season_watch_history` / `get_user_ratings` / `get_accounts` / `is_reachable`. Constructor is SSRF-hardened (revalidates URL, raises `SSRFRefused`) and injects `_SafePlexSession` into `PlexServer`. Installs `defusedxml.defuse_stdlib()` at import; parses via `defusedxml.ElementTree`. Defines `PlexResponseTooLarge(ValueError)`. |
| `services/media_meta/_plex_session.py` | `_SafePlexSession` (`requests.Session` subclass) enforcing per-call SSRF re-validation, `allow_redirects=False`, forced streaming + 16 MiB body cap (`_PLEX_MAX_BYTES`), `(connect, read)` timeout split, DNS pin (`pin()`), and `Accept-Encoding: identity` (gzip-bomb defence). Plus `_scrub_plex_token`, `_PLEX_TOKEN_RE`, and the private `_PlexBodyTooLarge`. |
| `services/media_meta/_plex_types.py` | TypedDicts for Plex shapes (`PlexLibrarySection` / `PlexMovieItem` / `PlexSeasonItem` / `PlexWatchEntry` / `PlexRatedItem` / `PlexAccount`) and converters `_movie_to_item` / `_season_to_item` / `_to_utc`. `_HISTORY_MAX_BYTES = 4 * 1024 * 1024`. Sets `is_anime` via `anime_detect`. |
| `services/media_meta/anime_detect.py` | `is_anime(show)`: explicit `Anime` genre → True; else `Animation` genre + studio in `_JP_STUDIOS` frozenset → True; else False. Standalone so scanner utilities can detect anime without pulling the Plex dependency tree. |
| `services/media_meta/tmdb.py` | `TmdbClient` — `search` / `search_multi` / `search_multi_paged` / `trending` / `popular_movies` / `popular_tv` / `details` / `is_reachable` over `api.themoviedb.org/3` (`_BASE`). `from_db()` factory fails closed (None on missing / undecryptable token), backed by a FIFO client cache (`_CLIENT_CACHE`) keyed on `(token, timeout)`, `_CLIENT_CACHE_MAXSIZE = 4`. Re-binds `shape_card` / `shape_detail` as staticmethods. |
| `services/media_meta/_tmdb_shapes.py` | Network-free TMDB TypedDicts (`TmdbSearchResult` / `TmdbDetailsPayload` / `TmdbCard` / `TmdbDetail`) and pure `shape_card` / `shape_detail` transforms (w300 posters via `_POSTER_BASE_W300`, vote rounded 1dp, genres / cast JSON-encoded, YouTube trailer-key extraction). |
| `services/media_meta/omdb.py` | `fetch_ratings(title, year, media_type; omdb_key OR conn+secret_key)` → `{imdb, rt, metascore}` subset, never raises on a network / decode error. `get_omdb_key` reads settings. `_attach_scrub_filters` wires `ScrubFilter` / `register_secret` so the `apikey` query param is redacted from urllib3 DEBUG logs. Module-level shared `_OMDB_CLIENT` session. |
| `services/media_meta/item_enrichment.py` | `apply_tmdb_detail` (in-place merge, only overwrites on a non-falsy value), `enrich_item_with_tmdb` (TMDB search + details + OMDb), `enrich_redownload_item` (suggestions-cache lookup first, TMDB / OMDb fallback). Consumed by `web/routes/download/confirm.py`. |
| `services/media_meta/__init__.py` | Package docstring / layering contract: metadata clients are shared across scanner and web, so importing `mediaman.web` / `mediaman.scanner` is forbidden. |
| `services/openai/client.py` | Shared `_OPENAI_CLIENT` (`SafeHTTPClient`, `(5.0, 30.0)` timeout) and `call_openai()` — POSTs the prompt to `/v1/responses`, parses the wrapper object, validates web-search titles. Holds `get_openai_key` / `get_openai_model` / `is_web_search_enabled` / `is_web_search_title_safe`. `_DEFAULT_MODEL = "gpt-5.5"`. |
| `services/openai/recommendations/prompts.py` | Prompt construction (`generate_trending`, `generate_personal`) with untrusted-data delimiter blocks + byte-budgeted Plex block, and `parse_recommendations` + `_validate_llm_string` (control-char + prompt-injection rejection), `sanitise_plex_string`, `strip_season_suffix`. The prompt-injection defence layer. |
| `services/openai/recommendations/persist.py` | `refresh_recommendations` — the recommendations orchestrator: fetch watch history + Plex ratings + previous titles, generate trending + personal, enrich, and DELETE + INSERT into `suggestions` inside one transaction (manual vs scheduled batch semantics via `_insert_recommendations`). Returns count inserted. |
| `services/openai/recommendations/enrich.py` | `enrich_recommendations` — in-place TMDB + OMDb fill for recommendation dicts (single loop reusing a shared `TmdbClient`; description truncated to 250 chars; OMDb IMDb-score fallback for `rating`). |
| `services/openai/recommendations/throttle.py` | Manual-refresh cooldown (`RECOMMENDATION_REFRESH_COOLDOWN_HOURS = 24`): `last_manual_refresh` / `refresh_cooldown_remaining` / `record_manual_refresh`, keyed on setting `last_manual_recommendation_refresh` (`_LAST_REFRESH_KEY`). |
| `services/openai/recommendations/repository.py` | suggestions-table read / write repository: `SuggestionRow` / `SuggestionDetail` frozen dataclasses, `fetch_suggestion_by_id` / `fetch_suggestion_header` / `mark_downloaded`. Shared by share-token and download routes. |
| `services/openai/recommendations/_types.py` | `RecommendationItem` TypedDict — the shape flowing prompts → enrich → persist; nullable fields are `NotRequired` because filled by later stages. |
| `services/openai/recommendations/__init__.py` | Re-exports `refresh_recommendations` (the pipeline entrypoint) and restates the forbidden-pattern rule: raw LLM output must pass `_validate_llm_string` before escaping. |

## Invariants

- **Layering: neither package imports from `mediaman.web` or `mediaman.scanner`.**
  They are the shared metadata layer for both. The only textual match is the
  docstring in `services/media_meta/__init__.py` stating the rule.
- **All outbound HTTP is routed through `SafeHTTPClient` or `_SafePlexSession`,**
  so every call inherits SSRF re-validation, redirect refusal, body caps, and
  (for Plex) DNS pinning + `Accept-Encoding: identity`. plexapi's own session is
  never used un-hardened — `PlexClient` injects `_SafePlexSession` into
  `PlexServer`.
- **Raw LLM output must pass validation before it can be persisted or re-enter a
  future prompt.** `parse_recommendations` → `_validate_llm_string` (control-char
  + injection-pattern rejection) for all output, and `is_web_search_title_safe`
  for web-search titles. This is the package's stated forbidden-pattern rule.
- **The web-search tool is doubly gated:** active only when the caller passes
  `use_web_search=True` **and** the `openai_web_search_enabled` setting is True
  (default False) — the indirect-prompt-injection surface is opt-in on both
  sides. The directive text and the `web_search_preview` tool are always sent
  together or not at all.
- **Factories fail closed.** `TmdbClient.from_db` returns None on a missing /
  undecryptable token (callers must handle absence, not a raise); the
  `PlexClient` constructor raises `SSRFRefused` if the configured URL fails the
  SSRF guard.
- **Secrets are registered for log-scrubbing before the first network call that
  could leak them** (`ScrubFilter.attach` + `register_secret`) — the Plex
  `X-Plex-Token` at `PlexClient` construction, the OMDb `apikey` inside
  `fetch_ratings`.
- **SQLite connections must not cross threads.** The OMDb key must be resolved via
  `get_omdb_key` in the owning thread and passed as `omdb_key=` to thread-pool
  workers; `fetch_ratings` requires either `omdb_key` or (`conn` + `secret_key`)
  and raises `TypeError` otherwise.
- **`get_season_watch_history` is all-or-nothing:** any episode-fetch failure
  re-raises to abort the whole season (the scanner marks it skipped) — a partial
  season history is never returned, to avoid mis-reading watched state.
- **`generate_personal` budgets the inner Plex-data content by bytes BEFORE
  wrapping it** in the `<UNTRUSTED_*>` / `<BEGIN_PLEX_DATA>` delimiters, so the
  closing delimiter tags are never truncated and no multi-byte codepoint is cut.
- **`suggestions.batch_id` is always a fixed-width `YYYY-MM-DD` string,** so
  lexical `>=` / `<` comparisons equal chronological ones — relied on by
  `_fetch_previous_titles` (30-day window) and `_insert_recommendations` (90-day
  pruning).
- **`_insert_recommendations` does DELETE + INSERT inside a single `with conn:`
  transaction** (rolls back on exception, CODE_GUIDELINES §9.7); callers must not
  wrap it in a second `with conn:`.

## Gotchas

- **`_DEFAULT_MODEL = "gpt-5.5"` must stay in sync with the web-layer allowlist**
  `validate_openai_model` in `web/models/settings.py` (`allowed = {"gpt-5.5",
  "gpt-5.4-mini"}`); a mismatch would let an unvalidated model reach the API.
- **The `openai` PyPI SDK is NOT a dependency.** Despite the
  `services/openai/__init__.py` docstring listing it as an optional package,
  nothing imports it — the Responses API is called via raw `SafeHTTPClient` HTTP.
  Stale docstring / defect worth fixing.
- **`TmdbClient._get` returns `Any` deliberately** (the response type varies dict
  vs list per endpoint); `Response.json()` raises `ValueError` (a
  `json.JSONDecodeError`, **not** a `RequestException`), so every per-method
  except clause must list `ValueError` explicitly — otherwise a TMDB HTML error
  page during an outage would crash the caller.
- **Web-search recommendations reject the ENTIRE batch on a single unsafe title**
  (conservative anti-injection), whereas non-web-search output is filtered
  per-item via `_validate_llm_string` — different trust models by design.
- **`TmdbClient` re-binds `shape_card` / `shape_detail` as staticmethods** purely
  for back-compat (`TmdbClient.shape_card(...)` call sites and test
  monkeypatches); the real implementations live in `_tmdb_shapes.py`.
- **`_SafePlexSession` resolves `resolve_safe_outbound_url` via the parent `plex`
  module's namespace at call time,** so tests that monkeypatch
  `mediaman.services.media_meta.plex.resolve_safe_outbound_url` still affect the
  sub-module. `allowed_hosts` is only threaded through when non-None, to keep
  url-only monkeypatches working.
- **`PlexResponseTooLarge` subclasses `ValueError`.** Plex body-cap breaches
  surface as this type (the scanner's catch-all was widened to include it) so an
  `except ValueError` catcher still works.
- **`_to_utc` assumes plexapi's naive datetimes are the Plex server's local wall
  clock** and converts via `astimezone(UTC)`; omitting it would shift stored
  `addedAt` / `updatedAt` by the local UTC offset.
- **The `TmdbClient` client cache is FIFO, not true LRU** (cache hits don't
  reorder the dict); indistinguishable from LRU at `maxsize 4` with a single
  token, but note it if callers vary the timeout heavily.
- **`call_openai` defaults `use_web_search=False` as a deliberate hardening** —
  the old default True let new call paths silently request web search whenever
  the operator setting was on.
- **`enrich_recommendations` truncates the TMDB description to 250 chars** and,
  when TMDB gives no rating, falls back to the OMDb IMDb score rounded to 1dp —
  preserved quirks from the pre-consolidation three-pass pipeline.
- **`generate_personal` is kept as one function on purpose** (the byte-budget
  arithmetic is one auditable unit); `_INJECTION_PATTERNS` intentionally omits
  `re.DOTALL` because the control-char strip already removes newlines before the
  patterns run.

## Extension points

- **A new Plex fetch** → add a method to `PlexClient` in `plex.py`; it inherits
  the `_SafePlexSession` hardening automatically. New shapes go in
  `_plex_types.py`.
- **A new TMDB endpoint** → add a method to `TmdbClient` in `tmdb.py` (list the
  `ValueError` in its except clause); network-free shaping goes in
  `_tmdb_shapes.py`.
- **A new OMDb rating field** → extend the `{imdb, rt, metascore}` subset built
  in `fetch_ratings` (`omdb.py`).
- **A new anime studio / rule** → `_JP_STUDIOS` (or the two-tier check) in
  `anime_detect.py` — standalone so the scanner avoids the Plex dep tree.
- **A new OpenAI model** → `_DEFAULT_MODEL` in `client.py` **and** the
  `validate_openai_model` allowlist in `web/models/settings.py` (keep them in
  sync — see Gotchas).
- **A new prompt-injection pattern** → `_INJECTION_PATTERNS` in `prompts.py`
  (mind the deliberate absence of `re.DOTALL`).
- **A new recommendation field** → add to `RecommendationItem` in `_types.py`
  (as `NotRequired` if late-filled), fill it in `enrich.py`, and add the column
  to the `_insert_recommendations` INSERT in `persist.py`.

## Related

- **No single entrypoint** — these are service libraries with several public
  surfaces:
  - Recommendations pipeline: `refresh_recommendations()` in
    `recommendations/persist.py` (re-exported from `recommendations/__init__.py`),
    called by `scanner/engine.py` (scheduled) and
    `web/routes/recommended/refresh.py` (manual, `api_refresh_recommendations`).
  - media_meta: `PlexClient` (built by `services/arr/build.py`
    `build_plex_from_db` and by `web/routes/settings/testers.py`),
    `TmdbClient.from_db()` (item_enrichment, enrich, dashboard poster fan-out,
    search routes), `omdb.fetch_ratings()`, and
    `item_enrichment.enrich_redownload_item()` (`web/routes/download/confirm.py`).
  - Low-level LLM call: `call_openai()` in `openai/client.py`.
- **Dependencies** (imported *by* this module; must never import this module):
  - Modules: [services-infra](services-infra.md) — `SafeHTTPClient`,
    `SafeHTTPError`, `SSRFRefused`, `resolve_safe_outbound_url`,
    `allowed_outbound_hosts`, `PINNED_EXTERNAL_HOSTS`, `get_string_setting` /
    `get_bool_setting`, plus `infra.http.pin` (imported privately by
    `_plex_session`).
  - `mediaman.core` — `scrub_filter` (`ScrubFilter` / `register_secret`), `time`
    (`now_utc` / `parse_iso_strict_utc`), `format`.
  - `mediaman.db` — `suggestions` + `settings` tables via the passed
    `sqlite3.Connection`.
  - Third-party: `requests` (session objects + exception types across
    tmdb / omdb / plex / openai), `plexapi` (`PlexServer` with the injected
    hardened session, `PlexApiException`), `defusedxml` (`defuse_stdlib()` +
    `ElementTree` for billion-laughs / XXE defence).
- **Consumers** (import this module; must never be imported *by* it):
  [services-arr](services-arr.md) (`build_plex_from_db`), the scanner pipeline,
  and the web routes (recommended-refresh, download / confirm, search, dashboard
  poster fan-out).
- **External HTTP endpoints** (all pinned / allowlisted): `api.themoviedb.org/3`,
  `image.tmdb.org`, `www.omdbapi.com`, `api.openai.com/v1/responses`, and the
  operator's Plex server.
- Law: [`CODE_GUIDELINES.md`](../../../CODE_GUIDELINES.md) — §9.5 (repository
  returns dataclasses, not raw rows), §9.7 (single-transaction DELETE + INSERT
  rollback), both cited in-code.
- Decisions: none recorded yet.
- Specs: none recorded yet.
