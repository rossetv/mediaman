# mediaman

Self-hosted media lifecycle management for Plex. Scans selected libraries for stale media, schedules deletion with a grace period, emails subscribers a weekly newsletter with "keep" links, and provides an admin web UI for browsing, protecting, and recovering items. Integrates with Sonarr, Radarr, NZBGet, Mailgun, TMDB, and OMDb.

## Features

- Weekly (or on-demand) scans of Plex libraries
- Configurable cleanup rules: minimum age, watch-inactivity threshold, grace period
- Dry-run mode
- HTML email newsletters with per-item "keep" links (anonymous 7/30/90-day snoozes, admin "forever" protection)
- Web UI for browsing library, managing downloads (NZBGet queue), viewing audit history, managing subscribers, and configuring settings
- All integration credentials stored encrypted at rest (AES-256-GCM)
- Sessions: bcrypt-hashed passwords, HTTP-only Secure cookies, rate-limited login

## Quick start (Docker)

1. Build the image:
   ```
   docker compose build
   ```
2. Create an `.env` file (see `.env.example`).
3. Start:
   ```
   docker compose up -d
   ```
4. Create the first admin user:
   ```
   docker compose exec mediaman mediaman-create-user
   ```
5. Open the web UI (default port 8282) and complete setup in Settings.

## Configuration

All service credentials (Plex, Sonarr, Radarr, NZBGet, Mailgun, TMDB, OMDb, OpenAI) are configured via the web UI and stored encrypted. Bootstrap environment variables are the only things needed before first start:

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `MEDIAMAN_SECRET_KEY` | Yes | — | Master key for encrypting stored credentials and signing tokens. >=32 chars. |
| `MEDIAMAN_PORT` | No | `8282` | Web server port. |
| `MEDIAMAN_DATA_DIR` | No | `/data` | Directory for the SQLite database. |
| `MEDIAMAN_TRUSTED_PROXIES` | No | (empty) | Comma-separated list of trusted reverse-proxy IPs; required if you sit behind a reverse proxy and rely on `X-Forwarded-For` / `X-Forwarded-Proto`. |
| `MEDIAMAN_DELETE_ROOTS` | No | (empty) | Comma-separated list of allowed filesystem roots for deletion (e.g. `/media`). **Required for deletion to work** — mediaman fails closed if unset. |
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

Report security issues privately — see `SECURITY.md` if present, otherwise use GitHub's private vulnerability reporting.

## Development

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .
pytest -q
```

Run the server locally:
```
MEDIAMAN_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  MEDIAMAN_DATA_DIR=./data \
  mediaman
```

## Licence

MIT. See `LICENSE`.
