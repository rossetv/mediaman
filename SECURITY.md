# Security Policy

## Supported versions

Only the latest release on the `main` branch receives security fixes.
Older releases are unsupported. Please upgrade before filing a report.

## Reporting a vulnerability

**Do not file a public GitHub issue for security vulnerabilities.**

Use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability):

1. Go to the [Security tab](../../security) of this repository.
2. Click **"Report a vulnerability"**.
3. Fill in the form with as much detail as possible (see below).

If you cannot use GitHub's private reporting, email the maintainers via the
address listed on the [GitHub profile](https://github.com/rossetv).

## What to include

- A clear description of the vulnerability and its impact.
- Steps to reproduce (proof-of-concept code is welcome; weaponised exploits are not).
- The version or commit hash where you found the issue.
- Any relevant log output or screenshots.

## Response timeline

| Stage | Target |
|---|---|
| Acknowledgement | 72 hours |
| Triage and severity assessment | 7 days |
| Fix or mitigation | 30 days (critical), 90 days (high/medium) |
| Public disclosure | After a fix is released or 90 days, whichever comes first |

We follow responsible disclosure. Once a fix is released we will publish a
GitHub Security Advisory crediting the reporter (unless anonymity is
requested).

## Scope

In scope:

- Authentication and session management
- Cryptographic storage of credentials
- SSRF / server-side request forgery
- Injection (SQL, command, template, header)
- Path traversal and symlink attacks
- Rate-limiting bypass
- Cross-site scripting (XSS) via stored or reflected data
- Insecure direct object references (IDOR)
- Privilege escalation within the admin UI

Out of scope:

- Denial-of-service attacks requiring local network access or a valid admin session
- Issues in dependencies that have already been publicly disclosed (report those upstream and open a regular issue here so we can update the dependency)
- Social engineering
- Physical access scenarios

## Known limitations (documented trade-offs)

The following are known and intentionally documented; reports about them alone
will be closed as `wontfix` unless a bypass or unexplored attack surface is
identified:

- **CSP `'unsafe-inline'`** — nonce-based CSP is planned but not yet implemented.
- **TMDB image tracking** — poster thumbnails load from `image.tmdb.org` in the
  browser; TMDB can infer which titles users view. This is a third-party privacy
  trade-off.
- **Single-worker token blacklist** — download-link tokens are stored in process
  memory. Running multiple Uvicorn workers allows token replay. The README
  documents that `--workers 1` is required.
- **Loopback and RFC 1918 egress** — integration services (Sonarr, Radarr, etc.)
  are often on private networks; outbound connections to those ranges are
  permitted by default. Set `MEDIAMAN_STRICT_EGRESS=1` to disable this.

## Security-relevant configuration

| Variable | Purpose |
|---|---|
| `MEDIAMAN_SECRET_KEY` | Master key for AES-256-GCM credential encryption and HMAC tokens. Must be strong (≥64 hex chars). |
| `MEDIAMAN_DELETE_ROOTS` | Colon-separated allow-list of filesystem roots that may be deleted. Fails closed if unset. |
| `MEDIAMAN_TRUSTED_PROXIES` | Comma-separated trusted reverse-proxy IPs for `X-Forwarded-For` handling. |
| `MEDIAMAN_STRICT_EGRESS` | Set to `1` to block outbound requests to loopback and private IP ranges. |
| `MEDIAMAN_BIND_HOST` | Defaults to `127.0.0.1`; set to `0.0.0.0` only inside a container behind a reverse proxy. |
