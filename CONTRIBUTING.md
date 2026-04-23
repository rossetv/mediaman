# Contributing to mediaman

Thank you for considering a contribution. This document covers the workflow,
coding standards, and review expectations.

## Code of Conduct

All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Check existing issues.** Your idea or bug may already be tracked.
- **Open an issue first** for non-trivial changes. Agree on scope and approach
  before writing code — it avoids wasted effort and difficult reviews.
- **Security issues** must be reported privately. See [SECURITY.md](SECURITY.md).

## Development environment

```bash
git clone https://github.com/rossetv/mediaman.git
cd mediaman
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest -q
```

Run linting and formatting checks:

```bash
ruff check .
ruff format --check .
```

Type checking (permissive baseline — strictness is raised incrementally):

```bash
mypy src/
```

## Workflow

1. Fork the repository and create a branch:
   ```
   git checkout -b feat/my-feature
   ```
   Branch names follow `<type>/<short-description>` using
   [Conventional Commits](https://www.conventionalcommits.org/) types:
   `feat`, `fix`, `refactor`, `perf`, `style`, `test`, `docs`, `build`, `chore`.

2. Make your changes. Keep commits small and atomic — one logical change per commit.

3. Ensure the full test suite passes and coverage does not regress.

4. Open a pull request against `main` and fill in the PR template.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <description>

<optional body>

<optional footer>
```

Examples:

```
fix(session): record failure during lockout to prevent brute-force bypass

feat(newsletter): validate recipients before Mailgun dispatch

docs: add security policy
```

Use imperative present tense: "add" not "added" or "adds". Lowercase first
letter. No trailing period. Breaking changes: add `!` before the colon and a
`BREAKING CHANGE:` footer.

## Coding standards

- **Python ≥3.11.** New code must be compatible with 3.11, 3.12, and 3.13.
- **British English** in comments, docstrings, and user-facing strings.
- **Ruff** for linting and formatting — run `ruff check .` and `ruff format .`
  before committing. The CI workflow enforces this.
- **Type annotations** on all new public functions and methods. Prefer
  `X | Y` union syntax over `Union[X, Y]`.
- **Docstrings** on all new modules, classes, and public functions. Explain
  *what* and *why*, not just *how*.
- **No `src/mediaman/` changes without tests.** New features and bug fixes must
  include tests that exercise the new or corrected code path.
- **No new dependencies** without prior discussion in an issue. Justify the
  dependency; prefer the standard library.
- **No personal or private data** in commits — no hostnames, tokens, or
  operator-specific configuration.

## Testing

- Unit tests live under `tests/unit/`.
- Integration tests (module boundaries, no external network) live under `tests/integration/`.
- Mark tests with `@pytest.mark.unit` or `@pytest.mark.integration` as appropriate.
- Shared fixtures belong in `tests/conftest.py` or `tests/helpers/`.
- Use `factories.py` in `tests/helpers/` for constructing test data dicts rather
  than building raw SQL dictionaries inline.
- The coverage floor is enforced in CI. Do not lower it.

## Pull request checklist

The PR template contains the full checklist. In summary:

- [ ] Tests pass (`pytest -q`)
- [ ] Ruff passes (`ruff check . && ruff format --check .`)
- [ ] No regressions in coverage
- [ ] New code has docstrings and type annotations
- [ ] PR description explains *why*, not just *what*
- [ ] Security-sensitive changes have been reviewed against the OWASP Top 10

## Changelog

Add an entry to [CHANGELOG.md](CHANGELOG.md) under the `[Unreleased]` section
for any user-visible change (feature, fix, deprecation, removal). Internal
refactors and test-only changes do not require a changelog entry.

## Licence

By submitting a pull request you agree that your contribution is licensed under
the [MIT Licence](LICENSE) that covers this project.
