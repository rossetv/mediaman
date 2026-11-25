# Contributing to mediaman

Thanks for your interest. mediaman is a small self-hosted project; contributions are welcome as pull requests. For a non-trivial change, open a small draft pull request first that describes the approach, so we can agree on it before you spend time coding.

## Reporting bugs and security issues

- Functional bugs: GitHub issues are turned off, so send a pull request with the fix, or a failing test that reproduces the bug. Say which commit SHA or Docker image tag (`sha-<commit>`) you ran, and include any relevant logs (please redact secrets).
- Security issues: report them privately as [`SECURITY.md`](SECURITY.md) describes. Do **not** open a public pull request.

## Development setup

mediaman targets Python 3.12 (see `.python-version`). The tests also need Node.js on your `PATH`: one test runs `node --check` over every file in `src/mediaman/web/static/js/`.

```bash
git clone https://github.com/rossetv/mediaman.git
cd mediaman
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

To run what you are working on:

```bash
mkdir -p data
[ -f mediaman.key ] || openssl rand -hex 32 > mediaman.key   # reuse one key
export MEDIAMAN_SECRET_KEY="$(cat mediaman.key)" MEDIAMAN_DATA_DIR=./data MEDIAMAN_FORCE_SECURE_COOKIES=false
mediaman-create-user   # once
mediaman               # serves http://127.0.0.1:8282
```

The [install guide](docs/install.md) covers running it for real: Docker, environment variables and the services it connects to.

## Required local checks

CI rejects PRs that fail any check below. `make check` runs them all, in CI's configuration:

```bash
make check
```

Each check is also its own target:

```bash
make test          # pytest with coverage (the floor is in pyproject.toml), parallel
make lint          # ruff check
make format-check  # ruff format --check
make typecheck     # mypy src/mediaman
make bandit        # bandit security scan
make audit         # pip-audit on requirements.lock
make format        # ruff format (rewrites files)
```

`make bandit` and `make audit` need `bandit` and `pip-audit`, which are not in the `dev` extra: `pip install "bandit[toml]" pip-audit`.

When you change a dependency in `pyproject.toml`, regenerate the lock with `bash scripts/pin-lock.sh` and commit it; CI fails if the lock no longer matches. The script needs Docker, and it moves every pin to the newest version `pyproject.toml` allows, not only the one you changed.

To catch format and lint problems before each commit, install the pre-commit hooks:

```bash
pip install pre-commit
pre-commit install
```

The hooks run `ruff format` and `ruff check --fix` on staged files. They do not run `mypy`; CI does.

## Coding standards

- Follow the conventions already present in the file you are editing — naming and import ordering.
- Prefer clear names over comments. Write a docstring or comment only for a why the code cannot show: a security reason, a non-obvious invariant, an external quirk. Do not restate the name or signature.
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
- Run `make check` before pushing.
- Keep the PR description focused on **what** changed and **why**. The diff already shows the *how*.
- Expect review feedback. The repository sets a high bar for correctness, edge-case handling, and security; PRs that ignore failure paths or skip tests will be sent back.

## Cleaning up

`make clean` removes local `.coverage` files and the `__pycache__`, `.pytest_cache`, `.ruff_cache` and `.mypy_cache` directories.
