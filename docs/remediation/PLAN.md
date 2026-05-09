# mediaman Remediation Plan

Status: **Pending user approval**. Do not execute until approved.

Baseline: Python 3.12.9 (matches `Dockerfile@python:3.12.9-slim`); `pytest -q` reports **2504 passed**. Every phase below ends with a green test run; any regression blocks phase completion.

Authoritative reference: `CODE_GUIDELINES.md` (kept as-is — guidelines are good; the gap is compliance).

---

## Verdict on `CODE_GUIDELINES.md`

**Do not rewrite.** The doc is well-structured, has examples, has cross-references, has explicit exception clauses. Rewriting would be churn. What it admits in its own preamble — "aspirational, not retrospective; carve-outs exist" — needs closing via *code changes*, not doc edits. After this remediation, drop the "aspirational" framing and tighten §3.1's carve-out language.

Two minor edits proposed *after* code is compliant:
- §2.1 — admit that `core/url_safety.py` legitimately uses `idna` and DNS, or move the module to `services/infra/`.
- §2.2 — admit that `crypto/` reads its own canary/salt rows, or move that I/O to `db/`.

---

## Findings overview

Six parallel agents produced exhaustive findings. Aggregated, deduplicated, ranked:

| Category | Count | Worst items |
|---|---|---|
| Architectural boundary violations | 56 SQL-in-routes, 6 routes-import-routes, 7 files >500 lines without rationale | route layer is shot through with raw SQL |
| Duplication (Python) | 50+ extractable patterns: keep-decision, ISO parsing, retry, HTTP boilerplate, format helpers | `relative_day_label`, `now_utc`, `resolve_keep_decision`, `parse_iso_strict_utc` |
| Duplication (frontend) | 35 raw `fetch` bypassing `MM.api`; 5 hand-rolled modals; 4 state-label maps; 0 callers of `c.btn`/`c.fpill`/`c.tile` macros | macro layer is dead weight; core JS modules unadopted |
| Duplication (CSS) | ~1200 lines deletable: dead `.lib-table*`, `.sug-*`, `.users-*` v1, `.deleted-*`, `.subscriber-*` v1, `.disk-*` v1, `.media-card`, `.btn-keep`/`.btn-sm-*` legacy aliases | `_base.css`/`_settings.css` are bloated with dead v1 systems |
| Overcomplication | `_ArrClientBase` (one subclass), `TokenSpec`/`Tokens` namespace, `strict_egress` plumbing, `_SALT_CACHE_MAX=4` LRU, `MM.tiles.render`/`MM.modal.setupDetail` unused | back-compat shims around recent splits |
| Critical security/safety | 1 PII leak, 1 silent swallow in production, 3 missing `# nosec` rationales | `audit_log` stores full email; `db/connection.py:117-118` |
| Error handling | 52 `except Exception:` (only ~5 at the four allowed §6.4 sites); 30+ `RuntimeError`/`ValueError` outside arg validation | `_throttle_persistence.py` comment claims fix; lines 91/144/243 still broad |
| Module mutable state | 11+ sites missing `# rationale:` keyword | rate-limiters, throttle state, caches |
| Type annotations | 30+ `Any` without `# rationale:` | most in `services/arr/`, `services/downloads/notifications.py` |
| Tests | mirror broken (142/151 src files have no test at canonical path); 30+ `_make_app` redefs; 25+ `_auth_client` redefs; 7 `parametrize` total; 36 `setup_method`; 110+ wall-clock-dependent tests; 42 tests with no assertions | suite is large but verbose and brittle |
| Oversized files | Python: 7 files >500; CSS: `_base.css` 1488, `_settings.css` 1247; JS: `settings/general.js` 834, `downloads.js` 713, `recommended.js` 616 | most sit on 500-line ceiling |
| Oversized functions | `api_media_redownload` ~178, `api_media_delete` ~171, `maybe_trigger_search` ~171, `_safe_rmtree` ~163, `change_password` ~145 | several have `# rationale:`; all are decomposable |

---

## Execution phases

Each phase ends with `pytest -q` green and a separate commit. Phases roughly proceed from highest blast-radius / lowest blast-radius to invasive structural moves. Phase numbering does NOT imply parallelism — phases run sequentially, sub-tasks within a phase may run in parallel.

