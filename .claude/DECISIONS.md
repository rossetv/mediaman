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
