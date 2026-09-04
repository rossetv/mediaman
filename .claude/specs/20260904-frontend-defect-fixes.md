# Frontend defect fixes — quick-keep durations, download.js parse error, abandon toast

**Branch:** `fix/frontend-defects`
**Date:** 2026-09-04

Three independently verified, small production defects in the web frontend, fixed and
tested together because they are all client-side JS/template bugs of the same class
(shipped code the Python toolchain never exercises) discovered in the same audit pass.
No architecture change, no new endpoints, no server-side behaviour change.

## Defect 1 — quick-keep duration mismatch

**Problem.** Clicking any quick-keep duration button except "Forever" (dashboard tile
menu, library quick-keep menu, and the per-season picker in the library keep dialog)
returned HTTP 400 `invalid_duration` from `POST /api/media/{id}/keep`.

**Cause.** `POST /api/media/{id}/keep` (`src/mediaman/web/routes/library_api/__init__.py`)
only accepts the keys of `VALID_KEEP_DURATIONS` in `src/mediaman/web/models/_common.py`
— the canonical long-form strings `"7 days"`, `"30 days"`, `"90 days"`, `"forever"`. Four
client-side sources sent the short form (`"7d"`, `"30d"`, `"90d"`) instead:

- `dashboard.html` — `data-duration="7d"/"30d"/"90d"` on the tile snooze menu, plus the
  default Keep button's `data_attrs={"duration": "30d"}`.
- `dashboard.js` — `keepBtn.dataset.duration || '30d'` default fallback.
- `library.html` — `data-keep-dur="7d"/"30d"/"90d"` on two quick-keep menus.
- `library.js` — `submitKeep()`'s non-TV path forwarded the raw short-form `duration`
  argument untouched; the TV path converted short→long via a local `durMap` (so the
  season-picker dialog itself worked); `confirmKeepDialog()`'s per-season fallback loop
  then converted the dialog's already-canonical `_keepDialogState.duration` back down to
  short form via a second, reversed `durMap` before sending it — undoing the one place
  that was already correct.

Only "Forever" worked, because `"forever"` happens to be both the short-form and
canonical spelling.

**Fix (client-side only — the server allowlist is intentionally not loosened).** Changed
every `data-duration=` / `data-keep-dur=` template literal, and both JS default
fallbacks, to send the canonical long-form string directly. Removed the two now-redundant
(and, in the per-season loop's case, actively wrong) short↔long conversion maps in
`library.js`, since `data-keep-dur` is canonical at the source after the template change.

**Tests.** `tests/unit/web/test_quick_keep_duration_literals.py` — scans every
`src/mediaman/web/templates/*.html` file and every `src/mediaman/web/static/js/**/*.js`
file for `data-duration=` / `data-keep-dur=` literals and for the
`dataset.duration || '...'` / `dataset.keepDur || '...'` JS fallback pattern, asserting
each value is a key of `VALID_KEEP_DURATIONS`. Verified failing against the pre-fix code
(stashed the source changes, kept the test) and passing after.

## Defect 2 — download.js fails to parse

**Problem.** `src/mediaman/web/static/js/download.js` did not parse
(`node --check` → `SyntaxError: Invalid or unexpected token`), breaking every page that
loads it.

**Cause.** `buildHeroCard()`'s hint-text block (around the `document.createElement('div')`
call and the "You'll be notified…" text node) used Unicode curly quotes (U+2018 `'` /
U+2019 `'`) as string delimiters instead of ASCII `'` — invalid JS syntax.

**Fix.** Replaced the curly-quote delimiters with ASCII `'` throughout that block. Left
curly characters that are genuine string *content* untouched (the em dash, and the
apostrophe inside "You'll").

**Tests.** `tests/unit/web/test_static_js_syntax.py` — runs `node --check` over every file
under `src/mediaman/web/static/js/`. Fails loudly (not a silent skip) if no `node` binary
is found on `PATH` or at the Homebrew fallback location, unless
`MEDIAMAN_SKIP_NODE_CHECK=1` is set. Verified `node --check` fails on the pre-fix file and
the new test fails accordingly; both pass after the fix. Confirmed via GitHub's
`runner-images` documentation that `ubuntu-latest` ships Node.js pre-installed and on
`PATH` with no setup step required; added an explicit `node --version` step to
`.github/workflows/ci.yml`'s `tests` job as a one-line, low-risk documentation of that
now-load-bearing assumption.

## Defect 3 — silent abandon-search failures

**Problem.** A failed "abandon search" request gave the user no feedback at all.

**Cause.** `src/mediaman/web/static/js/dl-abandon.js`'s failure handler called
`window.mediamanToast(...)`, which is not defined anywhere in the app — the `if
(window.mediamanToast)` guard was always false, so the `else` branch's
`console.error` (invisible to the user) ran every time instead.

**Fix.** Replaced with `window.UIFeedback.error(msg)` — the app's real feedback API
(`src/mediaman/web/static/js/ui-feedback.js`), matching its exact signature and the
pattern already used identically elsewhere (e.g. `library.js`'s keep-failure handling).
`ui-feedback.js` loads in `base.html`, which every page (including `downloads.html`,
where this modal lives) extends, so the direct call needs no existence guard.

**Tests.** `tests/unit/web/test_static_js_toast_api.py` — asserts no file under
`src/mediaman/web/static/js/` references `mediamanToast`. Verified failing against the
pre-fix file, passing after.

## Verification

All three regression tests were confirmed to fail against the pre-fix source (via
`git stash` of the source changes only, keeping the new test files) and pass after
restoring the fixes. Full gate suite run locally in a fresh Python 3.12 venv, installing
`requirements.lock` before `.[dev]` (matching CI's exact install sequence — installing
`.[dev]` alone resolves unpinned dev/transitive packages, e.g. a newer `anyio` that
triggers a `DeprecationWarning`-as-error under this repo's strict warnings filter):
`ruff check .`, `ruff format --check .` (both clean, verified under `ruff==0.15.22` — see
note below), `mypy src/mediaman` (clean), `bandit -r src/ -c bandit.yaml -ll -f txt`
(clean), and the full `pytest -n auto` suite (3012 tests collected, all passing, 87.65%
coverage against an 83% floor).

**Ruff-version note (not part of this change, reported for visibility):** the dev
dependency `ruff~=0.15` in `pyproject.toml` is a two-segment PEP 440 specifier, so it only
pins the leading `0` and floats across the entire 0.x range — a fresh `pip install
-e ".[dev]"` today resolves ruff 0.16.6, not 0.15.x. Ruff 0.16 added Markdown
code-block formatting and the `RUF036` lint rule, both of which flag pre-existing,
unrelated content (`CODE_GUIDELINES.md`'s embedded Python examples; a `None`-union
ordering in `src/mediaman/services/downloads/notifications.py`) that this branch does not
touch. Gates were verified clean under `ruff==0.15.22` to isolate this from genuine
findings; a same-day CI run of this PR may show `lint`/`format-check` red on those
unrelated files, purely from picking up 0.16.6. Out of scope for this change — flagged,
not fixed.

**pip-audit note (not part of this change, reported for visibility):** `pip-audit -r
requirements.lock --require-hashes` reports one known vulnerability —
`cryptography==49.0.0` (PYSEC-2026-3552, fixed in 50.0.0) — unrelated to this branch
(`requirements.lock` is byte-identical to `origin/main`). Fixing it means regenerating
`requirements.lock`, which per project memory is a separate, serialised workflow, and is
out of scope for a three-frontend-defect PR. Flagged, not fixed; the `dependency-audit`
gate is red on `origin/main` independent of this change.
