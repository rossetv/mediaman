# mediaman Remediation Plan 2

Follow-up to `docs/remediation/PLAN.md`. Closes the gaps surfaced by the
14-agent review of the 72-commit branch.

This document is for an **orchestrator Claude** to drive parallel subagent
dispatch. Each task lists the files it touches so the orchestrator can build a
conflict graph; tasks in the same wave do not share files and may be
dispatched in parallel.

---

## Intentional decisions (out of scope)

The following original-PLAN.md items are settled by user intent and must
**not** be re-opened by any subagent:

- **Full subscriber email in `audit_log` is intended.** Single-operator
  self-hosted threat model. The audit_log triggers are append-only by design.
- **`audit_log.user_agent` migration is not wanted.** `_ua_hash` in login-event
  detail JSON is sufficient.
- **Literal `# rationale:` keyword is dropped.** Explanatory comments are
  still required for module-level mutable state and broad-except sites; the
  grep-target word is not.
- **Direct commits to `main` are acceptable** for this remediation set; no
  topic branch required.

User decisions for this round (already made):

- **Phase 11 test mirror reorganisation: full scope.**
- **SSRF allowlist: wire into production callers** (do not just edit §10.6).
- **mypy `strict = true`: enable** (Wave 3).
- **`api_media_redownload` + `api_media_delete`: split both** into per-branch
  helpers.

---

## Execution model

Tasks are grouped into **waves**. Within a wave, tasks have no file conflicts
and run as parallel subagents. Waves are sequential — do not start wave N+1
until wave N is green.

After each wave the orchestrator runs the full gate:

```bash
source .venv/bin/activate \
  && pytest -q \
  && ruff check src tests \
  && ruff format --check src tests \
  && mypy src/mediaman
```

If a wave introduces failures, the orchestrator pauses, dispatches one debug
subagent per independent failure (per `superpowers:dispatching-parallel-agents`),
and only proceeds when the gate is green.

### Subagent contract

Every subagent prompt MUST:

- pass `model: "opus"`;
- name the exact files in scope and quote the failing-state evidence cited
  here;
- forbid the subagent from firing user-attention alerts (no
  `terminal-colour.sh`, no stop messages, no sounds);
- forbid the subagent from creating commits — the orchestrator commits after
  reviewing the diff;
- require the subagent to read CODE_GUIDELINES.md before editing;
- demand a structured final report (changes made, files touched, acceptance
  criteria evidence, any new findings).

Commits: one per logical task, Conventional Commits format (`refactor(scope):
…` / `fix(scope): …`), no co-author lines, no AI attribution.

---

## Wave 0 — Blocking pre-flight (serial)

The CI gate is currently red on mypy. Wave 0 must land before any other
work. Three tasks, can run in parallel — collectively still small.

### W0.1 — Fix mypy errors in `_scan_library.py`

- **Files:** `src/mediaman/scanner/_scan_library.py`
- **Failing state:** `mypy src/mediaman` reports 3 errors at lines 101, 102,
  193. From commit `6c00aaf`.
  - `:101` — `_resolve_added_at` called with `PlexMovieItem | PlexSeasonItem`;
    expects `dict[str, object]`. Either fix the call signature in
    `_scan_library` or widen `ScanEngine._resolve_added_at` to accept Plex
    item models.
  - `:102` — `list[PlexWatchEntry]` passed where `list[dict[str, object]]`
    expected. Same shape fix.
  - `:193` — `is_show_kept` called with `object`; expects `str | None`.
    Narrow the local type before the call.
- **Acceptance:** `mypy src/mediaman` returns 0 errors.

### W0.2 — CODE_GUIDELINES.md doc bugs

- **Files:** `CODE_GUIDELINES.md`
- **Edits:**
  1. §2.1 (~line 234) — remove the "URL safety" mention from the `core/`
     purpose list. It now lives in `services/infra/`. Leaves only the
     redirect at §2.1's bottom (~line 245-247).
  2. §2.2 (~lines 258, 261) — rename the cited setting keys from
     `aes_canary_v1` → `aes_kdf_canary` and `aes_salt_v1` → `aes_kdf_salt`.
     Real names live in `crypto/_aes_key.py:33-34`.
  3. §2.6 (~lines 336-338) — add `url_safety.py` and `path_safety.py` to the
     `services/infra/` description.
  4. §10.6 — does NOT need a head-line rewrite (the allowlist will be wired
     in W1.32); just verify the body still matches reality after Wave 1.
- **Acceptance:** doc edits land; no other §2.1/§2.2 text changed.

### W0.3 — Stale Phase 6 doc references

- **Files:**
  - `src/mediaman/web/routes/__init__.py:13`
  - `src/mediaman/web/auth/middleware.py:36, 158`
- **Edits:**
  - `routes/__init__.py:13` — `mediaman.web._helpers` no longer exists;
    correct to `mediaman.web.cookies` and `mediaman.web.auth.middleware`.
  - `middleware.py:36, 158` — `is_admin` is used by `keep.py` and `kept.py`
    only; remove the stale "and recommended" claim.
- **Acceptance:** doc edits land; `is_admin` callers match the docstrings.

---

## Wave 1 — Independent file-scoped tasks (parallel)

Forty tasks, every one touches a distinct file set from every other task in
this wave. Orchestrator dispatches as many in parallel as the host can
support (cap ≈ 15-20 concurrent Opus subagents to avoid rate limits).

### W1.1 — CSS dead-code purge round 2

- **Files:**
  - `src/mediaman/web/static/css/_tables.css:131-200, 251-274`
  - `src/mediaman/web/static/css/_settings.css:144-196, 207-285`
  - `src/mediaman/web/static/css/_buttons.css:151-172, 175-206, 209-237`
