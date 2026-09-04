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
under `src/mediaman/web/static/js/`, asserting unconditionally that a `node` binary is on
`PATH` (CODE_GUIDELINES §11.15 — no conditional logic in tests; CI's `tests` job asserts
`node --version` explicitly, so the precondition is load-bearing and documented rather
than silently skippable) and that at least one `.js` file was found. Verified `node
--check` fails on the pre-fix file and the new test fails accordingly; both pass after
the fix. Confirmed via GitHub's `runner-images` documentation that `ubuntu-latest` ships
Node.js pre-installed and on `PATH` with no setup step required; added an explicit `node
--version` step to `.github/workflows/ci.yml`'s `tests` job as a one-line, low-risk
documentation of that now-load-bearing assumption.

**Follow-on defect surfaced by making the file parse.** Once `download.js` could
actually run, its adversarial pre-push review caught a second, more severe bug in the
same code path: `download.html` (the per-token download-confirmation page) never loaded
`static/js/downloads/build_dom.js`, the module that defines `MM.downloads.buildDom`.
`buildHeroCard()` calls `MM.downloads.buildDom.buildHero(item)` — with the module
missing, a *successful* download threw a `TypeError` in the `.then` handler, which the
generic `.catch` reported to the user as "Network error — please try again". Before this
branch the file was a `SyntaxError`, so the button was inert and nobody hit the throw;
fixing the parse error alone would have converted "dead button" into "false failure
message on a successful download". Fixed by adding
`<script src="/static/js/downloads/build_dom.js" defer></script>` to `download.html`,
immediately before the `download.js` tag (`defer` preserves document order;
`build_dom.js`'s only dependency, `MM.dom`, is already loaded by `base.html`, which
`download.html` extends). Regression test:
`tests/unit/web/test_download_page_script_deps.py` — asserts `download.html` contains
both script tags with `build_dom.js` ordered before `download.js`. Verified failing
against the pre-fix template (`git show <pre-fix-commit>:src/mediaman/web/templates/
download.html`), passing after.

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

All four regression tests were confirmed to fail against their pre-fix source and pass
after restoring the fix — the first three via `git stash` of the source changes only
(keeping the new test files); the fourth (`test_download_page_script_deps.py`, added
after the pre-push adversarial review's round 1) via `git show <pre-fix-commit>:path`
instead, since this repo's worktrees share one git stash stack and a stash left mid-pop
from an unrelated session is a real hazard here — reading the old blob directly touches
neither the working tree nor the stash. Full gate suite run locally in a fresh Python
3.12 venv, installing `requirements.lock` before `.[dev]` (matching CI's exact install
sequence — installing `.[dev]` alone resolves unpinned dev/transitive packages, e.g. a
newer `anyio` that triggers a `DeprecationWarning`-as-error under this repo's strict
warnings filter): `ruff check .`, `ruff format --check .` (both clean, verified under
`ruff==0.15.22` — see note below), `mypy src/mediaman` (clean),
`bandit -r src/ -c bandit.yaml -ll -f txt` (clean), and the full `pytest -n auto` suite
(3013 tests collected, all passing, coverage above the 83% floor).

**Ruff-version note (not part of this change, reported for visibility):** the dev
dependency `ruff~=0.15.0` in `pyproject.toml` is a **three**-segment PEP 440 specifier, so
it expands to `>=0.15.0, ==0.15.*` and is effective — a fresh `pip install -e ".[dev]"`
today resolves ruff 0.15.x (verified: 0.15.22), not 0.16.x. PyPI's latest ruff is 0.16.6,
and ruff 0.16 added Markdown code-block formatting and the `RUF036` lint rule, both of
which would flag pre-existing, unrelated content (`CODE_GUIDELINES.md`'s embedded Python
examples; a `None`-union ordering in `src/mediaman/services/downloads/notifications.py`)
that this branch does not touch — but that is unreachable today because the pin holds.
Gates were verified clean under `ruff==0.15.22`, which is what the pin actually resolves
to, not an isolation workaround. The warning is for the future, not today: a later
Dependabot bump to `ruff~=0.16.0` will surface those findings on files this branch never
touched — treat them then as pre-existing drift, not as a regression in this PR.

**pip-audit note (superseded — kept for history):** at the time this spec was first
written, `pip-audit -r requirements.lock --require-hashes` reported one known
vulnerability — `cryptography==49.0.0` (PYSEC-2026-3552, fixed in 50.0.0) — unrelated to
this branch (`requirements.lock` was then byte-identical to `origin/main`), and fixing it
was flagged as out of scope for a three-frontend-defect PR. It was subsequently fixed in
a separate branch (`build/bump-cryptography`, `.claude/specs/20260904-cryptography-cve-bump.md`),
which this branch now rebases onto — `requirements.lock` here pins `cryptography==50.0.1`
and `pip-audit -r requirements.lock --require-hashes` is clean.
