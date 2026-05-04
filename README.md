# mediaman

[![CI](https://github.com/rossetv/mediaman/actions/workflows/ci.yml/badge.svg)](https://github.com/rossetv/mediaman/actions/workflows/ci.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/rossetv/mediaman)](https://hub.docker.com/r/rossetv/mediaman)

Self-hosted media lifecycle management for a Plex library. mediaman scans the libraries you point it at, schedules stale items for deletion behind a grace period, emails subscribers a newsletter with one-click "keep" links, and exposes a web UI for browsing the library, searching for new content via Sonarr/Radarr, watching the NZBGet queue, and recovering items.

Integrates with **Plex**, **Sonarr**, **Radarr**, **NZBGet**, **Mailgun**, **TMDB**, **OMDb**, and (optionally) **OpenAI** for recommendations.

## How it fits together

```
Browser / email client
        │
        ▼
  FastAPI web UI ── SQLite (encrypted settings, sessions, audit log,
        │            scheduled deletions, used-token store, …)
        │
        ├── Plex API           (PlexAPI)
        ├── Sonarr / Radarr    (REST)
        ├── NZBGet             (XML-RPC)
        ├── TMDB / OMDb        (metadata, posters)
        ├── Mailgun            (newsletter delivery)
        └── OpenAI             (recommendations, optional)
```

Every integration credential is stored encrypted at rest (AES-256-GCM, key derived via HKDF-SHA256 from `MEDIAMAN_SECRET_KEY`). The only secret you manage directly is `MEDIAMAN_SECRET_KEY`; everything else lives in the DB and is configured through the web UI.

## Features

- Scheduled (or on-demand) scans across selected Plex libraries.
- Per-rule cleanup policy: minimum age, watch-inactivity threshold, grace period; dry-run mode.
- HTML newsletter with HMAC-signed, single-use **keep** links — anonymous 7/30/90-day snoozes, admin "forever" protection.
- Library browser with detail panes, scheduled-deletion overrides, manual delete-with-Radarr/Sonarr-cleanup, and a kept-items view.
- Search → download flow against Sonarr/Radarr with NZBGet queue visibility, auto-abandon for stalled jobs, and one-shot signed download confirmation links.
- Recommendations page (OpenAI-backed) with per-user refresh.
- Admin user management (multi-admin), forced password change, bcrypt hashing, rate-limited login.
- Subscriber management for the newsletter.
- Audit history of scans, deletions, keeps, and admin actions.

## Quick start (Docker)

1. **Build the image:**
   ```bash
   docker compose build
   ```
2. **Create `.env`** (copy `.env.example`) and replace `MEDIAMAN_SECRET_KEY` with a real value:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   The startup entropy check rejects the placeholder.
3. **Mount your media.** In `docker-compose.yml`, replace `/path/to/your/media` with the host path. The default mount is **read-only** (`:ro`); only switch a root to `:rw` if you intend mediaman to delete from it (and list it in `MEDIAMAN_DELETE_ROOTS`).
4. **Prepare the data directory** (uid 1000 is hard-coded in the image) and start:
   ```bash
   mkdir -p data && chown 1000:1000 data
   docker compose up -d
   ```
5. **Create the first admin user.** Interactive (prompts for username and password):
   ```bash
   docker compose exec -it mediaman mediaman-create-user
   ```
   Non-interactive (avoids leaking the password through history or the process table):
   ```bash
   printf '%s' "$ADMIN_PW" | docker compose exec -T mediaman \
     mediaman-create-user --username admin --password-stdin
   ```
6. **Open the UI** on `http://<host>:8282` and finish configuration in **Settings**.

## Configuration

Service credentials (Plex, Sonarr, Radarr, NZBGet, Mailgun, TMDB, OMDb, OpenAI) are configured through the web UI and stored encrypted. The only environment variables you need are bootstrap concerns:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MEDIAMAN_SECRET_KEY` | **yes** | — | Master key for encryption and token signing. Must be ≥ 64 hex chars (or ≥ 43 URL-safe base64). |
| `MEDIAMAN_PORT` | no | `8282` | Web server port. |
| `MEDIAMAN_DATA_DIR` | no | `/data` | Directory for the SQLite database. |
| `MEDIAMAN_BIND_HOST` | no | auto | Defaults to `0.0.0.0` inside Docker, `127.0.0.1` on bare metal. Set explicitly to override. |
| `MEDIAMAN_TRUSTED_PROXIES` | no | (empty) | Comma-separated reverse-proxy IPs/CIDRs. Required for `X-Forwarded-For` / `X-Forwarded-Proto` to be honoured. Wildcards (`*`, `0.0.0.0/0`, `::/0`) are rejected with a CRITICAL log line — they would let any peer spoof the source IP and bypass per-IP rate limits. |
| `MEDIAMAN_DELETE_ROOTS` | no\* | (empty) | Colon-separated allow-list of filesystem roots mediaman may delete from (`/media:/media2`). Deletion **fails closed** if unset. Legacy `,` separator still accepted with a deprecation warning. |
| `TZ` | no | `UTC` | Scheduler timezone (any IANA name). |

\* Required if you want deletion to actually do anything.

See `.env.example` for the starter file.

## Running

mediaman is **single-worker**. Multi-worker uvicorn is rejected at startup.

```
mediaman                                # production entry point (uvicorn under the hood)
uvicorn mediaman.main:app --workers 1   # equivalent
```

Setting `MEDIAMAN_WORKERS`, `UVICORN_WORKERS`, or `WORKERS` to anything > 1 raises `RuntimeError`. Several invariants assume one process: the APScheduler instance, the per-IP login/rate-limit buckets, and the Sonarr/Radarr search-trigger throttle. (The download-token replay store moved to SQLite — that part *is* worker-safe — but the rest is not.) For HA, run multiple replicas behind a reverse proxy with sticky-or-shared session storage rather than scaling workers in one process.

### Health probes

- `GET /healthz` — liveness; 200 whenever the event loop is responsive.
- `GET /readyz` — readiness; 200 only when the scheduler started cleanly **and** the crypto canary decrypts. 503 with a JSON body explaining which subsystem is down.

## Security

- Sessions: 256-bit random tokens, HTTP-only Secure SameSite=Strict cookies.
- Passwords: bcrypt cost 12, constant-time compare, dummy hash on missing user (no enumeration).
- Login: per-IP rate limits; lockout window on repeated failure.
- Stored integration credentials: AES-256-GCM with a per-install salt; HKDF-SHA256 from `MEDIAMAN_SECRET_KEY`.
- Newsletter keep-links: HMAC-SHA256-signed, single-use (replay-protected via SQLite `keep_tokens_used`).
- Download confirmation links: HMAC-signed, single-use, persisted in SQLite (`used_download_tokens`).
- Deletion paths: validated against `MEDIAMAN_DELETE_ROOTS` before `shutil.rmtree`; fails closed if unset.
- Security headers applied globally: CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, HSTS.
- Container hardened: non-root uid 1000, all caps dropped, `no-new-privileges`, `mem_limit: 1g`, `cpus: 1.0`.

**Known limitations:**

- CSP currently includes `'unsafe-inline'` for scripts and styles. Nonce-based CSP is planned.
- TMDB poster images are loaded directly from `image.tmdb.org` by the browser, so TMDB sees which titles a subscriber's session viewed. Documented trade-off; proxy-through-mediaman is a deliberate non-feature for bandwidth reasons.
- Single-worker only (see Running).

Vulnerability reports: see [`SECURITY.md`](SECURITY.md). Please use GitHub's private vulnerability reporting; do not open a public issue.

## Operations

### Backups

mediaman uses SQLite in WAL mode, so a live database is up to three files (`mediaman.db`, `mediaman.db-wal`, `mediaman.db-shm`).

**Recommended — online backup (app keeps running):**

```bash
sqlite3 /data/mediaman.db ".backup /backup/mediaman-$(date +%Y%m%d%H%M%S).db"
```

The output is a single self-contained `.db` file; no `-wal`/`-shm` companions needed.

**Alternative — file copy (stop the app first):**

```bash
docker compose stop mediaman
cp /data/mediaman.db /data/mediaman.db-wal /data/mediaman.db-shm /backup/
docker compose start mediaman
```

Copying only `.db` while the app is running and the WAL has not been checkpointed will give you an inconsistent backup.

### Upgrades

```bash
docker compose pull && docker compose up -d
```

Schema migrations run automatically on startup. On cold start mediaman also reconciles two stranded-state cases: pending delete intents that crashed mid-way through the Radarr/Sonarr call, and download notifications that were claimed but never sent. Both are idempotent.

## Development

mediaman targets Python **3.12** (`.python-version`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install   # optional — runs ruff on staged files
```

CI gates (all enforced on PR):

| Job | Command |
|---|---|
| Tests + coverage floor | `pytest -q --cov=mediaman` (floor in `pyproject.toml`) |
| Lint | `ruff check .` |
| Format | `ruff format --check .` |
| Types | `mypy src/mediaman` |
| Security scan | `bandit -r src/ -c bandit.yaml -ll` |
| Dependency audit | `pip-audit -r requirements.lock --require-hashes` |
| Lock-file freshness | regenerated lock must equal committed `requirements.lock` |
| Docker build | multi-stage, digest-pinned base image |

The `Makefile` wraps the dev-loop subset:

```
make test         # pytest -q
make lint         # ruff check
make format       # ruff format (rewrites)
make typecheck    # mypy
make check        # lint + format-check + typecheck + test (pre-push smoke)
make clean        # remove .coverage, __pycache__, .pytest_cache, .ruff_cache, .mypy_cache
```

Run the server locally:

```bash
MEDIAMAN_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
  MEDIAMAN_DATA_DIR=./data \
  MEDIAMAN_BIND_HOST=127.0.0.1 \
  mediaman
```

Regenerate the locked dependency set after editing `pyproject.toml`:

```bash
bash scripts/pin-lock.sh
```

(Runs `pip-compile --generate-hashes --allow-unsafe --strip-extras` inside a `python:3.12-slim` container so the result is reproducible across hosts.)

## Further reading

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branch / commit / PR conventions.
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability privately.
- [`DESIGN.md`](DESIGN.md) — architecture notes, scanner internals, threat model.

## Licence

MIT. See [`LICENSE`](LICENSE).
