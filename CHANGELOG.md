# Changelog

All notable changes to mediaman are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Dates use ISO 8601 (YYYY-MM-DD).

---

## [Unreleased]

### Added

- `SECURITY.md` — private vulnerability reporting policy and scope.
- `CONTRIBUTING.md` — development environment, branch naming, commit conventions, and PR checklist.
- `CODE_OF_CONDUCT.md` — Contributor Covenant v2.1.
- `CHANGELOG.md` — this file.
- `.github/ISSUE_TEMPLATE/bug_report.yml` — structured bug report template.
- `.github/ISSUE_TEMPLATE/feature_request.yml` — structured feature request template.
- `.github/PULL_REQUEST_TEMPLATE.md` — standard PR checklist.
- `.github/dependabot.yml` — weekly automated dependency updates for pip and GitHub Actions.
- `.github/workflows/codeql.yml` — CodeQL static analysis on push, PR, and weekly schedule.
- CI: Python matrix across 3.11, 3.12, and 3.13.
- CI: `ruff check` and `ruff format --check` lint gate (hard failure).
- CI: `mypy` type-check step (soft gate, `continue-on-error: true` until baseline errors are resolved).
- CI: `bandit` security scan (soft gate, `continue-on-error: true`).
- CI: `pip-audit` dependency vulnerability scan (soft gate, `continue-on-error: true`).
- CI: least-privilege `GITHUB_TOKEN` permissions block.
- CI: concurrency group to cancel redundant in-flight runs on the same branch.
- pytest: `--strict-markers` and `--strict-config` options enforced.
- pytest: `filterwarnings = ["error"]` to surface deprecation warnings as failures.
- Coverage floor raised from 30% to 51%.

### Changed

- `pyproject.toml`: coverage `fail_under` raised from 30 to 51.
- `pyproject.toml`: added `--strict-markers`, `--strict-config`, and `filterwarnings = ["error"]` to pytest options.
- `README.md`: added links to Contributing, Security, and Changelog.

---

## [0.1.0] — 2026-04-23

Initial release.

### Added

- Weekly Plex library scan with configurable cleanup rules.
- HTML email newsletter with per-item "keep" links.
- Admin web UI for library browsing, download queue, audit log, subscribers, and settings.
- AES-256-GCM encrypted credential storage.
- Sonarr, Radarr, NZBGet, TMDB, OMDb, Mailgun, and OpenAI integrations.
- Bcrypt password hashing, HTTP-only Secure cookies, rate-limited login.
- Docker image with multi-arch support (linux/amd64, linux/arm64).

[Unreleased]: https://github.com/rossetv/mediaman/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rossetv/mediaman/releases/tag/v0.1.0