- **Edits:**
  - `_tables.css`: delete `.lib-table*`, `.lib-row*` (except keep
    `.lib-row-protected` — one caller at `library.html:112`), `.lib-sort-col`,
    `.lib-title-col`, `.lib-title`, `.lib-subtitle`, `.lib-size`,
    `.lib-watch-info`, `.lib-actions`, `.lib-row-meta`, `.lib-protection-label`,
    `.watchlist-none`, `.lib-type-wrap`, `.btn-sm`. Drop the "Finding 10" /
    "legacy `.lib-table` / `.lib-row` grid layout" comments at lines 5 and 26.
  - `_settings.css`: delete `.users-*` v1 (JS emits `usr-*`),
    `.subscriber-item/email/status`, `.btn-remove`. Drop the stale "JS-emitted"
    comment at line ~236.
  - `_buttons.css`: delete `.countdown-badge`, `.filter-pill*` family,
    `.tab-bar`, `.segmented`, `.seg-btn`, `.tab-panel`.
- **Verify:** for each class deleted, `rg -n "<class>"
  src/mediaman/web/templates/ src/mediaman/web/static/js/` returns zero.
- **Acceptance:** files compile via `ruff format --check`; UI smoke test
  unaffected.

### W1.2 — CSS rgba token sweep

- **Files:** `src/mediaman/web/static/css/_dl.css:86,308`,
  `_buttons.css:144`, `_settings_pg.css:126,288`.
- **Edit:** replace raw `rgba(255,214,10,.18)` etc. with the existing tokens
  `var(--rgba-warning-bg)` / `var(--rgba-purple-bg)` / `var(--rgba-orange-bg)`
  from `_tokens.css:94,98,102`.
- **Acceptance:** zero literal `rgba(255,` / `rgba(155,` / `rgba(255,159,` in
  those files.

### W1.3 — JS micro-helper duplication

- **Files:** `src/mediaman/web/static/js/downloads/build_dom.js:24-35`.
- **Edit:** delete local `q`, `setText`, `findByDlId`; rewrite callers to use
  `MM.dom.q`, `MM.dom.setText`, `MM.dom.findByAttr`.
- **Acceptance:** zero file-local helper definitions; downloads page renders
  unchanged.

### W1.4 — Delete dead-code duplicates