### Phase 0 — Establish trust (DONE)
- ✅ Python 3.12.9 venv (`.venv/`) with `pip install -e ".[dev]"`
- ✅ Baseline: 2504 tests passing
- ✅ All 6 audit reports complete

### Phase 1 — Critical security & safety (estimated: 1-2h)

**Must land first.** These are in production and affect users now.

1. **PII leak in audit log** — `web/routes/download/submit.py:280` interpolates full email into `audit_detail`. Fix: scrub via `_mask_email_log` before composing.
2. **Silent swallow in production** — `db/connection.py:117-118` `except Exception: pass`. Fix: replace with `logger.exception` and either move `_reset_state_for_tests` out of production code or document why broad catch is correct in this one site.
3. **`# nosec` markers without `# rationale:`** — `main.py:111`, `services/infra/storage.py:42,50` — add the literal keyword.
4. **`audit_log` schema lacks `user_agent`** — write migration `0036_audit_log_user_agent.py`; thread `user_agent` through `security_event_or_raise` and `log_audit`.
5. **Module-level mutable state without `# rationale:` keyword** — 11 sites; add the keyword. Mostly mechanical (rate-limiter singletons, dashboard disk-usage cache, search dedup, throttle state).

### Phase 2 — Test infrastructure (estimated: 3-4h)

Tests must be fixable before we start moving code, otherwise we cannot verify regressions cleanly.

1. **Add `tests/unit/web/conftest.py`** with shared fixtures:
   - `app_factory(*routers)` replacing 30+ `_make_app` redefs
   - `authed_client(app, conn, *, with_reauth=False)` replacing 25+ `_auth_client` redefs
2. **Add `tests/helpers/factories.insert_*`** — wrap existing dict factories with `(conn, **fields) -> id` writers; sweep 99 raw `INSERT INTO ...` calls.
3. **Inject clock** — add a `clock` pytest fixture (or adopt `freezegun`); migrate the worst offenders first (`test_engine.py`, `test_login_lockout.py`, `test_session_store.py`, `test_recommended_refresh_rate_limit.py`).
4. **Pin vague status assertions** — 14+ `assert resp.status_code in (...)` → exact code.
5. **Add a few missing tests for security perimeter modules** (`web/auth/middleware.py`, `web/middleware/csrf.py`) at canonical mirror paths — but defer the broad mirror reorganisation to Phase 7.

After this phase, future code refactors can rely on shared fixtures and exact assertions.

### Phase 3 — Eliminate ceremonial back-compat (estimated: 4-5h)

Pure deletion / consolidation. Each is mechanically reversible if anything breaks.

1. **CSS dead-code purge** (~1200 lines deletable):
   - `_base.css:649-833` — `.lib-table`/`.lib-row`/`.lib-header` legacy grid (178 lines, zero callers).
   - `_keep.css:4-34` — `.deleted-*` aliases (zero callers).
   - `_tiles.css:181-251` — `.media-card` (zero callers).
   - `_tiles.css:332-435` — `.sug-*` (~100 lines, zero callers).
   - `_settings.css:75-460` — v1 settings system (~400 lines): `.settings-section*`, `.subscriber-list/row/email/status`, `.users-self-card/list-card/...`, `.disk-row/path-input/threshold-input`, `.btn-add`/`.btn-save`/`.save-bar`.
   - `_buttons.css:88-115` — "legacy button aliases" block (file calls itself out).
   - `_base.css:1101-1136` — `.btn-download.*` (refactor JS first to emit `.btn .btn--primary`).
   - `_dl.css:510-525` — `.btn-download-hero`.
   - `_base.css:550-584` — `.toggle-switch*` (zero callers; `.tog` is canonical).
   - `_base.css:853-909` — `.history-table*` (zero callers; `.hist` is canonical).
   - `_base.css:1063` — wrong DESIGN.md cite.
   - Add `--rgba-warning-bg`, `--rgba-purple-bg`, `--rgba-orange-bg`, `--glow-success/warning/danger` tokens.
