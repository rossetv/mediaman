# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report security issues privately through GitHub's [private vulnerability reporting](https://github.com/rossetv/mediaman/security/advisories/new). This routes the report to the maintainers without disclosing it publicly.

If you cannot use GitHub's private advisory flow, contact the maintainer listed in `pyproject.toml` instead. Please include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce, including any relevant configuration.
- The mediaman version (commit SHA or tag) you reproduced against.

We aim to acknowledge reports within five working days and to coordinate a fix and disclosure timeline with you. Please give us a reasonable window to ship a fix before discussing the issue publicly.

## Supported versions

mediaman is a single-tracked project; security fixes land on `main` and the latest tagged release. There are no long-term support branches.

## Scope

Anything in this repository is in scope, including:

- Authentication, session handling, and password storage (`src/mediaman/auth/`).
- Token signing, encryption, and key derivation (`src/mediaman/security/`, anything that consumes `MEDIAMAN_SECRET_KEY`).
- Web request handling and route input validation (`src/mediaman/web/`).
- Filesystem operations performed during deletion or scanning.
- Outbound integrations (Plex, Sonarr, Radarr, NZBGet, Mailgun, TMDB, OMDb, OpenAI).

Out of scope:

- Self-inflicted misconfiguration (e.g. running with a weak `MEDIAMAN_SECRET_KEY`, exposing the admin port to the public internet without a reverse proxy, granting `:rw` mounts to roots not in `MEDIAMAN_DELETE_ROOTS`).
- Vulnerabilities in third-party services we integrate with — please report those upstream.

## Hardening notes

The `Security` section of `README.md` documents the cryptographic primitives, password policy, and known limitations (e.g. `'unsafe-inline'` in CSP, in-process token blacklist requiring single-worker operation). Please skim it before reporting — known limitations are tracked there rather than treated as new findings.
