# Contributing to mediaman

Thanks for your interest. mediaman is a small self-hosted project; contributions are welcome but please open an issue first for non-trivial changes so we can agree on the approach before you spend time coding.

## Reporting bugs and security issues

- Functional bugs: open a GitHub issue with steps to reproduce, the mediaman version (commit SHA or tag), and any relevant logs (please redact secrets).
- Security issues: see [`SECURITY.md`](SECURITY.md). Do **not** open a public issue.

## Development setup

mediaman targets Python 3.12 (see `.python-version`).

```bash
git clone https://github.com/rossetv/mediaman.git
cd mediaman
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install   # optional but strongly recommended — see below
```

The full quick-start (Docker, environment variables, first-admin creation) lives in the [Development section of `README.md`](README.md#development).

## Required local checks

CI rejects PRs that fail any of the following. The `Makefile` wraps the canonical incantations:

```bash
make test        # pytest -q
make lint        # ruff check
make format      # ruff format (rewrites files)
make typecheck   # mypy
```

Or run all checks at once:

```bash
make test && make lint && make typecheck
```

If you want format violations to be caught before commit instead of in CI, install the pre-commit hooks:

```bash
pre-commit install
```

The hooks run `ruff format` and `ruff check --fix` on staged files. They do not run `mypy` (it's slow); CI does.

## Coding standards

- Follow the conventions already present in the file you are editing — naming, import ordering, docstring style.
- Public functions, classes, and modules should have docstrings explaining *what* they do, *why* they exist, and any non-obvious behaviour. Prefer clear names over comments.
- New behaviour needs tests. Bug fixes should include a regression test.
- Keep changes small and focused. One logical change per PR; one logical change per commit.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <description>
```

Common types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `chore`. Imperative present tense, lowercase first letter, no trailing period.

## Pull requests

- Branch from `main` using a topical branch name (e.g. `fix/null-crash`, `feat/keep-link-expiry`).
- Run `make test && make lint && make typecheck` before pushing.
- Keep the PR description focused on **what** changed and **why**. The diff already shows the *how*.
- Expect review feedback. The repository sets a high bar for correctness, edge-case handling, and security; PRs that ignore failure paths or skip tests will be sent back.

## Cleaning up

`make clean` removes local `.coverage`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, and `.mypy_cache` artefacts. Run it before commits if you used `pytest --cov` and want to make sure no coverage data leaks into your working tree.