2. **JS deletions**:
   - `MM.tiles.render` (162 lines), `MM.modal.setupDetail` (113 lines), `MM.dom.findByAttr`/`setText`/`delegate` if not migrated to.
   - Note: these are legitimately useful primitives. Decision matrix: **migrate** (Phase 4) rather than delete. So this phase only deletes the duplicate per-page micro-helpers (`q`/`setText`/`findByDlId` etc. in 4 files).
3. **Python deletions**:
   - `web/__init__.py:21-50,112-138` — 15 back-compat re-exports (update test files).
   - `services/infra/http/__init__.py:16-68` — 14 underscore re-exports (update tests).
   - `crypto/__init__.py:11-49` — 8 private re-exports (update tests).
   - `web/auth/reauth.py:147` — `_require_reauth` alias (zero callers).
   - `services/infra/settings_reader.py:131-132` — `min`/`max` parameters on `get_int_setting`.
   - `services/infra/http/client.py:190` + `services/media_meta/_plex_session.py:89` — `strict_egress` parameter.
   - `scanner/phases/evaluate.py:47` — dead `episode_count` parameter.
   - `scanner/phases/evaluate.py:49` — dead `has_future_episodes` branch (and three tests that exist solely to exercise it).
   - `services/media_meta/_plex_types.py:77-97` — `_to_utc` identical-branch tautology.
   - `crypto/_aes_key.py:113-139` — `_SALT_CACHE_MAX=4` LRU → single-entry cache.
   - `crypto/tokens.py:271-329` — `TokenSpec`+`Tokens` namespace; collapse 5 shims to call `_encode_signed` directly.
   - `services/arr/_throttle_state.py:100-120` — `_search_backoff_seconds` reaching into `_deterministic_multiplier`; route through public `ExponentialBackoff.delay()` (or delete `ExponentialBackoff` if it has no other consumers).

### Phase 4 — Adopt shared infrastructure (estimated: 6-8h)

Consolidate duplicated patterns onto existing primitives.

1. **JS raw `fetch` → `MM.api`** (35 sites in 7 files): `recommended.js` (7), `library.js` (7), `dashboard.js` (5), `download.js` (2), `downloads.js` (1), `protected.js` (2), `force-password-change.js` (1), `dl-abandon.js` (1).
2. **Modal lifecycle → `MM.modal.setupDetail`** (5 sites): `recommended.js`, `search.js`, `library.js` (×2), `dl-abandon.js`.
3. **Tile/poster card → `MM.tiles.render`** (3 sites: `core/tiles.js`, `search.js:52-73`, `recommended.js:244-270`).
4. **Templates → Jinja macros** — DESIGN.md mandate. `c.btn`, `c.fpill`, `c.tile`, `c.empty` currently have ZERO callers. Convert templates: `dashboard.html`, `library.html`, `protected.html`, `keep.html`, `_dl_*.html`, `settings/_sec_integrations.html` (8 service-card blocks → new `service_card` macro).
5. **Python consolidation**:
   - Add `mediaman.core.time.now_utc()` and `parse_iso_strict_utc()`; sweep 30+ `datetime.now(UTC)` and 5 strict-parse sites.
   - Extract `resolve_keep_decision(duration, *, now)` (3 call sites: `library_api`, `kept`, `scheduled_actions`).
   - Extract `relative_day_label(execute_at, now, *, today, tomorrow, future, past=None)` (3 sites: `format_expiry`, `_protection_label`, `_days_until`).
   - Replace `kept.py:218-228` inline relative-date with existing `core/format.days_ago`.
   - Fold `services/mail/mailgun.py:38-69` retry into `services/infra/http/retry.py:dispatch_loop` (add `jitter_strategy` parameter).
   - Replace `web/routes/keep.py` 8 `HTMLResponse('{"error":...}')` with `respond_err` (also fixes content-type bug).
   - Extract `services/media_meta/tmdb.py:_get_results(path, *, params, label)` for 4 list-paged endpoints.
   - Promote `_mask_email`/`_mask_email_log` to `core/format.py`.
   - Drop the duplicate `setg_card_v2`/`setg_row_v2` Jinja macros — unify with `setg_card`/`setg_row` (decide which is canonical, sweep callers).
6. **Cross-stack `dl_state_label`** — emit Python source-of-truth as a JSON island consumed by `download.js`, `downloads.js`, `_dl_compact_row.html`, `_dl_hero_card.html`. Eliminates 4-site drift.

