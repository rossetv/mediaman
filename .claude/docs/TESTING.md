<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../INDEX.md)

# Testing

<!-- One concern: how testing is ARCHITECTED — layout, fixtures, warnings/coverage
config, markers, parallelism. How to CHECK the work (the gate commands and their
exit codes) lives in GATES.md, not here. -->

## Facts

### Layout & configuration

| Item | Value | Source |
|------|-------|--------|
| Test root | `tests/` (`tests/unit/`, `tests/integration/`, `tests/helpers/`) | `pyproject.toml` (`[tool.pytest.ini_options]` `testpaths`) |
| Unit tree | Mirrors `src/mediaman/**` package-for-package; 148 test files | tree |
| Integration tree | 5 files exercising route → middleware → repository seams via FastAPI `TestClient` against a real (temp-file) SQLite DB | `tests/integration/` |
| Shared fixtures | One conftest for the whole tree, `tests/conftest.py` — no per-package `conftest.py` exists | verified: `find tests -iname conftest.py` |
| `sys.path` | `pythonpath = ["src"]` puts `src/` on the path for collection | `pyproject.toml` (`[tool.pytest.ini_options]` `pythonpath`) |
| Strictness | `--strict-markers --strict-config` — an unregistered marker or an ini typo fails collection outright | `pyproject.toml` (`addopts`) |
| Default invocation | `-q --tb=short` | `pyproject.toml` (`addopts`) |

### Markers

| Marker | Meaning | Applied |
|--------|---------|---------|
| `unit` | fast, no I/O | auto-attached to every test under `tests/unit/` |
| `integration` | crosses a module boundary | auto-attached to every test under `tests/integration/` |
| Auto-tagging | a test with an explicit marker keeps it; only a missing marker is added | `tests/conftest.py` (`pytest_collection_modifyitems`) |
| Registration | both declared under `markers = [...]`; `--strict-markers` rejects anything else | `pyproject.toml` (`markers`) |
| Selection | `pytest -m unit`, `pytest -m integration` | — |
| New markers | discouraged without a written justification — the set is deliberately small | `CODE_GUIDELINES.md` §11.12 |

### Warnings policy

| Item | Value | Source |
|------|-------|--------|
| Default | `"error"` — any warning not explicitly ignored fails the test/collection | `pyproject.toml` (`[tool.pytest.ini_options]` `filterwarnings`) |
| Ignored | `DeprecationWarning` from `apscheduler`; `DeprecationWarning` from `plexapi` | same |
| `httpx2` requirement | starlette's `TestClient` deprecated `httpx` in favour of `httpx2`; under strict warnings that deprecation is a hard collection error, so `httpx2` must be installed alongside `httpx` — test-only, never a runtime dependency | `pyproject.toml` (`[project.optional-dependencies]` `dev`, comment above the `httpx2` entry) |

### Coverage

| Item | Value | Source |
|------|-------|--------|
| Floor | `fail_under = 83` | `pyproject.toml` (`[tool.coverage.report]` `fail_under`) |
| Floor policy | set two points under the 85% achieved on 2026-05-12; moves up, never down (a PR lowering it needs a written justification) | `pyproject.toml` (comment above `fail_under`); `CODE_GUIDELINES.md` §11.8 |
| Measured scope | `source = ["mediaman"]`, `branch = true`; omits `*/tests/*`, `*/web/templates/*` | `pyproject.toml` (`[tool.coverage.run]`) |
| Current reading | ~87.6% as of the last `make coverage` run — a point-in-time measurement, not stored anywhere in the repo; re-run rather than trust this figure as the code moves | not code-verifiable — no file citation |

### Parallelism (pytest-xdist)

| Item | Value | Source |
|------|-------|--------|
| Invocation | `-n auto`, both in dev (`make test` / `make coverage`) and in CI | `Makefile` (`test`, `coverage`); `.github/workflows/ci.yml` (`Run tests` step) |
| Suite size | ~2700 tests spread across the runner's CPUs; pytest-cov combines per-worker coverage automatically | `.github/workflows/ci.yml` (`Run tests` step comment) |
| Isolation contract | any test reading a live filesystem/clock signal must pin it — it cannot assume it is alone on the machine | `.github/workflows/ci.yml` (`Run tests` step comment) |
| Cross-test leakage guards | four autouse fixtures reset module-level global state around every test, because xdist's in-worker test order is non-deterministic | `tests/conftest.py` — see Fixtures below |

### Fixtures (`tests/conftest.py`)

