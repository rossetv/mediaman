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

These gates are the exact mechanical jobs CI runs (`.github/workflows/ci.yml`). The
developer `Makefile` wraps those same incantations, and as of 2026-07-16 every target
matches CI byte-for-byte — but where `CODE_GUIDELINES.md` §15.8 names a tool directly
(`ruff check .`, `ruff format --check .`), the gate invokes it directly rather than via
`make`: a gate routed through a wrapper is only as faithful as the wrapper, and these
targets had drifted once already. The gate set is CI-mirrored: no gate here that CI does
not also enforce, so "green locally" predicts "green on main" — with one stated
exception, the arm64 image build (see "Gates deliberately absent").

Environment: run inside a Python 3.12 virtualenv with `pip install -e ".[dev]"` plus
`pip-audit` and `bandit` (CI installs those two per-job; they are not in `[dev]`).

## Mechanical gates

<!-- Run by kb-gate.sh Check 3 on every push and PR. Exit 0 = pass. No model involved:
commands and exit codes, which is what makes them trustworthy. `id` is immutable —
the removal tripwire keys on it. -->

### gate: lint
kind: mechanical
why: ruff lint catches real bug-classes (mutable defaults, raise-without-from, unused code) and enforces the import order; CI's "Lint (ruff)" job fails the build on any finding. Runs `ruff check .` — the command `CODE_GUIDELINES.md` §15.8 names verbatim, and CI's literal invocation. Calls ruff directly rather than through `make lint` deliberately: a gate routed via a wrapper is only ever as faithful as the wrapper, and this one had already drifted to `src tests` once (realigned to CI on 2026-07-16). The scope is load-bearing — `.` covers `pyproject.toml`, which ruff validates under RUF200 (enabled by `select = ["RUF"]`, not ignored) and which Dependabot edits routinely; a malformed dependency specifier goes red here and in CI, but was invisible to the old `src tests` scope.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
ruff check .
```

### gate: format-check
kind: mechanical
why: an unformatted diff fails CI's lint job; keeping the tree formatted is a hard CI gate, and this is its read-only check (never the rewriting `ruff format`). Runs `ruff format --check .` to mirror CI exactly, for the same reason `lint` does not use its `make` target.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
ruff format --check .
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
why: the full pytest suite plus the coverage floor in `pyproject.toml` (`[tool.coverage.report]` `fail_under`, currently 83) is the "Tests" CI gate; a failing test or a drop below the floor means the work is not done. `make test` is byte-identical to CI's command and enforces the floor implicitly: pytest-cov reads pyproject's `fail_under` whenever `--cov-fail-under` is ABSENT. Never add that flag — the CLI value OVERRIDES the config, forking the floor into a second source of truth that silently pins it at whatever was typed. A `make coverage` target did exactly that (hardcoded 83) and was removed on 2026-07-16; it would have passed at 84% on the day the floor moved to 88 while CI went red. CI omits the flag for the same reason ("one source of truth"), and CODE_GUIDELINES §11.8 assigns the floor to `pyproject.toml` alone, moving up and never down.
added: 2026-07-16 — monocratic (claude-opus-4-8)
mandated-by-human: no

```sh
make test
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

<!-- CI-only checks that are NOT local gates, by design. State the real reason: "it
needs Docker" is NOT one of them — `gate: lock-up-to-date` above already runs
`docker run`, so that excuse does not survive this file's own gate set. -->

- **Docker image build** (`Build (amd64)` / `Build (arm64)`) — CI builds each
  architecture on its own native runner. Not enrolled locally; **wall-clock is NOT the
  reason** and neither is capability: the image installs no build toolchain (see
  `Dockerfile`), so there is no apt/dpkg cost — a cold `--no-cache` build measured 28s
  native arm64 and 37s emulated amd64 on an arm64 dev host, inside this file's own
  "few minutes" bar. Enrolling a build gate is therefore an open follow-up (an ADD —
  cheap, monocratic, no panel).
  **Residual risk this leaves uncovered — know it before trusting a green local run:**
  per `.github/workflows/ci.yml`'s own comment, the PR image build is the safety net
  that catches a **missing aarch64 wheel hash in `requirements.lock`**. No gate above
  covers that class: `lock-up-to-date` only proves the lock satisfies `pyproject.toml`
  on linux/amd64. A green local run does not predict a green arm64 build.
- **Docker manifest push** / **Cloudflare cache purge** — deploy-time jobs needing
  registry and Cloudflare credentials; genuinely not runnable locally.

## Retired

<!-- One line per retired id at column 0: `- <id> — <YYYY-MM-DD>`. None yet. -->
