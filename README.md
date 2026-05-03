# mediaman

[![CI](https://github.com/rossetv/mediaman/actions/workflows/ci.yml/badge.svg)](https://github.com/rossetv/mediaman/actions/workflows/ci.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/rossetv/mediaman)](https://hub.docker.com/r/rossetv/mediaman)

Self-hosted media lifecycle management for Plex. Scans selected libraries for stale media, schedules deletion with a grace period, emails subscribers a weekly newsletter with "keep" links, and provides an admin web UI for browsing, protecting, and recovering items. Integrates with Sonarr, Radarr, NZBGet, Mailgun, TMDB, and OMDb.

## Architecture

```
Browser / email client
        │
        ▼
  FastAPI web UI  ──── SQLite (encrypted settings, sessions, audit log)
        │
        ├── Plex API (PlexAPI)
        ├── Sonarr / Radarr REST API
        ├── NZBGet XML-RPC
        ├── TMDB / OMDb APIs
        ├── Mailgun API (newsletter)
        └── OpenAI API (recommendations, optional)
```

All integration credentials are stored **encrypted at rest** (AES-256-GCM, key derived via HKDF-SHA256 from `MEDIAMAN_SECRET_KEY`). The only secret you manage directly is `MEDIAMAN_SECRET_KEY`.

## Features

- Weekly (or on-demand) scans of Plex libraries
- Configurable cleanup rules: minimum age, watch-inactivity threshold, grace period
- Dry-run mode
- HTML email newsletters with per-item "keep" links (anonymous 7/30/90-day snoozes, admin "forever" protection)
- Web UI for browsing the library, managing downloads (NZBGet queue), viewing audit history, managing subscribers, and configuring settings
- All integration credentials stored encrypted at rest (AES-256-GCM)
- Sessions: bcrypt-hashed passwords, HTTP-only Secure cookies, rate-limited login

## Quick start (Docker)

1. Build the image:
   ```
   docker compose build
   ```
2. Create an `.env` file (see `.env.example`) and **replace `MEDIAMAN_SECRET_KEY`** with a value from:
   ```
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
3. Set the media path in `docker-compose.yml` — find the line with `/path/to/your/media` and replace it with the actual path on your host. The default mount is **read-only** (`:ro`); only add `:rw` for roots listed in `MEDIAMAN_DELETE_ROOTS`.
4. Create the data directory owned by uid 1000 (the mediaman container user), then start:
   ```
   mkdir -p data && chown 1000:1000 data
   docker compose up -d
   ```
5. Create the first admin user. The command prompts for both username and password interactively (use `-it` so the prompt is wired up):
   ```
   docker compose exec -it mediaman mediaman-create-user
   ```
   For non-interactive provisioning, pipe the password via stdin (avoids leaking it through shell history or the process table):
   ```
   printf '%s' "$ADMIN_PW" | docker compose exec -T mediaman mediaman-create-user --username admin --password-stdin
   ```
6. Open the web UI (default port 8282) and complete setup in Settings.

## Running

**Single-worker only.** Run mediaman with a single Uvicorn worker:

```
mediaman
```

or, if invoking uvicorn directly:

```
uvicorn mediaman.main:app --workers 1
```

> **Important:** do not use `--workers N` with N > 1. The token blacklist used
> by download links is stored in process memory. With multiple workers, a token
> consumed by worker A is invisible to worker B, allowing replay attacks on
> download tokens. Multi-worker support requires a shared backend store and is
> tracked as a known limitation (see `_USED_TOKENS` in `web/routes/download.py`).

## Configuration

All service credentials (Plex, Sonarr, Radarr, NZBGet, Mailgun, TMDB, OMDb, OpenAI) are configured via the web UI and stored encrypted. Bootstrap environment variables are the only things needed before first start:

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `MEDIAMAN_SECRET_KEY` | **Yes** | — | Master key for encrypting stored credentials and signing tokens. Must be strong (>=64 hex chars). |
| `MEDIAMAN_PORT` | No | `8282` | Web server port. |
| `MEDIAMAN_DATA_DIR` | No | `/data` | Directory for the SQLite database. |
| `MEDIAMAN_BIND_HOST` | No | auto | Host address uvicorn binds to. Defaults to `0.0.0.0` inside Docker (the published port is the only inbound route) and `127.0.0.1` on bare metal. Set explicitly to override either default. |
| `MEDIAMAN_TRUSTED_PROXIES` | No | (empty) | Comma-separated list of trusted reverse-proxy IPs or CIDRs; required if you sit behind a reverse proxy and rely on `X-Forwarded-For` / `X-Forwarded-Proto`. Wildcard values (`*`, `0.0.0.0/0`, `::/0`) are rejected with a CRITICAL log line — they would let any peer set `X-Forwarded-For` and bypass per-IP rate limits. |
| `MEDIAMAN_DELETE_ROOTS` | No | (empty) | Colon-separated list of allowed filesystem roots for deletion (e.g. `/media:/media2`). **Required for deletion to work** — mediaman fails closed if unset. |
| `TZ` | No | `UTC` | Timezone used by the scheduler. |

See `.env.example` for a starter file.

## Security

- Admin sessions use 256-bit random tokens, HTTP-only Secure SameSite=Strict cookies.
- Passwords are bcrypt-hashed (cost factor 12) with constant-time compare and a dummy hash on missing users to prevent username enumeration.
- Stored integration credentials are encrypted with AES-256-GCM, key derived via HKDF-SHA256 with a per-install salt.
- "Keep" email links are HMAC-SHA256-signed and single-use.
- Deletion paths are validated against an explicit allow-list before `shutil.rmtree` — deletion fails closed if `MEDIAMAN_DELETE_ROOTS` is unset.
- Rate-limited login attempts per source IP.
- Security headers applied globally (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, HSTS).

**Known limitations:**

- CSP includes `'unsafe-inline'` for scripts and styles. Nonce-based CSP is tracked as a planned improvement.
- TMDB poster images load from `image.tmdb.org` via the browser, which means TMDB can infer which titles subscribers viewed. This is documented as a known trade-off.
- The token blacklist for download links is in-process memory only; do not run multiple workers (see the Running section above).

Report security issues privately via [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) — please do not open a public issue.

## Operations

### Backups

mediaman uses SQLite in WAL mode. A live database consists of up to three files:

```
/data/mediaman.db
/data/mediaman.db-wal     # write-ahead log (may be absent if fully checkpointed)
/data/mediaman.db-shm     # shared-memory index (accompanies -wal)
```

**Recommended approach — online backup with `sqlite3`:**

```bash
sqlite3 /data/mediaman.db ".backup /backup/mediaman-$(date +%Y%m%d%H%M%S).db"
```

The `.backup` command uses the SQLite online-backup API. It works while the application is running, produces a consistent snapshot, and automatically checkpoints the WAL first. The resulting file is a single self-contained database; no `-wal` or `-shm` companions are needed.

**Alternative — file copy after checkpoint:**

If you prefer `cp` or `rsync`, stop the application first (or pause writes), then copy all three files together:

```bash
docker compose stop mediaman
cp /data/mediaman.db /data/mediaman.db-wal /data/mediaman.db-shm /backup/
docker compose start mediaman
```

Copying only the `.db` file while the application is running and the WAL has not been checkpointed will produce an inconsistent backup.

## Development

mediaman targets Python 3.12 (see `.python-version`).

```
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install   # optional but recommended; runs ruff on staged files
```

CI rejects PRs that fail any of these gates. The `Makefile` wraps the
canonical incantations:

```
make test        # pytest -q
make lint        # ruff check
make format      # ruff format (rewrites files)
make typecheck   # mypy
```

Or run the full pre-push smoke test in one go:

```
make check       # lint + format-check + typecheck + test
```

If you'd rather invoke the tools directly:

```
ruff check .
ruff format --check .
mypy src
pytest -q
```

`make clean` clears local `.coverage`, `__pycache__`, `.pytest_cache`,
`.ruff_cache`, and `.mypy_cache`. Run it after `pytest --cov` if you don't
want coverage data lingering in your working tree.

Run the server locally:
```
MEDIAMAN_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
  MEDIAMAN_DATA_DIR=./data \
  MEDIAMAN_BIND_HOST=127.0.0.1 \
  mediaman
```

Contributors: see [`CONTRIBUTING.md`](CONTRIBUTING.md) for branch / commit /
PR conventions, and [`SECURITY.md`](SECURITY.md) for how to report
vulnerabilities privately.

## Licence

MIT. See `LICENSE`.
