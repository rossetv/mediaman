<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every command below was verified to RUN and PASS
locally (Python 3.12 venv; lock check in python:3.12-slim) before being written here;
a gate that has never been run is not a gate. NEVER cite a line number. -->
↑ [INDEX](INDEX.md)

# Gates — mediaman

<!-- This file is an operator's RUNBOOK, not documentation. docs/TESTING.md explains how
testing is architected; this says how to CHECK the work and confirm it passes. -->

## DO NOT CHEAT. NEVER BYPASS A GATE.

**A red gate means the work is not done. It does not mean the gate is wrong.**

The cheapest way to turn a red gate green is to edit this file and delete the gate.
That is cheating, and **it will not feel like cheating at the time** — it will feel
like *"this gate was stale anyway."* **That feeling is the failure mode, not a
finding.**

Adding a gate is cheap. **Removing or editing a gate requires a `/panel` — never a
single Claude's decision.** Log the outcome to `DECISIONS.md`.

**Never edit the thing a gate points at in order to make the gate pass.** Do not
delete or `.skip` a failing test. Do not gut a Makefile rule, `pyproject.toml`
config, or the CI workflow. **The gate command is a pointer; hollowing out what it
points at is the same cheat wearing a better disguise** — and it is the one cheat
this file's machinery cannot detect, so it is on you.

Never `--no-verify`. Never skip. If you cannot make a gate pass, **stop and say so** —
that is a legitimate, respectable outcome. Silently weakening the standard is not.

**There is no override.** If work must ship red, a human pushes it themselves,
outside Claude.

## Changing gates

- **Add** — cheap, monocratic, no panel. Record `why`, date, provenance, model.
- **Remove or edit** — `/panel`, then a `DECISIONS.md` entry recording what was
  removed, why, and who approved (human | panel).
- **The human says "remove it"** — no panel; a human decision always overrides.
  The `DECISIONS.md` entry records provenance `human` and QUOTES the instruction.
- **Anti-drift:** if a human removes a gate to UNBLOCK work, propose re-adding it
  once unblocked. A one-off unblock must never silently become a permanent deletion.

## What is worth gating

These gates are the exact mechanical jobs CI runs (`.github/workflows/ci.yml`),
wrapped by the developer `Makefile`. If a `make` target passes locally, the matching
CI job should also pass (modulo Python patch version). The gate set is intentionally
CI-mirrored: no gate here that CI does not also enforce, so "green locally" is a true
predictor of "green on main".

Environment: run inside a Python 3.12 virtualenv with `pip install -e ".[dev]"` plus
`pip-audit` and `bandit` (CI installs those two per-job; they are not in `[dev]`).

## Mechanical gates

<!-- Run by kb-gate.sh Check 3 on every push and PR. Exit 0 = pass. No model involved:
commands and exit codes, which is what makes them trustworthy. `id` is immutable —
the removal tripwire keys on it. -->

### gate: lint
kind: mechanical
why: ruff lint catches real bug-classes (mutable defaults, raise-without-from, unused code) and enforces the import order; CI's "Lint (ruff)" job fails the build on any finding.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
make lint
```

### gate: format-check
kind: mechanical
why: an unformatted diff fails CI's lint job (`ruff format --check`); keeping the tree formatted is a hard CI gate, and this is its read-only check (never the rewriting `make format`).
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
make format-check
```

### gate: typecheck
kind: mechanical
why: the codebase is `mypy --strict`; a type regression is a real defect the "Type check (mypy)" CI job blocks on.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
make typecheck
```

### gate: security-scan
kind: mechanical
why: bandit is the "Security scan" CI gate; it catches insecure patterns (subprocess, weak crypto, injection sinks) that must not regress. Config: `bandit.yaml`.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
make bandit
```

### gate: dependency-audit
kind: mechanical
why: pip-audit against the hashed `requirements.lock` is the "Dependency audit" CI gate; it fails on a known CVE in any pinned runtime dependency.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
make audit
```

### gate: tests
kind: mechanical
why: the full pytest suite plus the 83% coverage floor is the "Tests" CI gate; a failing test or a coverage drop below the floor means the work is not done.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
make coverage
```

### gate: lock-up-to-date
kind: mechanical
why: Dependabot bumps `pyproject.toml` without regenerating `requirements.lock`; the "requirements.lock up to date" CI job fails on the resulting drift. This mirrors that check in python:3.12-slim (linux/amd64, matching runtime) — seed with the committed lock, re-compile WITHOUT --upgrade, diff the package/version/hash content. Regenerate a drifted lock with `bash scripts/pin-lock.sh`.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
docker run --rm --platform linux/amd64 -v "$PWD:/src" -w /src python:3.12-slim sh -c '
  set -eu
  pip install -q pip-tools >/dev/null 2>&1
  cp requirements.lock /tmp/lock.check
  pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras --output-file /tmp/lock.check pyproject.toml >/dev/null 2>&1
  grep -vE "^[[:space:]]*(#|$)" requirements.lock > /tmp/a
  grep -vE "^[[:space:]]*(#|$)" /tmp/lock.check > /tmp/b
  diff -q /tmp/a /tmp/b >/dev/null || { echo "requirements.lock no longer satisfies pyproject.toml — run scripts/pin-lock.sh"; diff -u /tmp/a /tmp/b; exit 1; }
'
```

## Semantic gates

<!-- Verified by the adversarial-reviewer agent on OPUS. Use ONLY for assertions no exit
code can express. None enrolled: the repo's semantic law (CODE_GUIDELINES.md, DESIGN.md)
is enforced by the adversarial-reviewer that kb-gate.sh Check 2b already forces on every
code push, so a duplicate semantic gate here would add nothing checkable. -->

## Gates deliberately absent

<!-- CI-only checks that are NOT local gates, by design (they need Docker + buildx +
registry secrets or exceed the "few minutes local" bar): -->

- **Docker image build** (`Build (amd64)` / `Build (arm64)`) — needs Docker Buildx and
  the full build context per architecture; runs in CI, not here.
- **Docker manifest push** / **Cloudflare cache purge** — deploy-time jobs that need
  registry and Cloudflare credentials; never runnable locally.

## Retired

<!-- One line per retired id at column 0: `- <id> — <YYYY-MM-DD>`. None yet. -->
