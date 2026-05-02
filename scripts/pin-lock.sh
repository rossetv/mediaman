#!/usr/bin/env bash
# Regenerate requirements.lock using Python 3.12 inside Docker.
# Run from the repo root: bash scripts/pin-lock.sh
#
# CI runs `pip install --require-hashes -r requirements.lock`, so the lock MUST
# carry hashes (--generate-hashes) and MUST be generated under the same Python
# version used at runtime (3.12, matching the Dockerfile and CI workflow).
#
# Flags rationale:
#   --generate-hashes    : required for `pip install --require-hashes` in CI
#   --allow-unsafe       : pin pip/setuptools too (otherwise CI may pull
#                          unhashed transitives)
#   --strip-extras       : avoid extras-as-markers that confuse some resolvers
#   --resolver=backtracking : default in modern pip-tools, made explicit
#
# Platform is pinned to linux/amd64 to match the production container; cross-
# platform wheel selection in pip-compile is marker-conditional and silently
# wrong if the host architecture differs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

docker run --rm \
  --platform linux/amd64 \
  -v "$REPO_ROOT:/src" \
  -w /src \
  python:3.12-slim \
  sh -c '
    set -eu
    pip install --quiet --upgrade "pip-tools>=7,<8"
    pip-compile \
      --generate-hashes \
      --allow-unsafe \
      --strip-extras \
      --resolver=backtracking \
      --output-file=requirements.lock \
      pyproject.toml
  '

echo "requirements.lock regenerated for Python 3.12"
echo "header: $(head -n 2 "$REPO_ROOT/requirements.lock" | tail -n 1)"
