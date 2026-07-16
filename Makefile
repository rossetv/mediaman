# mediaman developer Makefile.
#
# Wraps the canonical CI incantations so contributors don't have to memorise
# the exact pytest / ruff / mypy invocations. Each target below runs the SAME
# command as its matching job in .github/workflows/ci.yml, so if a `make`
# target passes locally the matching CI job should also pass (modulo
# environment differences such as Python patch version).
#
# That claim is load-bearing — CODE_GUIDELINES.md §15.3 states `make check`
# runs "in the same configuration CI uses". Keep these targets byte-identical
# to CI's commands; a target that quietly narrows its scope (e.g. `ruff check
# src tests` against CI's `ruff check .`) turns this file into a false green.

.PHONY: help test lint format format-check typecheck bandit audit check clean

# Default target — `make` with no arguments prints the menu.
help:
	@echo "mediaman developer targets"
	@echo ""
	@echo "  make test         Run the full pytest suite (mirrors CI exactly)"
	@echo "  make lint         Run ruff check (read-only)"
	@echo "  make format       Run ruff format (rewrites files)"
	@echo "  make format-check Run ruff format --check (read-only)"
	@echo "  make typecheck    Run mypy"
	@echo "  make bandit       Run bandit security scan"
	@echo "  make audit        Run pip-audit dependency audit"
	@echo "  make check        Run lint + format-check + typecheck + bandit + audit + test"
	@echo "  make clean        Remove local cache and coverage artefacts"

# Byte-identical to CI's `tests` job. This DOES enforce a coverage floor:
# pytest-cov reads `fail_under` from pyproject.toml ([tool.coverage.report])
# whenever --cov-fail-under is absent, so the suite goes red below the floor.
# Do NOT add --cov-fail-under here: the CLI flag OVERRIDES the config, which
# would fork the floor into a second source of truth and silently pin it at
# whatever number you typed. CODE_GUIDELINES.md §11.8 assigns the floor to
# pyproject.toml alone, and says it moves up, never down.
test:
	pytest -q --cov=mediaman --cov-report=term-missing --maxfail=10 -n auto

lint:
	ruff check .

format:
	ruff format .

format-check:
	ruff format --check .

typecheck:
	mypy src/mediaman

bandit:
	bandit -r src/ -c bandit.yaml -ll -f txt

audit:
	pip-audit -r requirements.lock --require-hashes

# `check` is the "before pushing" smoke test. Mirrors the CI gates.
check: lint format-check typecheck bandit audit test

clean:
	rm -f .coverage .coverage.*
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) -prune -exec rm -rf {} +