| Fixture | Kind | Purpose |
|---------|------|---------|
| `pytest_collection_modifyitems` | collection hook | auto-marks tests `unit`/`integration` by directory (see Markers) |
| `_reset_rate_limiters` | autouse | drains every live `RateLimiter`/`ActionRateLimiter` bucket and the IP-resolver LRU cache, before and after each test |
| `_fake_dns_ok` | autouse | stubs `socket.getaddrinfo` to a fixed public IP so the SSRF DNS guard doesn't need real network |
| `_clear_scanner_runner_caches` | autouse | resets the scanner runner's cached `PlexClient`, before and after each test |
| `_reset_db_connection_state` | autouse | drops the thread-local DB connection registered by the previous test |
| `conn` / `db_path` / `tmp_data_dir` | function | opens and initialises a fresh SQLite DB under `tmp_path`; `conn` closes it on teardown |
| `app_factory` | function | builds a minimal FastAPI app with `state.config`/`state.db` wired and the given routers mounted |
| `authed_client` | function | `TestClient` carrying a live admin session cookie; `with_reauth=True` also mints a reauth ticket |
| `templates_stub` | function | Jinja2 stand-in that echoes the render context back as JSON instead of rendering real HTML |
| `fake_http` / `fake_response` | function | monkeypatches the `SafeHTTPClient` transport seam (`_dispatch`) to queue/capture outbound HTTP calls |
| `freezer` | function | freezes `datetime.now()` and the stdlib `time` family at `2026-05-01T12:00:00+00:00` via `freezegun`; advance with `freezer.tick(timedelta(...))` (positional argument, not a keyword) |
| `secret_key` | function | deterministic 64-hex-char key so HMAC/`Config`-derived output is reproducible across runs |

### Test data factories (`tests/helpers/factories.py`)

| Item | Value | Source |
|------|-------|--------|
| Shape | dict `make_*` builders, paired with connection-taking `insert_*` companions that persist the row and return its id | `tests/helpers/factories.py` |
| Coverage | media items, scheduled actions, settings, audit log, kept shows, subscribers, suggestions, recent downloads, download notifications, admin users; plus `MagicMock`-shaped Plex show/season/episode builders | `tests/helpers/factories.py` |
| Convention | `insert_*(conn, **fields)` starts from the matching `make_*()` defaults, then applies overrides — never a hand-built object with every field spelled out | `tests/helpers/factories.py` (e.g. `insert_media_item`) |

## Procedures

1. Run only unit tests for fast local iteration: `pytest -m unit`
2. Run only integration tests: `pytest -m integration`
3. Run one area with clearer tracebacks (skip the parallel workers): `pytest -k scanner -p no:xdist`
4. Advance the frozen clock inside a test: `freezer.tick(timedelta(seconds=601))`, using the `freezer` fixture — never call `datetime.now(UTC)` directly in time-dependent test code.
5. Add a new piece of shared test scaffolding: extend `tests/conftest.py` — there is no scoped `tests/<area>/conftest.py` yet, so the whole tree shares one (`CODE_GUIDELINES.md` §11.4).
6. Add a new module-level cache/limiter/singleton under `src/`: add or extend an autouse reset fixture in `tests/conftest.py` in the same change, or it leaks state across tests under `-n auto` (see Fixtures above).

## Failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Collection error naming a third-party module | `filterwarnings = ["error", ...]` promotes a warning to an error; a bumped or new dependency starts emitting one on import/use | Add a scoped `ignore::<Warning>:<module>` entry to `pyproject.toml`'s `filterwarnings` — never a blanket ignore |
| `TestClient` import/use fails under strict warnings | starlette ≥1.3 deprecated `httpx` in its own `testclient` in favour of `httpx2` | Install `httpx2` alongside `httpx` (`pip install -e ".[dev]"`) |
| Flaky assertion on disk usage or timing, only under `-n auto` | A real syscall (e.g. `shutil.disk_usage`) drifts as sibling xdist workers write their own `tmp_path` dirs concurrently | Mock the syscall instead of sampling live disk state (see `tests/unit/services/infra/test_storage.py`) |
| A test passes alone but fails in the full suite | Module-level cache/limiter/DB-connection state leaking across tests — xdist's in-worker test order is non-deterministic | Add or extend an autouse reset fixture in `tests/conftest.py` |
| New marker rejected at collection | `--strict-markers` rejects a marker not declared in `pyproject.toml`'s `markers` | Register it there first, with the justification `CODE_GUIDELINES.md` §11.12 asks for |
| `pytest -m unit` (or `-m integration`) misses a test or tags it wrong | The file lives outside both `tests/unit/` and `tests/integration/`, so `pytest_collection_modifyitems` never tags it | Move the file under the correct subtree, or mark it explicitly |

## Related

- Law: [`CODE_GUIDELINES.md`](../../CODE_GUIDELINES.md) §11 (Testing) — the authoring rules this file's facts implement: 90/10 unit/integration pyramid, one-test-file-per-source-file, behaviour-named tests, factories over hand-built objects, no `time.sleep`, coverage-floor policy, no live upstreams.
- Runbook: [`GATES.md`](../GATES.md) — `gate: tests` (`make coverage`) is how this suite is CHECKED; this file is how it is architected, a different concern.
- `tests/helpers/factories.py` — the `make_*`/`insert_*` data builders referenced above.
- `Makefile` (`test`, `coverage`) and `.github/workflows/ci.yml` (`Run tests` step) — the two places `-n auto` is invoked.