### Phase 5 — Move SQL out of route handlers (estimated: 6-8h)

The largest §2.7.1 violation cluster. 56 `conn.execute` calls in 17 route files. New repository modules where missing.

Targets, by severity:
- `web/routes/library_api/__init__.py` (10 calls + `BEGIN IMMEDIATE` orchestration) → extend `web/repository/library.py`
- `web/routes/library_api/delete_intents.py` (10 calls — *whole file* is repository-shaped) → relocate as `web/repository/delete_intents.py` with thin route wrapper
- `web/routes/settings/__init__.py` (8 calls incl. `_load_settings`) → new `web/repository/settings.py`
- `web/routes/scan.py:147-165` → use existing repository + `with conn:` instead of manual transaction
- `web/routes/dashboard/_data.py` (4 SQL blocks; despite docstring claiming queries live in `_data.py`) → move to `web/repository/dashboard.py`
- `web/routes/users/crud.py:226` (§2.7.4 — only `web/auth/` may touch `admin_users`) → move to `web/auth/password_hash.py`
- `web/routes/kept.py` (5 calls) → use `web/repository/kept.py`
- `web/routes/library.py` (5 calls) → use repository
- `web/routes/subscribers.py:120-137` → drop manual transaction; rely on `web/repository/subscribers.py`
- `web/routes/poster/__init__.py` (3 calls) → split into poster repository
- `web/routes/dashboard/_data.py` (4 calls) → repository
- `web/routes/download/_tokens.py` (3 calls), `web/routes/download/status.py` (2), `web/routes/search/_enrichment.py` (2), `web/routes/download/confirm.py` (1), `web/routes/recommended/_query.py` (1), `web/routes/settings/secrets.py` (1) → repository moves

