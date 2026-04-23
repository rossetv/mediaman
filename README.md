# mediaman

<!-- BADGE: build status -->
<!-- BADGE: coverage -->
<!-- BADGE: licence: MIT -->
<!-- BADGE: python: >=3.11 -->

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

## Screenshots

_Coming soon._

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
4. Start:
   ```
   docker compose up -d
   ```
5. Create the first admin user:
   ```
   docker compose exec mediaman mediaman-create-user
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
| `MEDIAMAN_BIND_HOST` | No | `127.0.0.1` | Host address uvicorn binds to. Set to `0.0.0.0` inside Docker. |
| `MEDIAMAN_TRUSTED_PROXIES` | No | (empty) | Comma-separated list of trusted reverse-proxy IPs; required if you sit behind a reverse proxy and rely on `X-Forwarded-For` / `X-Forwarded-Proto`. |
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

Report security issues privately — see `SECURITY.md` if present, otherwise use GitHub's private vulnerability reporting.

## Development

```
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Run the server locally:
```
MEDIAMAN_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
  MEDIAMAN_DATA_DIR=./data \
  MEDIAMAN_BIND_HOST=127.0.0.1 \
  mediaman
```

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before
opening a pull request. For bugs and feature requests, use the issue templates.

## Security

Security issues must be reported privately. See [SECURITY.md](SECURITY.md) for
the disclosure policy, scope, and response timeline.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a history of notable changes.

## Licence

MIT. See `LICENSE`.
