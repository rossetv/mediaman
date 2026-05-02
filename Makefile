# mediaman developer Makefile.
#
# Wraps the canonical CI incantations so contributors don't have to memorise
# the exact pytest / ruff / mypy invocations. CI runs the same commands; if a
# `make` target passes locally, the matching CI job should also pass (modulo
# environment differences such as Python patch version).

.PHONY: help test lint format format-check typecheck check clean

# Default target — `make` with no arguments prints the menu.
help:
	@echo "mediaman developer targets"
	@echo ""
	@echo "  make test         Run the full pytest suite (-q)"
	@echo "  make lint         Run ruff check (read-only)"
	@echo "  make format       Run ruff format (rewrites files)"
	@echo "  make format-check Run ruff format --check (read-only)"
	@echo "  make typecheck    Run mypy"
	@echo "  make check        Run lint + format-check + typecheck + test"
	@echo "  make clean        Remove local cache and coverage artefacts"

test:
	pytest -q

lint:
	ruff check .

format:
	ruff format .

format-check:
	ruff format --check .

typecheck:
	mypy src

# `check` is the "before pushing" smoke test. Mirrors the CI gates.
check: lint format-check typecheck test

clean:
	rm -f .coverage .coverage.*
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) -prune -exec rm -rf {} +
