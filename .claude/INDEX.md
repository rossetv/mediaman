<!-- Claude-maintained; humans never edit. THE registry: every file under
.claude/ has a row in "KB docs" or "Modules" (excepted: INDEX itself, session
artefacts under specs/, plans/, worktrees/, and memory/ bodies — MEMORY.md is
their registry) — an unregistered file is a defect. kb-updater reconciles this
table against disk and code every run. Verified stamps live ONLY here (date @
short sha of the commit verified against). Injected verbatim every session,
never truncated; registry and module rows are never dropped for size — compress
elsewhere first. Repos outgrowing a single-level index (roughly >40 modules)
split into area sub-indexes, only when actually needed. -->

# mediaman — KB Index

Self-hosted media lifecycle management for Plex: a background scanner that
decides what to keep or delete, an admin web UI, and a subscriber newsletter.

## KB docs

| Doc | Purpose | Verified |
|-----|---------|----------|
| [OVERVIEW](OVERVIEW.md) | system summary — always injected | 2026-07-19 @ c69ed47 |
| [DECISIONS](DECISIONS.md) | append-only decision log | 2026-07-16 @ c6ea90d |
| [MEMORY](MEMORY.md) | project memory index — always injected | 2026-07-19 @ c69ed47 |
| [GATES](GATES.md) | the gates that define "done" — Claude's runbook | 2026-07-19 @ c69ed47 |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | layering, flows, data layer, integrations | 2026-07-19 @ c69ed47 |
| [OPERATIONS](docs/OPERATIONS.md) | scheduler jobs, scan lifecycle, degradation | 2026-07-19 @ c69ed47 |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | image build, compose, CI publish, lock regen | 2026-07-19 @ c69ed47 |
| [CONFIGURATION](docs/CONFIGURATION.md) | env + settings table, encrypted secrets | 2026-07-19 @ c69ed47 |
| [TESTING](docs/TESTING.md) | how testing is architected (not the runbook) | 2026-07-19 @ c69ed47 |
| [SECURITY](docs/SECURITY.md) | auth, crypto, SSRF/path defence, audit log | 2026-07-19 @ c69ed47 |
| [GLOSSARY](docs/GLOSSARY.md) | domain jargon (arr, kept, abandon, snoozed …) | 2026-07-19 @ c69ed47 |

## Modules

| Module | Purpose | Entrypoint | Doc |
|--------|---------|-----------|-----|
| app-entry | Startup surface: env → validated config, app factory, first-run bootstrap | `src/mediaman/main.py` | [→](docs/modules/app-entry.md) |
| web-http | The HTTP surface: every FastAPI route handler plus shared response helpers | `src/mediaman/web/routes/__init__.py` | [→](docs/modules/web-http.md) |
| web-auth | Sole owner of web auth: admin CRUD, bcrypt, sessions, lockout, reauth | `src/mediaman/web/auth/__init__.py` | [→](docs/modules/web-auth.md) |
| web-middleware | The security perimeter: CSRF, rate limit, security headers, body size | `src/mediaman/web/middleware/__init__.py` | [→](docs/modules/web-middleware.md) |
| web-data | Persistence + validation for the web tier: repository queries, pydantic models | `src/mediaman/web/repository/__init__.py` | [→](docs/modules/web-data.md) |
| web-frontend | Server-rendered UI: Jinja2 templates and static assets | `src/mediaman/web/templates/` | [→](docs/modules/web-frontend.md) |
| scanner | Background Plex-library scanner and deletion engine | `src/mediaman/scanner/engine.py` | [→](docs/modules/scanner.md) |
| services-arr | Unified Sonarr/Radarr v3 integration, spec-driven HTTP client | `src/mediaman/services/arr/base.py` | [→](docs/modules/services-arr.md) |
| services-downloads | NZBGet integration: merged queue, notifications, abandon | `src/mediaman/services/downloads/__init__.py` | [→](docs/modules/services-downloads.md) |
| services-media-meta | External metadata (Plex, TMDB, OMDB) and OpenAI recommendations | `src/mediaman/services/media_meta/__init__.py` | [→](docs/modules/services-media-meta.md) |
| services-mail | Outbound email: Mailgun transport and the subscriber newsletter | `src/mediaman/services/mail/mailgun.py` | [→](docs/modules/services-mail.md) |
| services-infra | Domain-agnostic primitives: HTTP client, DNS pinning, URL/path safety, storage, rate limiters | `src/mediaman/services/infra/__init__.py` | [→](docs/modules/services-infra.md) |
| platform | The foundation every layer stands on: core utils, crypto, db | `src/mediaman/core/__init__.py` | [→](docs/modules/platform.md) |

## Goal → start here

| Goal | Start at |
|------|----------|
| Understand the system | [OVERVIEW](OVERVIEW.md) → [ARCHITECTURE](docs/ARCHITECTURE.md) |
| Check my work is done | [GATES](GATES.md) |
| Change what gets deleted or kept | `src/mediaman/scanner/engine.py` → [modules/scanner](docs/modules/scanner.md) |
| Add or change a page / endpoint | `src/mediaman/web/routes/` → [modules/web-http](docs/modules/web-http.md) |
| Touch login, sessions or passwords | [SECURITY](docs/SECURITY.md) → [modules/web-auth](docs/modules/web-auth.md) |
| Add a setting or a secret | [CONFIGURATION](docs/CONFIGURATION.md) → [modules/web-data](docs/modules/web-data.md) |
| Talk to Sonarr/Radarr or NZBGet | [modules/services-arr](docs/modules/services-arr.md), [modules/services-downloads](docs/modules/services-downloads.md) |
| Call an external HTTP service | `src/mediaman/services/infra/http/` → [modules/services-infra](docs/modules/services-infra.md) |
| Change the UI | `DESIGN.md` (the law) → [modules/web-frontend](docs/modules/web-frontend.md) |
| Bump a dependency / fix the lock | [DEPLOYMENT](docs/DEPLOYMENT.md) → `scripts/pin-lock.sh` |
| Decode domain jargon | [GLOSSARY](docs/GLOSSARY.md) |

## Human docs (read-only for Claude)

| Doc | Covers |
|-----|--------|
| `CODE_GUIDELINES.md` | the law — reviewer enforces every section |
| `DESIGN.md` | visual language — reviewer enforces on UI diffs |
| `README.md` | what the project is, how to run it |
| `CONTRIBUTING.md` | contributor workflow |
| `SECURITY.md` | the project's public security policy (not `docs/SECURITY.md`, which is this KB's threat/control map) |

## Central vs peripheral

- **Central** (changes fan out): `src/mediaman/db/schema_definition.py`, `src/mediaman/config.py`, `src/mediaman/services/infra/`, `src/mediaman/core/`, `src/mediaman/db/connection.py`
- **Peripheral** (isolated): `src/mediaman/services/mail/newsletter/`, `src/mediaman/web/static/`, `src/mediaman/services/openai/recommendations/`
