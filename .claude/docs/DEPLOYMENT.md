<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../INDEX.md)

# Deployment

<!-- One concern: how mediaman ships — image build, compose, CI release
pipeline, and the locked dependency set. Runtime env-var configuration is
README.md's table, not duplicated here. -->

## Facts

| Item | Value | Source |
|------|-------|--------|
| Base image | `python:3.12.9-slim`, digest-pinned, identical in both stages | `Dockerfile` (`FROM python:3.12.9-slim@sha256:...`) |
| Build shape | Two-stage: `builder` (venv + `--require-hashes` install + `compileall`) → runtime (copies `/opt/venv` only) | `Dockerfile` (`AS builder`) |
| No build toolchain installed | Every pin in `requirements.lock` ships a prebuilt cp312 wheel for linux/amd64 and linux/arm64; project is pure Python | `Dockerfile` (comment above the `RUN pip install` step) |
| Runtime user | Fixed uid/gid `1000:1000`, name `mediaman`, no login shell | `Dockerfile` (`useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin mediaman`) |
| Data volume | `/data`, owned by uid 1000 | `Dockerfile` (`VOLUME /data`) |
| Exposed port | `8282` (overridable via `MEDIAMAN_PORT`) | `Dockerfile` (`EXPOSE 8282`) |
| Container entrypoint | `CMD ["mediaman"]` — the `mediaman` console script | `Dockerfile`; `pyproject.toml` (`[project.scripts]` `mediaman = "mediaman.main:cli_main"`) |
| Container healthcheck | `HEALTHCHECK` hits `http://localhost:$MEDIAMAN_PORT/healthz`, `--interval=30s --timeout=5s --start-period=15s --retries=3` | `Dockerfile` (`HEALTHCHECK`) |
| Compose service | `mediaman`, `build: .`, image tag `mediaman:latest` | `docker-compose.yml` |
| Compose hardening | `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `mem_limit: 1g`, `cpus: 1.0`; `read_only: true` deliberately NOT set (tempfile/cryptography/uvicorn writes to `/tmp`) | `docker-compose.yml` (`cap_drop`) |
| Default media mount | Read-only (`:ro`); switched to `:rw` only for roots listed in `MEDIAMAN_DELETE_ROOTS` | `docker-compose.yml` (`/path/to/your/media:/media:ro`) |
| Python version pin | `>=3.12,<3.13`, matches the Dockerfile base and CI | `pyproject.toml` (`requires-python`) |
| Registry | Docker Hub, `<DOCKERHUB_USERNAME secret>/mediaman` | `.github/workflows/ci.yml` (`secrets.DOCKERHUB_USERNAME`) |
| Canonical repo guard | Cloudflare purge only runs when `github.repository == 'rossetv/mediaman'` | `.github/workflows/ci.yml` (`if: github.event_name == 'push' && github.repository == 'rossetv/mediaman'`) |
| Dependency locking | `pip-compile --allow-unsafe --generate-hashes --strip-extras`, hashes enforced everywhere via `--require-hashes` | `requirements.lock` header; `scripts/pin-lock.sh` |
| Lock regeneration | `bash scripts/pin-lock.sh`, runs `pip-compile` inside `python:3.12-slim` (`--platform linux/amd64`) so the result is host-independent | `scripts/pin-lock.sh` |
| Automated dependency PRs | Dependabot: `pip` (weekly, Monday, patch updates grouped) and `github-actions` (weekly, Monday) | `.github/dependabot.yml` (`package-ecosystem: "pip"` / `"github-actions"`) |

## Procedures

1. **Local image build**: `docker compose build` — builds from `Dockerfile` using the digest-pinned base; no registry auth needed.
2. **Bring the stack up**: `docker compose up -d` (after populating `.env` from `.env.example` and preparing `./data` per README.md's Quick Start).
3. **Regenerate the lock after editing `pyproject.toml`**: `bash scripts/pin-lock.sh` — always run this, never hand-edit `requirements.lock`; commit the regenerated file.
4. **Upgrade a running deployment**: `docker compose pull && docker compose up -d` — schema migrations and startup reconciliation run automatically (see `app-entry.md`).
5. **CI release path on push to `main`** (each step gated on all prior jobs succeeding):
   1. `tests`, `lint`, `typecheck`, `security-scan`, `dependency-audit`, `lock-up-to-date` all pass (see table below).
   2. `docker-build` runs **twice in parallel on native runners** — `ubuntu-latest` for `linux/amd64`, `ubuntu-24.04-arm` for `linux/arm64` — no QEMU emulation. Each leg pushes its image **by digest only** (`push-by-digest=true`, no tag) and uploads the digest as a build artifact.
   3. `docker-merge` downloads both digest artifacts and runs `docker buildx imagetools create` to assemble one multi-arch manifest list, tagged `latest` and `sha-<github.sha>`.
   4. `cloudflare-refresh` (canonical repo only) toggles Cloudflare Development Mode on, then purges the zone cache (`purge_everything`) — each Cloudflare API call retries up to 3 times with backoff.

## CI jobs (`.github/workflows/ci.yml`)

| Job | What it runs | Gate |
|-----|---------------|------|
| `tests` | `pytest -q --cov=mediaman --cov-report=term-missing --maxfail=10 -n auto` | Coverage floor lives in `pyproject.toml` (`[tool.coverage.report]`), not duplicated in CI |
| `lint` | `ruff check .` + `ruff format --check .` | Both must pass |
| `typecheck` | `mypy src/mediaman` | Full-tree, hard-failing |
| `security-scan` | `bandit -r src/ -c bandit.yaml -ll` | `-ll` = MEDIUM severity and above; LOW findings are tracked as `nosec` exemptions in `bandit.yaml` |
| `dependency-audit` | `pip-audit -r requirements.lock --require-hashes` | Audits the concrete locked/hashed set actually shipped, not the loose `pyproject.toml` constraints |
| `lock-up-to-date` | Seeds `pip-compile` (no `--upgrade`) with the committed lock, diffs against a fresh compile | Fails only when `pyproject.toml` no longer matches the committed lock (unrelated upstream releases don't fail this — see the job's inline comment) |
| `docker-build` (×2, matrix `amd64`/`arm64`) | `docker/build-push-action`; PRs build with `push: false` (validation only); pushes on `main` build-and-push by digest | Needs `tests`, `lint`, `typecheck`, `security-scan`, `dependency-audit`, `lock-up-to-date` all green |
| `docker-merge` | `docker buildx imagetools create` — assembles the two per-arch digests into one manifest list | Needs `docker-build`; push-only |
| `cloudflare-refresh` | Cloudflare Development Mode toggle + cache purge via `curl` | Needs `docker-merge`; push-only, canonical repo only |

Both PR and push events run `docker-build`; only a push to `main` reaches `docker-merge` / `cloudflare-refresh` (`if: github.event_name == 'push'` on each).

## Failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `lock-up-to-date` CI job fails | `pyproject.toml` was edited (dependency added/removed/version-constrained) but `requirements.lock` wasn't regenerated | Run `bash scripts/pin-lock.sh` and commit the result |
| `pip install --require-hashes -r requirements.lock` fails on a fresh dependency | A newly-added package (or one bumped by hand, not by `pin-lock.sh`) lacks a wheel for one target arch, or the lock is stale | Regenerate via `scripts/pin-lock.sh`; if a wheel is genuinely missing for an arch, reinstate `build-essential` (+ `rust` for pyo3/maturin packages) in the Dockerfile builder stage per its inline comment |
| `docker-build` arm64 leg fails but amd64 passes (or vice versa) | Architecture-specific wheel missing in `requirements.lock`, surfaced only under native (non-emulated) builds | Check the failing package's PyPI wheel matrix; regenerate the lock once a compatible release exists |
| Container starts but `/readyz` returns 503 | Scheduler failed to start or the AES crypto canary failed — non-fatal by design, web UI stays reachable | Inspect logs for the bootstrap failure reason (see `app-entry.md` for the fail-closed readiness invariant) |
| `docker compose up -d` container exits immediately with a data-dir error | `./data` not owned by uid 1000 (hard-coded in the image) | `mkdir -p data && chown 1000:1000 data` before starting, per README.md's Quick Start |
| Cloudflare purge step fails after 3 retries | Cloudflare API outage or invalid/expired `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ZONE_ID` secret | Re-run the workflow once the API recovers; rotate the token if expired — the manifest push has already succeeded independently, so the image is still shippable |
| `cloudflare-refresh` silently skipped on a fork's push to its own `main` | Guarded by `if: github.repository == 'rossetv/mediaman'` | Expected — forks have no Cloudflare zone to purge |

## Related

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [modules/app-entry.md](modules/app-entry.md) — bind-host resolution, single-worker enforcement, `/healthz` vs `/readyz` semantics
- [modules/platform.md](modules/platform.md) — single-worker in-process state that `MEDIAMAN_WORKERS`-style scaling would break
- `README.md` (`## Quick start (Docker)`, `## Configuration`, `## Operations`) — operator-facing setup, env vars, backup/upgrade steps
