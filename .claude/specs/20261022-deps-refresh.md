# Dependency refresh — consolidate open Dependabot PRs

## Why
Fifteen Dependabot PRs were open, most red: pip PRs never regenerate
`requirements.lock`, and every branch (main included) failed pip-audit on
urllib3 2.7.0 (PYSEC-2026-4175/4176/4177, fixed in 2.8.0). Rebasing and
re-locking each PR serially is fifteen rounds of the same work; one branch does it once.

## Scope
- Action bumps (#63, #76, #77, #87, #88): cherry-picked verbatim (SHA pins).
- Runtime: fastapi ~=0.143, uvicorn ~=0.54, pydantic ~=2.14; build: setuptools >=84.
- Dev: ruff ~=0.17.0 (CI install + pre-commit rev in lockstep), mypy ~=2.4,
  pytest-mock ~=3.16, httpx2 ~=2.13, coverage ~=7.16.
- `requirements.lock` regenerated with `scripts/pin-lock.sh` (full upgrade).
- ruff 0.17 formats code blocks in Markdown; `extend-exclude = ["*.md"]` keeps
  ruff Python-only (human docs are off-limits and their compact examples are deliberate).
- #66 (cryptography ~=50.0) is already on main — closed as superseded.

## Done
All `GATES.md` gates green locally on Python 3.12; CI green; superseded PRs closed.
