# Security Policy

## Reporting a vulnerability

Please do **not** report security problems in a public pull request.

Report security issues privately through GitHub's [private vulnerability reporting](https://github.com/rossetv/mediaman/security/advisories/new). This routes the report to the maintainers without disclosing it publicly.

If you cannot use GitHub's private advisory flow, email the author listed under `authors` in `pyproject.toml` instead. Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce, including any relevant configuration.
- The commit SHA or Docker image tag (`sha-<commit>`) you reproduced against.

We aim to acknowledge reports within five working days and to coordinate a fix and disclosure timeline with you. Please give us a reasonable window to ship a fix before discussing the issue publicly.

## Supported versions

mediaman has no versioned releases. Security fixes land on `main`, and CI publishes each `main` build as the `rossetv/mediaman:latest` Docker image. Only the latest image is supported; there are no long-term support branches.

## Scope

Anything in this repository is in scope, including:

- Authentication, session handling, and password storage (`src/mediaman/web/auth/`).
- Token signing, encryption, and key derivation (`src/mediaman/crypto/`, anything that consumes `MEDIAMAN_SECRET_KEY`).
- Web request handling and route input validation (`src/mediaman/web/`).
- Filesystem operations performed during deletion or scanning.
- Outbound integrations (Plex, Sonarr, Radarr, NZBGet, Mailgun, TMDB, OMDb, OpenAI).

Out of scope:

- Self-inflicted misconfiguration (e.g. running with a weak `MEDIAMAN_SECRET_KEY`, exposing the admin port to the public internet without a reverse proxy, setting `MEDIAMAN_DELETE_ROOTS` wider than the media you want scheduled deletions to touch).
- Vulnerabilities in third-party services we integrate with — please report those upstream.

## Known limitations

These are by design, not findings:

- mediaman must run as a single process. It refuses to start when `MEDIAMAN_WORKERS`, `UVICORN_WORKERS` or `WORKERS` is above 1.
- The Content Security Policy still allows inline `style` attributes (`style-src-attr 'unsafe-inline'`). Scripts are restricted to same-origin files and per-request nonces.
- `MEDIAMAN_DELETE_ROOTS` bounds only scheduled deletions. The Delete button on the Library page asks Radarr or Sonarr to delete the files through their own APIs, wherever they are.
