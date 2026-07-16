# Decisions

<!-- Claude-maintained, append-only. Entries are never edited or deleted; a
reversal gets a new dated entry that names what it supersedes. Every entry
starts with a heading of the form:

    ## YYYY-MM-DD — <short decision title>

kb-context.sh extracts titles by that pattern — this format and that script
are a coupled contract; change them only together, in the CLAUDE repo.

Entry body shape (Spec/Affects/Supersedes lines only when applicable):

    **Decision:** <what was decided>
    **Why:** <the reason — trade-offs considered>
    **Spec:** .claude/specs/<file>.md
    **Affects:** <KB doc paths this decision touches, comma-separated>
    **Supersedes:** <date/title of the earlier entry, only if a reversal>

Delete nothing above when appending; append new entries at the end of file. -->

## 2026-07-16 — Gate commands quote CI directly rather than the Makefile wrapper

**Decision:** Ratify the three contested gate-command edits in `.claude/GATES.md` as
they stand — `tests` → `make test`, `lint` → `ruff check .`, `format-check` →
`ruff format --check .` — and do NOT revert them to their `make`-wrapped forms.
Additionally correct two prose defects the panel proved: the `lint` `why:` clause
claiming the scope gap was "identical today… a latent trap, not a live bug", and the
"Gates deliberately absent" rationale excluding the Docker image build on "minutes of
apt/dpkg". No gate was removed, none weakened, and no `id` changed. Approved by
`/panel` — three independent Opus panellists plus a judge — per this file's "Changing
gates" rule, which requires a panel and this record for any edit.

**Why:** The winning proposal alone located `CODE_GUIDELINES.md` §15.8 ("CI gates are
not optional"), whose table names each tool verbatim — Tests `pytest -q --cov=mediaman`
(no `--cov-fail-under`), Lint `ruff check .`, Format `ruff format --check .`. That
converts the edits from merely defensible into mandated, and convicts the pre-edit
commands: `make lint` runs `ruff check src tests`, and `make coverage` hardcodes a
floor §11.8 assigns to `pyproject.toml`. Nothing in the repo's law obliges a gate to be
a `make` target; the law names the tool. The judge verified every load-bearing claim
against primary sources rather than the convening Claude's advocacy: `make test` does
enforce the pyproject floor (real repo, no flag → exit 1); `make coverage`'s
`--cov-fail-under=83` genuinely overrides config, reproduced as a false-green (config
floor 88, actual 86.67% → no flag red, flag green) — so the ORIGINAL `make coverage`
was the fail-open gate and the edit closes it; `ruff check .` is a proven strict
superset (421 vs 420 files, the delta being `pyproject.toml`, which RUF200 validates
and which Dependabot edits routinely). The Docker rationale was fabricated: the
`Dockerfile` installs no build toolchain, and the judge measured a cold `--no-cache`
build at 28s native arm64 and 37s emulated amd64 with zero apt/dpkg calls — false on
cause and on magnitude, since ~65s sits inside this file's own "few minutes" bar. The
stale `ci.yml` comment it was copied from is the fabrication's origin. Laundering check
came back clean: every gate was born in the bootstrap commit with
`mandated-by-human: no`, and no such flag has ever been flipped.
The runner-up lost for ruling "keep" on the Docker stanza without measuring it, thereby
affirming that fabricated cause, and for overstating the lint gap as a live false-green
when firing RUF200 required deliberately breaking `pyproject.toml`. The third proposal
lost as the least accurate: its all-"keep" slate ratified both the false lint clause —
which it noticed, then declined to act on — and the unmeasured Docker rationale.

**Affects:** .claude/GATES.md

## 2026-07-16 — Makefile realigned to CI byte-for-byte; gate prose refreshed

**Decision:** Realign every `Makefile` target with the command its matching CI job runs
(`lint` → `ruff check .`, `format-check` → `ruff format --check .`, `format` → `ruff
format .`, `test` → CI's exact `pytest … --maxfail=10 -n auto`), delete the redundant
`coverage` target whose hardcoded `--cov-fail-under=83` was a second enforcement site
for a floor `CODE_GUIDELINES.md` §11.8 assigns to `pyproject.toml` alone, and correct
the stale `.github/workflows/ci.yml` `docker-build` comment that blamed apt/dpkg. This
falsified three claims in `.claude/GATES.md` that described the now-removed divergence,
so the `lint` and `tests` `why:` lines and the "What is worth gating" preamble were
refreshed to stay true. No gate command, `id`, `kind`, assertion or `mandated-by-human`
flag changed; the gate set is unchanged. Provenance: **human** — no panel, per this
file's rule that a human decision overrides. The instruction, verbatim:

> fix these: Defects in your project — reporting, not touching:
> - Makefile header claims "CI runs the same commands" — false 3 ways; also falsifies §15.3's "local pass means CI pass".
> - Makefile test: comment says "no failure threshold" — it lies, and dangerously: someone "fixing" it by adding --cov-fail-under would silently gut the tests gate.
> - ci.yml docker-build comment ("minutes… apt/dpkg") is stale — the fabrication's origin.

**Why:** The `Makefile` header promised "CI runs the same commands", and §15.3 states
`make check` runs "in the same configuration CI uses. Local pass means CI pass" — both
were false: `lint`/`format-check` were scoped to `src tests` against CI's `.`, hiding
`pyproject.toml` from RUF200 validation, which matters because Dependabot edits that
file routinely. §15.3 is law in a human-owned doc Claude may not edit, so the only way
to make it true was to fix the Makefile, not soften its wording. The `test:` comment
claimed "no failure threshold" while the target does enforce the pyproject floor — the
dangerous direction, since someone believing the comment could "fix" it by adding
`--cov-fail-under` and silently gut the tests gate, the one cheat GATES.md names as
undetectable by its own machinery. `coverage` was deleted rather than merely stripped of
its flag: with the flag gone it duplicated `test` exactly, and nothing outside the
Makefile referenced it. The `ci.yml` comment was the origin of the apt/dpkg fabrication
that reached GATES.md via the earlier panel; leaving it live would re-seed the same
false claim. The gate commands still call ruff directly rather than through the now-
correct `make` targets: a gate routed through a wrapper is only as faithful as the
wrapper, and these targets had drifted once already — the prior panel's ratification of
calling CI's command directly therefore stands unchanged.

**Affects:** .claude/GATES.md
