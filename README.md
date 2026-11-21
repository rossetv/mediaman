# mediaman

[![CI](https://github.com/rossetv/mediaman/actions/workflows/ci.yml/badge.svg)](https://github.com/rossetv/mediaman/actions/workflows/ci.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/rossetv/mediaman)](https://hub.docker.com/r/rossetv/mediaman)

mediaman keeps a self-hosted Plex library from filling up. A scheduled scan queues old, unwatched items for deletion after a grace period; a newsletter email lists them with a signed link to keep each one; and an admin web UI browses the library and drives Sonarr, Radarr and NZBGet.

## Run it

```bash
openssl rand -hex 32 > mediaman.key   # keep this: it decrypts your stored settings

docker run -d --name mediaman \
  -p 8282:8282 \
  -e MEDIAMAN_SECRET_KEY="$(cat mediaman.key)" \
  -e MEDIAMAN_DELETE_ROOTS=/media \
  -v mediaman-data:/data \
  -v /path/to/your/media:/media \
  rossetv/mediaman

docker exec -it mediaman mediaman-create-user   # prompts for the first admin's name and password
```

Open `http://<host>:8282`, log in, and enter your services under Settings: Plex, Sonarr, Radarr, NZBGet, Mailgun for the newsletter, TMDB and OMDb for metadata, and OpenAI if you want recommendations. Their keys and passwords are stored encrypted in the database in `/data`, under `MEDIAMAN_SECRET_KEY`.

Two things that may surprise you:

- The session cookie is marked `Secure`, so a browser on plain `http://` to anything but localhost drops it and you cannot stay logged in. Put mediaman behind an HTTPS reverse proxy, or set `MEDIAMAN_FORCE_SECURE_COOKIES=false` on a network you trust.
- mediaman deletes only inside `MEDIAMAN_DELETE_ROOTS`, and refuses every deletion when it is unset. Leave it out and mount your media `:ro` if you want it never to delete.

The image is built from `main` for amd64 and arm64, tagged `latest` and `sha-<commit>`. `/healthz` answers 200 while the process is up; `/readyz` answers 503 unless the scheduler started and the key matches the one the database was created with.

To upgrade, pull the image and recreate the container with the same key and the same `/data` volume; the database migrates itself on start.

```bash
docker pull rossetv/mediaman
docker rm -f mediaman
# then the same docker run as above
```

## Environment

| Variable | Default | What it does |
|---|---|---|
| `MEDIAMAN_SECRET_KEY` | required | 64+ hex characters or 43+ URL-safe base64; a weak key stops startup |
| `MEDIAMAN_DELETE_ROOTS` | unset: no deletions | Colon-separated directories mediaman may delete from, as seen inside the container |
| `MEDIAMAN_PORT` | `8282` | Port to listen on |
| `MEDIAMAN_DATA_DIR` | `/data` | Where the SQLite database lives |
| `MEDIAMAN_BIND_HOST` | `0.0.0.0` in a container, else `127.0.0.1` | Address to bind |
| `MEDIAMAN_TRUSTED_PROXIES` | unset | Comma-separated proxy IPs or CIDRs whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed; `*` is refused |
| `MEDIAMAN_ALLOWED_HOSTS` | any host | Comma-separated `Host` header values to accept |
| `MEDIAMAN_FORCE_SECURE_COOKIES` | unset: always `Secure` | `false` allows login over plain HTTP and also turns HSTS off |
| `MEDIAMAN_HSTS_ENABLED` | `false` | `true` sends `Strict-Transport-Security` on HTTPS requests |
| `MEDIAMAN_MAX_REQUEST_BYTES` | `8388608` | Larger request bodies get a 413 |
| `MEDIAMAN_MEDIA_PATH` | `/media` | Mount point whose disk usage the newsletter reports |

Service credentials and the rest of the settings are in the web UI. Advanced variables, which most installs leave alone: `MEDIAMAN_STRICT_EGRESS`, `MEDIAMAN_CLOUDFLARE_PROXIES`, `MEDIAMAN_FINGERPRINT_MODE`, `MEDIAMAN_REAUTH_WINDOW_SECONDS`, `MEDIAMAN_HSTS_PRELOAD`, `MEDIAMAN_WORKERS`, `MEDIAMAN_EAGER_APP`.

## Development

Python 3.12.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install "bandit[toml]" pip-audit   # make check also runs these

make check   # ruff, ruff format --check, mypy, bandit, pip-audit, pytest with the coverage floor

mkdir -p data
MEDIAMAN_SECRET_KEY=$(openssl rand -hex 32) MEDIAMAN_DATA_DIR=./data \
  MEDIAMAN_FORCE_SECURE_COOKIES=false mediaman   # serves http://127.0.0.1:8282
```

`docker-compose.yml` builds the image from source instead of pulling it. See [CONTRIBUTING.md](CONTRIBUTING.md) to send a change and [SECURITY.md](SECURITY.md) to report a vulnerability. MIT licence, see [LICENSE](LICENSE).