- **Files:**
  - **Delete:** `src/mediaman/scanner/_post_scan.py` (zero callers; logic
    inlined in `engine.py:329-343`).
  - **Slim:** `src/mediaman/web/auth/user_crud.py` — keep only
    `find_username_by_user_id`. Delete `UserRecord`, `list_users`,
    `user_must_change_password`, `set_must_change_password`,
    `_cleanup_reauth_tickets`, `_delete_user_atomically`, `delete_user`.
    Routes import from `password_hash.py` (live versions). The duplicated
    `delete_user` also re-introduces a `RuntimeError("last_user")` sentinel
    Phase 9A explicitly deleted — deletion fixes that too.
  - **Slim:** `src/mediaman/web/repository/delete_intents.py` — delete the
    dead duplicates `MediaDeleteSnapshot` (TypedDict), `_MediaNotFound`,
    `snapshot_media_for_delete`, `handle_radarr_delete`, `handle_sonarr_delete`,
    `finalise_media_delete`. Routes use the `library_api.py` versions.
  - **Rewrite test mocks:** `tests/unit/scanner/test_engine.py` patches
    `mediaman.scanner._post_scan._send_newsletter` /
    `_refresh_recommendations` 14 times against a dead module. Rewrite each
    patch to target the real call path (likely
    `mediaman.services.mail.newsletter.send_newsletter` and
    `mediaman.services.openai.recommendations.refresh_recommendations` — verify
    by reading `engine.run_scan`'s actual imports). Verify each rewritten
    patch fires by adding an assertion side-effect.
- **Acceptance:**
  - `rg "from mediaman\.scanner\._post_scan" src/ tests/` returns zero.
  - `rg "from mediaman\.web\.auth\.user_crud import" src/ tests/` shows only
    `find_username_by_user_id`.
  - `rg "MediaDeleteSnapshot|snapshot_media_for_delete"
    src/mediaman/web/repository/delete_intents.py` returns zero.
  - Tests still pass with rewritten patches firing (not silently no-op).

### W1.5 — Wire poster repository

- **Files:** `src/mediaman/web/routes/poster/fetch.py:169-171, 288-291`.
- **Edit:** replace 3 raw SQL calls with `web/repository/poster.py` helpers
  (`fetch_plex_credentials`, `fetch_arr_ids`). The repository helpers already
  exist and are imported by nobody.
- **Acceptance:** `rg "conn\.execute|\.execute\(" src/mediaman/web/routes/`
  returns zero.

### W1.6 — Fix settings.py back-import direction

- **Files:**
  - `src/mediaman/web/repository/settings.py:28`
  - `src/mediaman/web/routes/settings/secrets.py`
- **Edit:** the constants `SECRET_FIELDS`, `INTERNAL_KEYS`,
  `SECRET_PLACEHOLDER`, `SECRET_CLEAR_SENTINEL` belong with the repository
  (canonical writer of secret rows). Move them to `web/repository/settings.py`
  (or a sibling `_settings_constants.py` if `settings.py` is already large).
  Update `routes/settings/secrets.py` (and any other callers) to import from
  the new location. Repositories **must not** import from `web/routes/`.
- **Acceptance:** `rg "from mediaman\.web\.routes" src/mediaman/web/repository/`
  returns zero.

### W1.7 — Remove library.py sys.modules hack

- **Files:** `src/mediaman/web/routes/library.py:178-196`.
- **Edit:** drop the `sys.modules["mediaman.web.routes.library._query"] = ...`
  shim. Update the test(s) that patch the dead path to patch
  `mediaman.web.repository.library_query` directly.
- **Acceptance:** `rg "sys\.modules" src/mediaman/web/routes/` returns zero;
  the test that depended on the shim still passes.

### W1.8 — Transaction tightening

- **Files:**
  - `src/mediaman/web/routes/kept.py:178-191, 294-334, 352-366`
    (`api_unprotect`, `api_keep_show`, `api_remove_show_keep`)
  - `src/mediaman/web/routes/subscribers.py:149-150` (`api_unsubscribe` audit
    write happens after `delete_subscriber` commits)
  - `src/mediaman/web/repository/library_api.py:241` (`record_redownload`)
- **Edit:** for each, ensure the mutation and the audit-log write happen
  inside the same transaction. Use `with conn:` + `BEGIN IMMEDIATE` (or call
  the equivalent repository helper that does so). The current state allows
  for an audit row to be missing if the second write crashes.
- **Acceptance:** zero audit-after-commit pairs in the named files; each
  multi-write path is enclosed in a single `with conn:` block.

### W1.9 — library_query `__all__` cleanup

- **Files:** `src/mediaman/web/repository/library_query.py:41-57, 463-479`.
- **Edit:** `__all__` lists underscore-prefixed names (`_VALID_SORTS`,
  `_MAX_SEARCH_TERM_LEN`, `_days_ago`, `_protection_label`, `_type_css`).
  Either drop the underscores (preferred — the names are imported by sibling
  modules) or remove from `__all__`.
- **Acceptance:** every name in `__all__` is non-underscored.

### W1.10 — Caught-too-narrow regressions (Phase 9 follow-up)

- **Files & lines:**
  - `src/mediaman/services/arr/fetcher/_base.py:44`
  - `src/mediaman/services/arr/fetcher/_sonarr.py:239`
  - `src/mediaman/web/routes/poster/fetch.py:237, 251`
  - `src/mediaman/web/routes/download/confirm.py:287`
  - `src/mediaman/web/routes/download/status.py:467`
- **Edit:** each currently catches `(requests.RequestException, SafeHTTPError)`
  but wraps a call that now raises `ArrUpstreamError` (Phase 9 Item 3). Add
  `ArrError` (or `ArrUpstreamError`) to the catch tuple at each site.
- **Test:** for each site, simulate `ArrUpstreamError` and confirm the handler
  catches it.
- **Acceptance:** mocked `ArrUpstreamError` raises do not surface as 500s at
  those sites.

### W1.11 — Re-narrow `_transport.py` and `engine.py`

- **Files:**
  - `src/mediaman/services/arr/_transport.py:97, 107, 118, 128, 181`
  - `src/mediaman/scanner/engine.py:199, 332, 342`
- **Edit:**
  - `_transport.py`: replace `except Exception` (currently labelled with
    `# rationale: preserve-and-rethrow`) with the form Phase 9B used:
    `except (SafeHTTPError, requests.RequestException, ValueError) as exc:`
    and re-raise as `ArrError`/`ArrUpstreamError` as appropriate. The
    rationale label was added when the narrowing should have been restored.
  - `engine.py`: restore the narrowing reverted by Phase 8C. Original:
    `(PlexApiException, requests.RequestException, sqlite3.Error)`. Add a
    1-line comment above each `except` explaining the catch.
- **Acceptance:** no bare `except Exception` at those line numbers.

### W1.12 — Typed exceptions for domain errors

- **Files:**
  - `src/mediaman/web/routes/users/crud.py:110` + `web/auth/password_hash.py`
    `create_user` body — define `UserExistsError(Exception)` in
    `password_hash.py`; raise it from `create_user` instead of
    `ValueError("user_exists")`; catch at the route.
  - `src/mediaman/scanner/runner.py:258` — `ValueError` from SSRF refusal.
    Catch `SSRFRefused` (define in `services/infra/url_safety.py` if missing)
    or wrap with a 1-line rationale comment.
  - `src/mediaman/bootstrap/scan_jobs.py:130` — replace
    `raise RuntimeError("Refusing to start scheduler: ...")` with
    `raise SchedulerStartupRefused(...)`. Define the class in `bootstrap/`.
  - `src/mediaman/crypto/aes.py:133, 135` — replace `raise ValueError(...)`
    in the decrypt path with `raise CryptoInputError(...)` (subclass of
    `CryptoError`).
- **Acceptance:** named exceptions exist and replace the generic raises;
  callers catch by type.

### W1.13 — Fix `logger.warning(..., exc)` stack drop

- **Files:** `src/mediaman/services/mail/newsletter/render.py:43`.
- **Edit:** change to `logger.exception(...)` or add `exc_info=True`.
- **Acceptance:** `rg "logger\.warning\(.*exc(?!_info)" src/` returns zero
  (sweep, not just the one line — there may be siblings).

### W1.14 — Repository ROLLBACK pattern

- **Files:** `src/mediaman/web/repository/subscribers.py:123-125`.
- **Edit:** replace `except Exception: conn.execute("ROLLBACK"); raise` with
  a context-manager pattern (`with conn:`) so the original exception is not
  lost if ROLLBACK itself raises.
- **Acceptance:** zero manual `conn.execute("ROLLBACK")` calls in
  `web/repository/`.

### W1.15 — Bootstrap crypto narrowing

- **Files:** `src/mediaman/bootstrap/crypto.py:66, 79`.
- **Context:** the c089474 incident showed this catch swallows
  `ModuleNotFoundError` and conflates "missing module" with "AES key wrong"
  → 13-day silent scheduler outage in prod.
- **Edit:** narrow to `(CryptoError, sqlite3.DatabaseError)` (or equivalent
  set covering the real failure modes). Let `ImportError` /
  `ModuleNotFoundError` propagate so future relocation bugs surface
  immediately.
- **Acceptance:** `ImportError` propagates past `bootstrap_crypto`; canary
  failure still trapped and `canary_ok=False` still set.

### W1.16 — Add missing broad-except comments

- **Files:**
  - `src/mediaman/web/auth/session_store.py:215, 394, 412`
  - `src/mediaman/web/auth/password_hash.py:374, 448`
  - `src/mediaman/scanner/_scan_library.py:128`
  - Any sites surfaced by `rg -B1 "except Exception" src/mediaman/` with no
    explanatory comment within 3 lines above.
- **Edit:** §6.4 still requires an explanatory comment naming why broad is
  correct and which §6.4 site (1=outermost retry, 2=scheduler runner,
  3=FastAPI handler, 4=cold-start). Add the comment, or narrow the catch
  where the site doesn't fit one of the four allowed.
- **Acceptance:** every `except Exception:` in `src/` has either a narrower
  catch or an explanatory 1-line comment.

### W1.17 — Restore notifications typing (Phase 10 revert)

- **Files:**
  - `src/mediaman/services/downloads/notifications.py:177, 203, 232, 263-265`
  - `src/mediaman/services/downloads/_notification_email.py:43, 62, 110, 112`
- **Edit:** merge `14a6501` reverted the original 68f6c3c typing. Re-apply:
  - `arr: LazyArrClients`
  - `mailgun: MailgunClient`
  - `template: jinja2.Template`
  - `movie: RadarrMovie | None`
  - `row_id: int`, `tmdb_id: int | None`, `tvdb_id: int | None`
  - `get_notification_template() -> jinja2.Template`
- **Acceptance:** zero bare `Any` in those two files; mypy clean.

### W1.18 — Type poster/fetch.py

- **Files:** `src/mediaman/web/routes/poster/fetch.py:161, 192, 214, 275, 302`.
- **Edit:** replace 6 bare `Any` with concrete types:
  `conn: sqlite3.Connection`, `row: sqlite3.Row`, `config` to its real
  config type, `http_client: SafeHTTPClient | None`.
- **Acceptance:** zero bare `Any` in `poster/fetch.py`; mypy clean.

### W1.19 — arr re-cast cleanup

- **Files:**
  - `src/mediaman/services/arr/_radarr_methods.py:35, 67, 81, 91, 116`
  - `src/mediaman/services/arr/_sonarr_methods.py:144, 179, 247, 271, 294`
  - `src/mediaman/services/arr/_transport.py:175`
  - `src/mediaman/services/arr/_add.py:45, 71`
  - `src/mediaman/services/arr/_lookups.py:28, 33, 38, 47`
  - `src/mediaman/services/arr/base.py:109, 118`
- **Edit:** remove `cast(dict, ...)` / `cast(dict[str, object], ...)`. Replace
  return types with the TypedDicts that already exist in `_types.py`:
  `RadarrMovie`, `SonarrSeries`, `ArrRootFolder`, `ArrQualityProfile`,
  `ArrLookupResult`, `ArrQueueItem`. Type `_put`'s body parameter as
  `Mapping[str, object]` (or similar) if that's what enables removing the
  casts.
- **Acceptance:** zero `cast(dict, ...)` / `cast(dict[str, object], ...)` in
  `services/arr/`; mypy clean.

### W1.20 — list[dict] parameterisation sweep

- **Files:**
  - `src/mediaman/services/openai/recommendations/{prompts,persist}.py`
  - `src/mediaman/services/mail/newsletter/{schedule,recipients,enrich,render,summary}.py`
  - `src/mediaman/services/downloads/download_format/{_types,_render}.py`
  - `src/mediaman/services/downloads/download_queue/items.py:38-44`
  - `src/mediaman/web/routes/search/{_enrichment,detail}.py`
  - `src/mediaman/web/routes/{history,kept}.py`
  - `src/mediaman/web/routes/download/status.py:247`
  - `src/mediaman/scanner/_scan_library.py:151, 190`
- **Edit:** replace unparameterised `list[dict]` / `dict` with typed shapes.
  Reuse existing TypedDicts where they apply; define new ones sparingly and
  in the same module that owns the data.
- **Acceptance:** zero `list[dict]` / `dict` (without parameters) annotations
  in those files; mypy clean.

### W1.21 — JS fetch → MM.api migration

- **Files:**
  - `src/mediaman/web/static/js/recommended.js:58, 82, 302`
  - `src/mediaman/web/static/js/recommended/refresh.js:72, 102, 141`
  - `src/mediaman/web/static/js/recommended/poll.js:80`
  - `src/mediaman/web/static/js/downloads/poll.js:31`
- **Edit:** replace raw `fetch(...)` with `MM.api.get(...)` /
  `MM.api.post(...)`. For `downloads/poll.js:31` (401/403 redirect): either
  extend `MM.api` with an `onAuthFailure` callback or document the carve-out
  in a 1-line comment.
- **Acceptance:** zero raw `fetch(` in those four files (or each retained
  one has a per-site rationale comment).

### W1.22 — JS recommended.js modal migration

- **Files:** `src/mediaman/web/static/js/recommended.js:189-193, 332-342,
  377-379`.
- **Edit:** replace the hand-rolled `openModal` / `closeModal` /
  backdrop-click with `MM.modal.setupDetail`. This is the migration that
  e81e166's commit message falsely claimed had landed.
- **Acceptance:** `recommended.js` uses `MM.modal.setupDetail`; zero
  `dm.style.display = 'flex'` / `dm.style.display = 'none'` patterns.

### W1.23 — c.tile macro adoption

- **Files:**
  - `src/mediaman/web/templates/dashboard.html:72`
  - `src/mediaman/web/templates/protected.html:23, 61, 99`
  - `src/mediaman/web/templates/_rec_card.html:11`
- **Edit:** replace each hand-rolled `<div class="tile">...</div>` block with
  a `{{ c.tile(...) }}` call. The macro currently has zero callers. If the
  signature doesn't accommodate every page's `data-*` attributes, extend the
  macro (preferred — add a `raw_data_attrs={}` parameter that takes
  pre-encoded values) rather than keep five inline tiles.
- **Acceptance:** all five sites use `c.tile`; visual smoke test against the
  pre-change DOM.

### W1.24 — `datetime.now(UTC)` sweep

- **Files (18 sites):**
  - `src/mediaman/web/auth/session_store.py:70, 261`
  - `src/mediaman/services/downloads/download_format/_classify.py:67, 93, 119, 155`
  - `src/mediaman/services/arr/_throttle_persistence.py:213`
  - `src/mediaman/web/repository/delete_intents.py:67, 82`
  - `src/mediaman/web/repository/download.py:59`
  - `src/mediaman/web/routes/search/_enrichment.py:138`
  - `src/mediaman/web/routes/recommended/pages.py:201`
  - `src/mediaman/services/downloads/notifications.py:359`
  - `src/mediaman/services/downloads/_notification_claims.py:142`
  - `src/mediaman/services/openai/recommendations/{throttle,prompts,persist}.py`
  - `src/mediaman/services/mail/newsletter/__init__.py:164, 170`
  - `src/mediaman/scanner/deletions.py:302`
  - `src/mediaman/db/connection.py:205`
- **Edit:** replace `datetime.now(UTC)` with `now_utc()` from `core.time`.
  Files already importing `core.time` are the most embarrassing — fix those
  first.
- **Acceptance:** `rg "datetime\.now\(UTC\)|datetime\.now\(timezone\.utc\)"
  src/mediaman/` ≤ 5 remaining sites, each with an inline rationale.

### W1.25 — `parse_iso_strict_utc` adoption

- **Files:**
  - `src/mediaman/web/auth/reauth.py:298, 299`
  - `src/mediaman/web/auth/session_store.py:118`
  - `src/mediaman/services/mail/newsletter/_time.py:23`
  - `src/mediaman/services/openai/recommendations/throttle.py:36`
- **Edit:** replace inline `datetime.fromisoformat(...)` + manual UTC fixup
  with `parse_iso_strict_utc(...)` from `core.time`.
- **Acceptance:** zero inline fromisoformat+tz-fixup patterns in those files.

### W1.26 — Split `api_media_redownload` + `api_media_delete`

- **Files:**
  - `src/mediaman/web/routes/library_api/redownload.py:166`
    (`api_media_redownload` ~180 lines)
  - `src/mediaman/web/routes/library_api/__init__.py` (`api_media_delete`
    ~131 lines) — keep in current file or move to a sibling `delete.py`
    (orchestrator decides; symmetry with `redownload.py` favours the move).
- **Edit:** extract per-branch helpers
  (`_try_radarr_redownload(...) -> JSONResponse | None`,
  `_try_sonarr_redownload(...) -> JSONResponse | None`, similar for delete).
  The current `# rationale:` on `api_media_redownload` is **factually wrong**:
  it claims "audit-log writes that all share a single DB connection and must
  roll back together — splitting the branches into helpers would require
  threading the connection and rollback state through every call", but
  `record_redownload` (`repository/library_api.py:241`) commits its own
  transaction. The Radarr/Sonarr branches are sequential and independent.
- **Acceptance:** both functions ≤60 lines OR carry a corrected, accurate
  rationale explaining the real reason for non-decomposition.

### W1.27 — Split remaining oversized functions

- **Files & functions (each named in PLAN.md Phase 8):**
  - `src/mediaman/web/auth/session_store.py:240 validate_session` (106 lines;
    has Phase 1..5 sub-comments inviting extraction)
  - `src/mediaman/services/infra/storage.py:81 _validate_delete_roots` (113
    lines; extract per-root check helper)
  - `src/mediaman/web/routes/settings/__init__.py:209 api_update_settings`
    (80 lines)
  - `src/mediaman/web/routes/settings/__init__.py:292 api_test_service`
    (88 lines)
  - `src/mediaman/web/routes/kept.py:244 api_keep_show` (94 lines; coordinate
    with W1.8 transaction-tightening)
  - `src/mediaman/web/routes/download/status.py:149 _radarr_status` (82
    lines; the rationale block at line 233 covers only `_sonarr_status`)
  - `src/mediaman/web/auth/password_hash.py:148 authenticate` (83 lines)
  - `src/mediaman/services/arr/_transport.py:132 _unmonitor_with_retry` (64
    lines; marginal — split or rationale)
- **Acceptance:** each ≤60 lines OR carries a specific, non-tautological
  `# rationale:`-style comment explaining why.

### W1.28 — Split search.js

- **Files:** `src/mediaman/web/static/js/search.js` (449 lines).
- **Edit:** split into `src/mediaman/web/static/js/search/shelves.js` and
  `src/mediaman/web/static/js/search/detail_modal.js` per PLAN.md Phase 8.
  Keep `search.js` as a thin bootstrap that imports both.
- **Acceptance:** no JS file > 400 lines after this; search page visual diff
  clean.

### W1.29 — Unify buildHeroCard / buildHeroPlaceholder

- **Files:**
  - `src/mediaman/web/static/js/download.js:82-187` (~105 lines,
    `buildHeroCard`)
  - `src/mediaman/web/static/js/downloads/build_dom.js:134-204`
    (`buildHeroPlaceholder`)
- **Edit:** extract a shared `MM.downloads.buildHero(state, item)` (or
  similar) that returns the hero DOM tree. Both pages call it.
- **Acceptance:** zero duplicate `<div class="dl-hero">...</div>` builders.

### W1.30 — Re-decompose `run_scan` + `_scan_library.scan_items`

- **Files:**
  - `src/mediaman/scanner/engine.py:239 run_scan` (107 lines)
  - `src/mediaman/scanner/_scan_library.py:37 scan_items` (98 lines)
- **Edit:** restore the per-phase helpers `_sync_phase_fetch`,
  `_sync_phase_write`, `_scan_all_libraries`, `_cleanup_orphans_per_library`,
  `_record_deletion_outcome`, `_run_post_scan_followups`,
  `_apply_scan_decision`, `_evaluate_scan_item` that 8f2e2d2 extracted and
  d4dd57d re-inlined. Place them in `engine.py` or `_scan_library.py` — **not
  in `_post_scan.py`** which W1.4 deletes.
- **Acceptance:** `run_scan` and `scan_items` both ≤60 lines.

### W1.31 — bootstrap/db.py test-patch cleanup

- **Files:** `src/mediaman/bootstrap/db.py:17-19, 77-83`.
- **Edit:** drop `tempfile`, `_assert_data_dir_writable`, `_remediation_for`
  from `__all__` (private symbols don't belong there;
  `DataDirNotWritableError` can stay if it's a public domain exception).
  Update tests that `patch.object(bootstrap_db_mod.tempfile, ...)` to patch
  the real path (`mediaman.bootstrap.data_dir.tempfile` or wherever the
  symbol genuinely lives).
- **Acceptance:** `__all__` contains only intended public names; tests pass
  after patch-path changes.

### W1.32 — SSRF allowlist wiring (production enforcement)

- **Files:**
  - `src/mediaman/services/infra/http/client.py:408` (the chokepoint)
  - `src/mediaman/services/infra/url_safety.py:122-161` (the helper)
  - `src/mediaman/web/routes/poster/fetch.py:126, 152`
  - `src/mediaman/web/routes/settings/core.py:94`
  - `src/mediaman/services/media_meta/plex.py:107`
  - `src/mediaman/services/media_meta/_plex_session.py:110`
- **Edit:**
  1. Add an `allowed_hosts: frozenset[str] | None = None` parameter to
     `SafeHTTPClient.__init__` (preferred — derive once at boundary). When
     non-None, every `_resolve(url)` call enforces it. When None, fall back
     to the deny-list.
  2. At every outbound call site, compute the allowlist via
     `allowed_outbound_hosts(conn)` and pass it to the client constructor.
  3. Fix the partial-population bug at
     `services/infra/url_safety.py:147-152`: on `sqlite3.Error` during
     settings read, the function currently logs a warning and continues
     with a half-built allowlist. Either return
     `frozenset(PINNED_EXTERNAL_HOSTS)` on any DB error (fail-closed per
     the docstring) or rewrite the docstring to admit partial population.
     **Prefer fail-closed** — the comment promises it.
  4. Add a test that asserts a production call with a configured-but-spoofed
     `plex_url` resolves to the allowlist and refuses an off-allowlist host.
- **Acceptance:**
  - Every outbound call in `web/routes/` and `services/media_meta/` passes
    `allowed_hosts` (verifiable by `rg "SafeHTTPClient\(" src/`).
  - §10.6 head-line is now enforced for the chokepoint paths.
  - `allowed_outbound_hosts` returns the pinned-only set on DB error.

### W1.33 — services/infra public-surface adoption (~80 callers)

Five subagents in parallel, one per directory:

- **W1.33a — `src/mediaman/web/` callers (largest, ~30 sites)**
- **W1.33b — `src/mediaman/services/` callers excluding `infra/` (~20)**
- **W1.33c — `src/mediaman/scanner/` callers (~10)**
- **W1.33d — `src/mediaman/bootstrap/` + `crypto/` + `db/` callers (~10)**
- **W1.33e — `tests/` callers (~34)**

- **Edit per subagent:** rewrite `from mediaman.services.infra.<submod>
  import X` → `from mediaman.services.infra import X` for every X re-exported
  by `services/infra/__init__.py`. The public list is in `__init__.py:55`.
  Also rewrite the `services/infra/__init__.py:17-29` docstring — replace the
  "keep doing so for readability" guidance with the §1.7-compliant "use the
  public surface" instruction.
- **Acceptance:** zero `from mediaman.services.infra.<submod> import` in
  production code; tests follow the same rule unless an in-test private
  symbol is genuinely needed (with a 1-line per-site comment).

### W1.34 — Test factory writers + adoption

- **Files:** `tests/helpers/factories.py` + sweep.
- **Edit:**
  - Add factory writers: `insert_settings`, `insert_audit_log`,
    `insert_kept_show`, `insert_subscriber`, `insert_suggestion`,
    `insert_recent_download`, `insert_download_notification`,
    `insert_admin_user`. Each takes `(conn, **fields) -> id` like the
    existing `insert_media_item`.
  - Sweep raw `INSERT INTO` in `tests/unit/` and `tests/integration/`.
    Current count: 192. Target: <30 with per-site rationale.
  - Adopt existing `insert_media_item` / `insert_scheduled_action` in the
    per-file `_insert_*` shims at:
    - `tests/unit/web/test_security_findings.py:36-62`
    - `tests/unit/web/test_keep_route.py:32-50`
    - `tests/unit/web/test_history.py:16, 116, 132`
    - `tests/unit/scanner/test_engine.py:424, 442, 999, 1015`
    - `tests/unit/scanner/test_repository.py:24, 57`
- **Acceptance:** raw `INSERT INTO` in tests <30; per-file `_insert_*` shim
  count = 0.

### W1.35 — Status-code assertion pinning

- **Files:** 18 sites of `assert resp.status_code in (...)` in tests.
- **Edit:** pin each to a single exact code. Adopt `parametrise_status_codes`
  helper (`tests/conftest.py:348`) where it fits, or delete the helper if it
  doesn't (orchestrator decides after seeing first three adoption attempts).
- **Acceptance:** zero `status_code in (...)` outside conftest; helper either
  has live callers or is deleted.

### W1.36 — Freezer adoption (worst-offender clocks)

- **Files:**
  - `tests/unit/scanner/test_engine.py`
  - `tests/unit/auth/test_login_lockout.py` (note: also being moved in W2.1a;
    coordinate)
  - `tests/unit/auth/test_session_store.py` (same)
  - `tests/unit/web/test_recommended_refresh_rate_limit.py`
- **Edit:** migrate `datetime.now(UTC)` / `time.time()` / `time.sleep()`
  clusters to the `freezer` fixture (`tests/conftest.py:323`). Document any
  case freezer doesn't fit.
- **Acceptance:** the four files use `freezer.move_to()` / `freezer.tick()`;
  `freezer` is no longer 0-adopter dead code.

### W1.37 — `templates_stub` fixture + `_make_app` dedup

- **Files:**
  - `tests/conftest.py` — add a `templates_stub` fixture wrapping the
    JSON-echoing mock templates currently re-implemented in 4 places.
  - `tests/unit/web/test_force_password_change.py:24`
  - `tests/unit/web/test_download_token_page.py:18`
  - `tests/unit/web/test_page_session_binding.py:20`
  - `tests/unit/web/download/test_confirm.py:32`
- **Edit:** replace each `_make_app` template-stub copy with the shared
  fixture. Address the 10 thin `_app` wrappers from the Phase 2 review the
  same way — inline at call site, or make a higher-order fixture.
- **Acceptance:** zero `_make_app` redefs that wrap `app_factory`; zero
  template-stub JSON-echo duplicates.

### W1.38 — Integration `test_keep_flow.py` migration

- **Files:** `tests/integration/test_keep_flow.py:23, 25-58, 86-103, 127-138,
  181-192`.
- **Edit:** `TestKeepFlow.test_full_keep_lifecycle` still calls
  `init_db(str(db_path))` while sibling classes use the `conn` fixture.
  Unify. Migrate 8 raw INSERTs to the new factory writers (depends on
  W1.34).
- **Acceptance:** file uses `conn` fixture throughout; raw INSERTs gone.

### W1.39 — Security-perimeter middleware tests

- **Files:**
  - `tests/unit/web/middleware/test_csrf.py` (NEW)
  - `tests/unit/web/test_middleware_is_admin.py` (extend)
- **Edit:**
  - New `test_csrf.py` at the canonical mirror path. Cover the exempt-routes
    allowlist, origin checks, double-submit cookie logic. CSRF middleware
    source is `src/mediaman/web/middleware/csrf.py` (224 lines).
  - Extend `test_middleware_is_admin.py` to cover the other four callables
    in `web/auth/middleware.py`: `get_current_admin`, `get_optional_admin`,
    `get_optional_admin_from_token`, `resolve_page_session`.
- **Acceptance:** new test file exists; middleware coverage is comprehensive
  (no callable in `web/middleware/csrf.py` or `web/auth/middleware.py`
  untested).

### W1.40 — Module-level state explanatory comments

Per intentional decision, the literal `# rationale:` keyword is dropped. But
§8.5 still requires an explanatory comment. Sites currently with bare
declarations:

- **Files:**
  - `src/mediaman/web/auth/session_store.py:44-46`
  - `src/mediaman/web/routes/download/submit.py:30`
  - `src/mediaman/web/routes/download/status.py:38`
  - `src/mediaman/web/routes/subscribers.py:62`
  - `src/mediaman/web/auth/_password_hash_helpers.py:109-110`
  - `src/mediaman/services/infra/http/dns_pinning.py:55-58`
- **Edit:** add a 1-line comment above each declaration naming why a global
  is required (rate-limiter scope, dummy-hash for timing-attack mitigation,
  DNS pin cache rationale, etc.).
- **Acceptance:** every module-level mutable state in those files has an
  explanatory comment.

---

## Wave 2 — Phase 11 test refactor (full scope, parallel)

Eight task groups; one or more subagents per group. Wave 1 must be green
first because most W2 tasks operate on test files whose fixtures Wave 1
finishes adopting.

### W2.1 — Mirror src layout

One subagent per top-level package, parallel-safe (no file overlap):

- W2.1a — `tests/unit/auth/*` → `tests/unit/web/auth/*`
- W2.1b — Tests for `src/mediaman/core/` → `tests/unit/core/`
- W2.1c — Tests for `src/mediaman/crypto/` → `tests/unit/crypto/`
- W2.1d — Tests for `src/mediaman/db/` → `tests/unit/db/`
- W2.1e — Tests for `src/mediaman/web/middleware/` → `tests/unit/web/middleware/`
- W2.1f — Tests for `src/mediaman/web/routes/` → `tests/unit/web/routes/`
- W2.1g — Tests for `src/mediaman/web/repository/` → `tests/unit/web/repository/`
- W2.1h — Tests for `src/mediaman/services/infra/` →
  `tests/unit/services/infra/`
- W2.1i — Tests for `src/mediaman/services/mail/` →
  `tests/unit/services/mail/`
- W2.1j — Tests for `src/mediaman/services/media_meta/` →
  `tests/unit/services/media_meta/`
- W2.1k — Tests for `src/mediaman/services/openai/` →
  `tests/unit/services/openai/`

- **Method:** `git mv` to preserve history. Update conftest import paths if
  they were location-dependent. Run `pytest` after each subagent commits to
  catch import-path bugs early.
- **Acceptance:** every `src/mediaman/<pkg>/<mod>.py` has a corresponding
  `tests/unit/<pkg>/test_<mod>.py` OR an explicit not-a-mirror note in
  CODE_GUIDELINES.md §16.

### W2.2 — Split the three giants

Three parallel subagents (no file overlap):

- W2.2a — `tests/unit/web/test_downloads_api.py` (1974 → 4-6 files): split
  by endpoint group (`submit`, `confirm`, `status`, `token`, `abandon`).
  Class names should be preserved.
- W2.2b — `tests/unit/scanner/test_engine.py` (1664 → per-phase): split by
  scan phase (`fetch`, `write`, `cleanup`, `decision`).
- W2.2c — `tests/unit/services/test_arr_search_trigger.py` (1361 →
  per-concern): split by concern (`throttle`, `search`, `backoff`).
- **Acceptance:** each resulting file ≤500 lines; original tests preserved
  (not renamed or dropped).

### W2.3 — Distribute security-by-batch test files

- **Files:** `tests/unit/test_security_hardening.py`,
  `test_security_hardening_r2.py`, `tests/unit/web/test_security_findings.py`.
- **Edit:** distribute each test into the module-level test file that owns
  the behaviour. Delete the originals after distribution.
- **Acceptance:** the three by-batch files no longer exist.

### W2.4 — Replace `setup_method` with autouse fixtures

One subagent per cluster (group by file to avoid conflicts after W2.1 moves):

- W2.4a — Tests in `tests/unit/web/test_downloads_api.py` (may be on
  W2.2a's split files instead)
- W2.4b — `tests/unit/web/test_library_mutations.py`
- W2.4c — `tests/unit/web/test_auth_routes_coverage.py`
- W2.4d — `tests/unit/web/download/test_submit.py`
- W2.4e — `tests/unit/web/test_download_token_page.py`
- W2.4f — Remaining 41 sites — split by directory after W2.1 completes.
- **Acceptance:** `rg "def setup_method\(self," tests/` returns zero.

### W2.5 — Stop importing private symbols

One subagent per directory after W2.1:

- W2.5a — `tests/unit/crypto/test_crypto.py` — 19 underscored imports from
  `crypto._aes_key`, `crypto.tokens`. Rewrite to test via the public crypto
  surface (`encrypt_value`, `decrypt_value`, `encode_signed`).
- W2.5b — `tests/unit/scanner/test_engine.py` siblings — engine internals.
  Coordinate with W1.4 (dead-target patches already rewritten there).
- W2.5c — `tests/unit/services/test_arr_search_trigger.py` siblings —
  private throttle helpers. Test through `services/arr/`'s public methods.
- W2.5d — `tests/unit/web/test_security_*.py` — private `_CSP`,
  `_UNSUB_LIMITER`, `_TEST_CACHE`, `_load_settings`. After W2.3
  redistribution, rewrite each.
- W2.5e — Remaining ~80 sites. Split by file/directory.
- **Edit:** rewrite each test to exercise behaviour through public callers.
  Where genuinely impossible, document with a 1-line per-site rationale.
- **Acceptance:** `rg "from mediaman\.[a-z_.]+\._[a-z]" tests/` ≤ 20, each
  with documented rationale.

### W2.6 — Parametrise

- **Files:**
  - `tests/unit/services/test_url_safety.py` (~68 tests → ~3 parametrised
    cases per concern)
  - `tests/unit/services/test_mailgun.py` (~12 → ~3)
  - HTTP-status assertion families across the suite (use
    `parametrise_status_codes` from conftest if W1.35 kept it).
- **Acceptance:** `test_url_safety.py` ≤100 lines; `test_mailgun.py` shorter
  than baseline; `parametrise_status_codes` has live callers (or has been
  deleted in W1.35).

### W2.7 — Eliminate ResourceWarning ignore

- **Files:** `pyproject.toml:96` and connection-leak sources.
- **Edit:** the `"ignore::ResourceWarning"` filter is a temporary patch over
  real connection leaks. Run `pytest -q -W default::ResourceWarning` to
  surface offenders; fix leaks at source (likely in `db/connection.py` and
  any test fixtures that build SQLite connections without `close()`). Once
  the suite is clean of new `ResourceWarning`s, remove the filter line.
- **Acceptance:** `pyproject.toml` no longer carries
  `"ignore::ResourceWarning"`; no test emits the warning.

### W2.8 — Coverage floor bump

- **Files:** `pyproject.toml:166` (`fail_under = 58`).
- **Edit:** after W2.1-W2.7 land, run `coverage report` to determine the
  actual achieved coverage. Raise `fail_under` to `(actual - 1)%` floor (per
  CLAUDE.md "up, never down" rule). Add a comment naming the target.
- **Acceptance:** `fail_under` ≥ 60; per-file gaps documented in the
  pyproject comment.

---

## Wave 3 — Tightening (after Wave 2 green)

### W3.1 — Enable mypy `strict = true`

- **Files:** `pyproject.toml` (mypy config block at line ~145).
- **Edit:** enable `strict = true`. This will surface many new errors.
  Approach: run `mypy --strict src/mediaman/<pkg>` per top-level package and
  fix per-package. Allocate one subagent per package, parallel:
  - W3.1a — `core/`, `bootstrap/`, `db/`, `crypto/` (likely small surface)
  - W3.1b — `services/arr/` (largest after W1.17-W1.19)
  - W3.1c — `services/downloads/`
  - W3.1d — `services/mail/` + `services/openai/` + `services/media_meta/`
  - W3.1e — `services/infra/` + `services/scheduled_actions.py`
  - W3.1f — `scanner/`
  - W3.1g — `web/auth/` + `web/middleware/`
  - W3.1h — `web/routes/` (likely many errors; may need sub-splitting)
  - W3.1i — `web/repository/` + remaining `web/` top-level
- **Acceptance:** `mypy --strict src/mediaman` returns 0 errors.

### W3.2 — Final consistency check

- **Action:** dispatch one Opus 4.7 reviewer with the "holistic" prompt from
  the original 14-agent review (see `docs/remediation/REVIEW_NOTES.md` or
  re-derive from PLAN.md + this document). Verify every blocker resolved.
- **Acceptance:** reviewer reports no blockers; final gate is green.

---

## Conflict graph notes for the orchestrator

Tasks that share files (cannot run in parallel even within their wave):

- **W1.4** (delete `_post_scan.py` + rewrite test mocks) and **W1.30**
  (re-decompose `run_scan`): both touch scanner test mocks. Run W1.4 first.
- **W1.11** (re-narrow `_transport.py`) and **W1.19** (arr re-cast cleanup):
  both touch `services/arr/_transport.py`. Run W1.11 first.
- **W1.17** (notifications typing) and **W1.24** (`now_utc` sweep): both
  touch `services/downloads/notifications.py`. Run W1.17 first.
- **W1.18** (poster typing) and **W1.5** (wire poster repo) and **W1.10**
  (poster narrow catches): all touch `web/routes/poster/fetch.py`. Run
  W1.5 → W1.10 → W1.18.
- **W1.27** (`api_keep_show` split) and **W1.8** (`kept.py` transactions):
  both touch `web/routes/kept.py`. Run W1.8 first.
- **W1.32** (SSRF wiring) and **W1.10** (poster narrow): both touch
  `web/routes/poster/fetch.py`. Run W1.32 after W1.10.
- **W2.1** (mirror moves) and **W2.4** (setup_method removal): same files.
  Run W2.1 first.
- **W2.5** (private-symbol cleanup) depends on W2.1 (file paths change).
- **W2.6** (parametrise `test_url_safety.py`) depends on W2.5d (private
  imports out).
- **W2.7** (ResourceWarning) and **W2.5** (private imports): touch many of
  the same test files. Run W2.5 first.

Tasks free to run in parallel within their wave (no conflicts):

- W1.1, W1.2, W1.3, W1.6, W1.7, W1.9, W1.12, W1.13, W1.14, W1.15, W1.16,
  W1.20, W1.21, W1.22, W1.23, W1.25, W1.26, W1.28, W1.29, W1.31, W1.33a-e,
  W1.34, W1.35, W1.36, W1.37, W1.38, W1.39, W1.40 — distinct file sets.

---

## Estimated scope

- Wave 0: 3 tasks, ~1-2 h serial.
- Wave 1: 40 tasks, ~25-40 h with parallelism.
- Wave 2: 8 task groups (~25-35 subagents total), ~20-30 h with parallelism.
- Wave 3: 2 tasks (W3.1 has 9 sub-tasks), ~10-15 h.

Realistic wall-clock with full multi-agent parallelism: **3-5 working
sessions**.