Also move `scanner/repository/library_query.py` → `web/repository/library_query.py` (its docstring admits it's web-facing).

### Phase 6 — Fix routes-to-routes imports (estimated: 1h)

§2.8.6 violations. 6 sites import `mediaman.web.routes._helpers`.

- Move `set_session_cookie` (`web/routes/_helpers.py:24-31`) → `web/cookies.py` (new), or fold into `web/auth/middleware.py`.
- Move `is_admin` (same file) → `web/auth/middleware.py` (it already wraps `get_optional_admin_from_token`).
- Move `is_request_secure` (`web/routes/auth.py`) → `web/_helpers.py` so the function-local imports in `force_password_change.py:179`, `users/passwords.py:93`, `users/sessions.py:98` disappear.
- Delete `web/routes/_helpers.py`.

### Phase 7 — Reorganise top-level modules (estimated: 2-3h)

§2 taxonomy is incomplete. 5 top-level files unclassified.

1. `audit.py` → `core/audit.py` (depends only on `core.time`; imported by 17 files at every layer).
2. `validators.py` → `bootstrap/validators.py`.
3. `app_factory.py` — split: keep `lifespan` and `create_app` at top-level (FastAPI surface); move `bootstrap_db` and `bootstrap_crypto` into `bootstrap/db.py` and `bootstrap/crypto.py` (currently shims that re-export from `app_factory`). Removes the bootstrap circular wiring.
4. `db/migrations/v35.py` → `db/migrations/0035_aes_v1_sunset.py` (fix non-conformant filename per §9.2).
5. Re-export `services/infra` settings/storage/path-safety/url_safety from package `__init__.py` so 22 sites stop bypassing the public surface.

### Phase 8 — Decompose oversized files (estimated: 6-8h)

These split cleanly along responsibility seams identified by the audits.

**CSS** (`_base.css` and `_settings.css` shrink dramatically once dead code is gone in Phase 3; remaining work is to peel off concerns):
- `_base.css` → `_tables.css` (`.tbl`), `_storage.css`, `_modal.css` (the 383-line modal block), `_login.css`, `_history.css`. Target: <700 lines.
- `_settings.css` → split v2 `.setg-pg` block to `_settings_v2.css`. Target: <500 lines each side.
- `_dl.css` (655) → split `download.html` page styles to `_download_page.css`.
- `_tiles.css` (589) → after dead-code removal, ≈350 lines, fits.

**JS**:
- `settings/general.js` (834) → `settings/savebar.js`, `settings/disk_thresholds.js`, `settings/overview.js`, `settings/toggles.js`. Core retained: bootstrap + `collectSettings` + wiring.
- `downloads.js` (713) → `downloads/poll.js`, `downloads/render_hero.js`, `downloads/render_row.js`, `downloads/render_recent.js`.
- `recommended.js` (616) → `recommended/refresh.js`, `recommended/modal.js`, `recommended/poll.js`.
- `search.js` (491) → `search/shelves.js`, `search/detail_modal.js`.
- `download.js` ↔ `downloads.js` — share the `buildHeroCard` / `buildHeroPlaceholder` near-duplicate.

**Python**:
- `services/arr/base.py` (580) — split per logical concern (Sonarr-only, Radarr-only, shared) once `_ArrClientBase` is collapsed in Phase 3.
- `scanner/engine.py` (574) — already documented; keep the existing `# rationale:` but split `_scan_items` (91 lines) and `run_scan` (107 lines) by extracting per-phase helpers.
- `web/routes/library_api/__init__.py` (604) — once SQL moves out (Phase 5), the file shrinks; further split into per-action route files.
- `web/routes/poster/__init__.py` (602) — split into routes vs poster-fetch service vs cache management.
- `web/routes/settings/__init__.py` (536) — once SQL moves out (Phase 5), assess.
- `services/downloads/notifications.py` (506) — add file-level `# rationale:` and break `check_download_notifications` into per-phase helpers.

**Functions** > 60 lines without rationale (Phase 8 also):
- `api_media_redownload`, `api_media_delete`, `maybe_trigger_search`, `_safe_rmtree`, `change_password`, `_sonarr_status`, `api_keep_show`, `_validate_delete_roots`, `api_update_settings`, `validate_session`, `api_test_service`, `_radarr_status`, `_unmonitor_with_retry`, `authenticate`, `check_download_notifications`. Split each by extracting per-branch helpers; transaction-spanning excuse only stands once or twice.

### Phase 9 — Tighten error handling (estimated: 4-6h)

The largest mechanical surface (52 sites). Approach: each broad `except Exception:` either gets narrowed to specific exception types OR earns an explicit `# rationale:` comment naming why broad is correct.

1. **Replace `RuntimeError` rollback sentinels** with private domain exceptions: `library_api/__init__.py:272`, `password_hash.py:417,511`.
2. **Convert `services/infra/storage.py` 24 `raise ValueError(...)` into a `DeletionRefused`/`PathSafetyError` hierarchy.** §6.2 contract.
3. **Convert `services/arr/base.py` 5 `raise ValueError(...)` for upstream protocol errors into `ArrUpstreamError` subclasses.**
4. **Tighten `_throttle_persistence.py:91,144,243`** — the file's own comment block at lines 41-46 explicitly names the right pattern (`(sqlite3.OperationalError, sqlite3.DatabaseError)`).
5. **Tighten `_client_base.py:84,93,103,112`** — replace four `except Exception` with `except (requests.RequestException, SafeHTTPError, ValueError)`.
6. **Tighten route-handler catches** in `subscribers.py:139,259`, `users/crud.py:110,183,254`, `recommended/api.py:261`, `scan.py:104,164,167,200`. Push to FastAPI exception handler where appropriate.
7. **Fix `logger.warning(..., exc)` discarding stack** — 5 sites; replace with `logger.exception(...)` or add `exc_info=True`.
8. **Add `# rationale:` to remaining legitimate broad catches** (cold-start recovery, scheduler job runner, FastAPI exception handler, outermost retry).
9. **Fix `# rationale:` markers on module-level state** that have explanatory comments but lack the literal keyword.

### Phase 10 — Type annotations (estimated: 2-3h)

30+ `Any` annotations without `# rationale:` (§5.5, §16.11).

1. Convert `services/arr/` `dict[str, Any]` to `TypedDict` for Sonarr/Radarr response shapes (§5.3).
2. Convert `services/downloads/notifications.py` `arr: Any`, `movie: Any`, `mailgun: Any` to typed protocols.
3. Add `# rationale:` to legitimately untypeable surfaces (`scanner/fetch.py:45 plex_client: Any`, XML-RPC return).
4. Drop unused `cast(Any, x)` calls.

### Phase 11 — Test mirror & cleanup (estimated: 6-8h)

Now that source has stabilised, restore the mirror.

1. **Move tests to mirror src layout** — ~50 file moves. Examples: `tests/unit/auth/*` → `tests/unit/web/auth/*`, `tests/unit/services/test_format.py` → `tests/unit/core/test_format.py`, `tests/unit/services/test_url_safety.py` → `tests/unit/core/test_url_safety.py`, etc.
2. **Split giants** — `test_downloads_api.py` (1972) → 4-6 files; `test_engine.py` (1661) → per-phase files; `test_arr_search_trigger.py` (1359) → per-concern files.
3. **Distribute `test_security_hardening.py`/`r2.py`/`test_security_findings.py`** into the module-level test files that own each behaviour. These tests-by-audit-batch couple test names to history.
4. **Replace `setup_method` (36 sites) with autouse fixtures.**
5. **Stop importing private symbols** — ~80 sites; rewrite to test through public callers, especially `test_crypto.py`, `test_arr_search_trigger.py`, `test_engine.py`'s pokes into `engine._arr_cache._dates`.
6. **Parametrise** — `test_url_safety.py` (~25 tests → ~3), `test_mailgun.py` (~12 → ~3), HTTP-status assertion families.
7. **Eliminate `ResourceWarning` ignore** in `pyproject.toml` by fixing connection leaks at source.
8. **Coverage floor** — bump from 58% upward as appropriate.

### Phase 12 — Final polish (estimated: 1-2h)

1. Add file-level `# rationale:` headers to remaining oversized files: `services/arr/base.py`, `services/downloads/notifications.py`.
2. Reconcile SSRF allowlist: implement true allowlist per §10.6, OR revise §10.6 to admit the deny-list reality (with rationale).
3. Drop "aspirational" framing from `CODE_GUIDELINES.md` preamble once carve-outs closed.
4. Resolve `core/url_safety.py` placement (§2.1 vs reality of `idna` and DNS).
5. Resolve `crypto/` DB access (§2.2 vs reality of canary/salt).

---

## Execution methodology

- One commit per logical change. Commit messages follow Conventional Commits (`refactor`, `fix`, `test`, etc.).
- After each phase: `pytest -q && ruff check src tests && ruff format --check src tests && mypy src/mediaman` must pass before starting the next phase.
- Use parallel sub-agents only where work is genuinely independent (e.g. CSS deletions, JS migrations across distinct files); sequential where there are ordering dependencies (Phase 5 before 6, Phase 5 before 8 for routes, etc.).
- Sub-agents must NOT fire user-attention alerts.
- No PRs — direct commits to a topic branch (`refactor/code-guidelines-compliance`) that the user reviews when ready. (Confirm before creating any branch.)

---

## Estimated total scope

- ~50 file moves
- ~1200 lines of CSS deleted
- ~700-900 lines of Python deleted/inlined
- ~50 broad `except` sites narrowed
- ~50 SQL calls relocated
- 30+ test files refactored to fixtures
- 5+ migration files (audit_log user_agent, possibly others)

Realistically: **30-50 hours of focused work**, spread across the 12 phases. Phases 0-3 (critical/safety + deletions) are the highest-leverage and lowest-risk; phases 5-8 are the most invasive; phase 11 is large but mostly mechanical.

---

## Decision points for the user

Before execution, confirm:

1. **Approach acceptance**: phased execution as above, with `pytest -q` between phases?
2. **Branch strategy**: single long-lived `refactor/code-guidelines-compliance` branch, or one branch per phase (12 branches, 12 PRs)?
3. **Scope adjustment**: any phase you want skipped, deferred, or attacked first?
4. **Cosmetic/policy questions**:
   - Adopt `freezegun` (new dev dependency) for clock injection in tests?
   - Tokenise the brand colours (`--brand-plex` etc.) or keep the hex literals?
   - SSRF allowlist: implement strict allowlist, or relax §10.6 to admit deny-list reality?
   - CODE_GUIDELINES.md edits: defer to end (Phase 12), or revise upfront?
