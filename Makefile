# mediaman developer Makefile.
#
# Wraps the canonical CI incantations so contributors don't have to memorise
# the exact pytest / ruff / mypy invocations. CI runs the same commands; if a
# `make` target passes locally, the matching CI job should also pass (modulo
# environment differences such as Python patch version).

.PHONY: help test coverage lint format format-check typecheck bandit audit check clean

# Default target — `make` with no arguments prints the menu.
help:
	@echo "mediaman developer targets"
	@echo ""
	@echo "  make test         Run the full pytest suite (mirrors CI: -q --cov -n auto)"
	@echo "  make coverage     Run the full pytest suite and fail if coverage < 83%"
	@echo "  make lint         Run ruff check (read-only)"
	@echo "  make format       Run ruff format (rewrites files)"
	@echo "  make format-check Run ruff format --check (read-only)"
	@echo "  make typecheck    Run mypy"
	@echo "  make bandit       Run bandit security scan"
	@echo "  make audit        Run pip-audit dependency audit"
	@echo "  make check        Run lint + format-check + typecheck + bandit + audit + test"
	@echo "  make clean        Remove local cache and coverage artefacts"

# Mirrors the CI gate: parallel workers, coverage report, no failure threshold.
test:
	pytest -q --cov=mediaman --cov-report=term-missing -n auto

# Same as test but also enforces the 83% coverage threshold used in CI.
coverage:
	pytest -q --cov=mediaman --cov-report=term-missing -n auto --cov-fail-under=83

lint:
	ruff check src tests

format:
	ruff format src tests

format-check:
	ruff format --check src tests

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
