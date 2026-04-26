#!/usr/bin/env bash
# Regenerate requirements.lock using Python 3.12 inside Docker.
# Run from the repo root: bash scripts/pin-lock.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

docker run --rm \
  -v "$REPO_ROOT:/src" \
  -w /src \
  python:3.12-slim \
  sh -c "pip install --quiet pip-tools && pip-compile --output-file=requirements.lock pyproject.toml"

echo "requirements.lock regenerated for Python 3.12"
