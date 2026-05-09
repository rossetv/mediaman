# Engineering Guidelines

> This document is for everyone who writes, reviews, or operates code in the mediaman
> repository. It is **canonical**: the rules below are the standard of care for the
> codebase, and a violation is a code-review blocker — not a judgement call. Propose
> changes in a pull request whose description names the rule, the rationale, and the
> alternative considered; the same standards that govern code govern this file. The
> spirit of the document is short: prefer boring code, narrow public surfaces, and
> errors designed at the same time as the success path. When two of those tug against
> each other, the values in [§1](#1-philosophy) decide.

> **Aspirational, not retrospective.** These guidelines describe the target state.
> The current codebase has known carve-outs — most notably a handful of files that
> still exceed the 500-line ceiling ([§3.1](#31-file-size)) and a tail of functions
> that exceed the 60-line ceiling ([§3.2](#32-function-size)). Those are tracked as
> ongoing work; new code must conform from day one, and changes that extend an
> existing carve-out are blocked. Apply the rules to every PR; reduce the carve-out
> set over time.

## TL;DR — the ten rules most often violated

1. Use the shared HTTP client; no new `requests.get` outside
   `services/infra/http/` ([§8.1](#81-outbound-http-only-via-the-shared-client)).
2. Repository functions return dataclasses, not `sqlite3.Row`
   ([§9.5](#95-repository-returns-dataclasses-never-raw-rows)).
3. Parameter-substitute every SQL value; no f-string SQL, ever
   ([§9.6](#96-parameter-substitute-always)).
4. `from __future__ import annotations` and full type signatures on every public
   function ([§5.1](#51-public-functions-are-fully-typed),
   [§5.7](#57-from-__future__-import-annotations)).
5. `raise NewError(...) from original` is mandatory when re-raising
   ([§6.3](#63-raise-x-from-original-is-mandatory)).
6. `except Exception:` only at the four documented outer-boundary sites
   ([§6.4](#64-except-exception-is-reserved-for-outermost-loops)).
7. Never log a token, password, hash, or key ([§7.4](#74-never-log-secrets)).
8. Module-level mutable state needs a `threading.Lock` *and* a `# rationale:` comment
   ([§8.5](#85-module-level-mutable-state-is-forbidden-by-default)).
9. Test names describe behaviour, not the function under test
   ([§4.8](#48-test-names-describe-behaviour),
   [§11.3](#113-tests-are-named-for-behaviour)).
10. Files over 500 lines and functions over 60 lines need a written `# rationale:` or
    they get split ([§3.1](#31-hard-limits)).

## 1. Philosophy

These are the values that survive every refactor. Style guides change; values do not. When two
rules in this document appear to conflict, the value below that the rules serve wins. Every
reviewer is expected to push back on changes that violate these — and to accept the same push
back gracefully when their own changes do.

### 1.1 Read like plain English

Code is read ten times for every time it is written. A function whose one-line summary needs
the word "and" is doing two things and must be split. A condition that needs a comment to be
understood must be lifted into a named predicate. A `for` loop that nests three deep almost
always wants to become a generator and a transform. The reader's working memory is the
scarcest resource in the system; spend everything else first.

```python
# Bad — the summary needs "and": fetch episodes AND mark them seen.
def fetch_and_mark_seen(show_id: str) -> list[Episode]: ...

# Good — two intent-revealing names, each does one thing.
def fetch_episodes(show_id: str) -> list[Episode]: ...
def mark_episodes_seen(episodes: Iterable[Episode]) -> None: ...
```

A violation: a 70-line `route handler` that logs in the user, validates the form, calls
Sonarr, persists a row, and renders HTML. Split into four named steps; the handler then reads
like a table of contents.

### 1.2 Boring beats clever

Reach for the standard library before a third-party helper, and a third-party helper before a
custom abstraction. `dataclasses`, `pathlib`, `contextlib`, `secrets`, `hmac`, `enum`,
`functools.cache` — these are not consolation prizes. A clever metaclass saves three lines and
costs every future reader twenty minutes. Cleverness is a tax paid by people who did not write
the code.

A violation: a custom descriptor protocol to memoise a method when `functools.cached_property`
exists. A violation: a hand-rolled retry loop with backoff when the existing
`services/infra/http/client.py` already does it.

### 1.3 Every line is a liability

Code is not an asset; it is debt against future change. The default move when reviewing a
diff is *delete*, then *abstract*, then *write*. A new helper must replace at least two
existing call sites or be on a clear path to doing so within the same PR. Speculative
generality — parameters that no caller supplies, hooks that no caller registers — is a debt
with no upside.

A violation: a `format_size(value, *, unit="auto", precision=2, locale=None)` helper used in
exactly one place that always passes the defaults.

### 1.4 Errors are designed, not caught

An exception is a designed signal between the place that knows the failure shape and the
place that knows how to react. Exception types live close to the code that raises them. A
caller that swallows or converts an exception is making a deliberate, documented choice. A
bare `except Exception:` is almost always a bug — see [§6](#6-errors).

A violation: `try: ... except Exception: pass` around a Sonarr call to "be defensive". The
correct move is to let `SonarrError` propagate and decide at the route boundary whether the
user sees a 502 or a queued retry.

### 1.5 Tests are the README for behaviour

A new contributor must be able to read the test file for a module and understand what the
module promises. Test names describe behaviour, not implementation. A test that breaks when
an internal helper is renamed is a bad test; a test that breaks when a documented behaviour
changes is a great test. See [§11](#11-testing).

A violation: `test_format_added_display_calls_strftime`. The behaviour is "render a UTC ISO
timestamp as a relative phrase"; the implementation is `strftime`. Test the behaviour.

### 1.6 Names carry domain language

Plex calls them *items*; Sonarr calls them *series*; mediaman has *media items*, *scheduled
actions*, *keep tokens*, *snoozes*, *kept shows*, *delete intents*, *download notifications*.
These are domain words. A function called `do_thing(item)` discards the precise word the
user, the database, and the test all share. Discovering the right word is part of the work.

A violation: `task_id` for a scheduled action; `task` is generic, `scheduled_action_id` is the
domain word and matches the table.

### 1.7 Module boundaries are contracts, not suggestions

A package's `__init__.py` is its public surface. Reaching past it into internal modules
couples you to implementation. The dependency arrows in [§2](#2-module-taxonomy) are not
aspirational; an import that violates them is a code-review blocker. A web route that
imports `mediaman.db.connection` directly, bypassing the repository layer, fails review even
if it works.

### 1.8 Comments answer "why", never "what"

If the *what* needs explaining, rename the function or extract a helper. The *why* — the
non-obvious constraint, the workaround, the domain rule, the security finding being closed —
that belongs in a comment. A comment that paraphrases the next line of code is noise; a
comment that names the invariant the code protects is gold.

```python
# Bad — restates the obvious.
# Increment the counter
counter += 1

# Good — names the invariant.
# Increment after the fsync; a crash before this line must replay,
# never under-count, the upload.
counter += 1
```

### 1.9 Premature abstraction is worse than duplication

Two call sites that *look* alike are not yet a pattern. Three is the threshold for extracting
a helper, and even then only if the third site already exists. A premature base class or
generic protocol locks in the wrong axis of variation; deleting it later is harder than
deleting duplication. See the rule of three; obey it.

A violation: a `BaseArrClient` introduced for "future Lidarr support" with no Lidarr in
scope. mediaman has Sonarr and Radarr; a parameterised `ArrClient` driven by a spec is the
right shape today, derived from two real instances — not three speculative ones.

### 1.10 Public API is small by default

Every public name is a promise. A function not in `__all__`, a name with a leading
underscore, a private module — these are reversible decisions. A name in the public surface
of a module is, in practice, irreversible. Default to private; promote to public only when a
caller outside the package needs it and the contract is documented.

A violation: a `_private_helper` that other modules import as `from foo import
_private_helper`. Either the name is public (drop the underscore, document it, add it to
`__all__`) or the caller is wrong (move the function or restructure).

### 1.11 Fail closed, fail loud

Security-relevant defaults must refuse the operation when the configuration is missing.
`MEDIAMAN_DELETE_ROOTS` unset means deletion does nothing; an unset HMAC key means the
process refuses to start; a missing CSRF token means the request is rejected. Loud failure
beats silent corruption; a refused operation can be retried, a silent miss cannot be undone.

### 1.12 One process, one truth

mediaman is single-worker by design. The scheduler, the rate-limit buckets, and the
search-trigger throttles each assume a single in-process owner. New shared state must either
go through SQLite (so it survives a restart and would still work under multi-process) or
declare in a comment that it is single-worker only. Drift between "we assume single
worker" and "we shipped a multi-worker feature" is a class of latent bug we refuse to
accept.

## 2. Module Taxonomy

The codebase has six layers. Imports flow downward only; an upward import is a review-blocker
defect. The diagram is the contract.

```
            ┌──────────────────────────────────────────────┐
            │             web/        (FastAPI)            │
            │   routes / middleware / auth / templates     │
            └──────────────┬───────────────┬───────────────┘
                           │               │
                           ▼               ▼
            ┌──────────────────────────────────────────────┐
            │             services/                        │
            │   arr / downloads / mail / media_meta /      │
            │   openai / rate_limit / infra                │
            └──────────────┬───────────────┬───────────────┘
                           │               │
                           ▼               ▼
            ┌──────────────────────────────────────────────┐
            │             scanner/                         │
            │   engine / phases / repository / fetch       │
            └──────────────────────┬───────────────────────┘
                                   │
                                   ▼
            ┌──────────────────────────────────────────────┐
            │      db/   (schema, connection, migrations)  │
            └──────────────────────┬───────────────────────┘
                                   │
                                   ▼
            ┌──────────────────────────────────────────────┐
            │   crypto/   bootstrap/   core/   (leaves)    │
            └──────────────────────────────────────────────┘
```

### 2.1 `core/`

**Purpose.** Pure, dependency-free utilities: time parsing, URL safety, formatting, small
data primitives.

**Allowed deps.** Standard library only. No third-party imports. No internal imports — `core`
is a leaf.

**Forbidden patterns.** No I/O. No logging side-effects at import time. No global mutable
state. No `requests`, no `sqlite3`, no `apscheduler`.

**Naming.** Flat module per concern: `core/time.py`, `core/url_safety.py`, `core/format.py`.
A function in `core` must be testable in five lines without any fixture.

### 2.2 `crypto/`

**Purpose.** Symmetric encryption, HKDF, HMAC token signing, session-token generation.

**Allowed deps.** `cryptography`, stdlib `secrets`/`hmac`/`hashlib`, and `core/`.

**Forbidden patterns.** No DB access — keys live in env or are passed in. No logging of key
material, plaintexts, ciphertexts, or HMACs (except prefix-truncated for forensics).

**Naming.** `generate_*`, `sign_*`, `verify_*`, `encrypt_*`, `decrypt_*`. A function that
returns sensitive bytes must be named so it is impossible to confuse with a logging helper.

### 2.3 `bootstrap/`

**Purpose.** Process startup: env validation, secret-key sanity, data-dir creation,
single-worker assertion, scheduler launch, readiness signalling.

**Allowed deps.** Anything below `web/`. Imported by `mediaman.main` and nothing else.

**Forbidden patterns.** Bootstrap functions are called once. They must not be called from
request handlers. Idempotency is a goal, not an excuse to invoke them at runtime.

### 2.4 `db/`

**Purpose.** SQLite schema, migration runner, connection lifecycle, WAL configuration. Owns
the only `sqlite3.connect` call in the production codebase.

**Allowed deps.** `sqlite3`, `core/`, `crypto/` (for the canary). `db/migrations/` may import
nothing outside `db/` — migrations must be hermetic.

**Forbidden patterns.** No business logic. No queries against domain tables — those live in
the `repository/` of the owning package. No reading of the connection from a global.

**Naming.** `db/connection.py`, `db/schema_definition.py`, `db/migrations/00NN_*.py`. Each
migration filename starts with a zero-padded sequence number; gaps are forbidden.

### 2.5 `scanner/`

**Purpose.** The Plex-driven media-lifecycle pipeline: fetch, evaluate, schedule, delete,
audit, recover. Owns its own `repository/` for `media_items`, `scheduled_actions`,
`kept_shows`, `snoozes`.

**Allowed deps.** `db/`, `core/`, `crypto/`, `services/*` (Plex, Arr, Mailgun). May import
the `repository/` of any other package only via that package's public surface.

**Forbidden patterns.** No FastAPI imports. No `Request` or `Response` objects. The scanner
must be runnable from a script; if it imports `web/`, it is wrong.

**Naming.** `engine.py` orchestrates. `phases/<verb>.py` does one phase
(`phases/fetch.py`, `phases/evaluate.py`, `phases/upsert.py`, `phases/delete.py`).
`repository/<table_group>.py` does SQL. Phase functions take a connection plus immutable
inputs and return immutable outputs.

### 2.6 `services/`

**Purpose.** All outbound integrations and the small computations that wrap them.

**Allowed deps.** `db/`, `core/`, `crypto/`. Each subpackage may import its siblings only
through the documented public surface of that sibling.

**Forbidden patterns.** No `web/` imports — services know nothing about HTTP requests. No
`scanner/` imports — services are below the scanner.

**Subpackages:**

- `services/infra/` — shared plumbing: `http/client.py` (the only `requests.Session` allowed
  in the codebase), `settings_reader.py`, `storage.py`, `format.py`. Other services import
  `infra`; `infra` imports nothing from a sibling.
- `services/arr/` — Sonarr + Radarr. Driven by a single `spec` so client divergence stays
  declarative. `base.ArrClient` is the only HTTP-facing class; `sonarr.py` and `radarr.py`
  are thin shims for backwards-compatible imports.
- `services/downloads/` — NZBGet client, queue model, format detection. Owns its own DB
  tables (`download_format/`, `download_queue/`).
- `services/mail/` — Mailgun client and the newsletter renderer. `newsletter/` builds the
  HTML; `mail/<provider>.py` sends.
- `services/media_meta/` — TMDB and OMDb metadata, poster URL building, rating normalisation.
- `services/openai/` — recommendations: prompt building, response parsing, persistence under
  `recommendations/`.
- `services/rate_limit/` — per-IP rate limit buckets and the IP-extraction helper.

### 2.7 `web/`

**Purpose.** FastAPI app, route handlers, middleware, auth, Jinja templates.

**Allowed deps.** Everything below it.

**Forbidden patterns.**

1. Route handlers must not call `sqlite3` directly — they go through the relevant
   `repository/` or service.
2. Route handlers orchestrate, services compute. A handler that contains a `for` loop with
   business logic is a service waiting to be extracted.
3. Templates must not import Python helpers other than the macros in
   `templates/_components.html`. Render-time logic belongs in the route or a small
   view-model in `web/models/`.
4. `web/auth/` is the only place that touches the `sessions` and `users` tables.
5. `web/middleware/` writes responses; it does not call services.

**Subpackages:**

- `web/routes/` — one router per surface area, named for the noun
  (`routes/keep.py`, `routes/library_api/`, `routes/dashboard/`). Each file owns the URL
  prefix it registers and defines its own `router = APIRouter()`.
- `web/auth/` — sessions, password hashing, token hashing, fingerprinting, login CLI. The
  only module allowed to import `bcrypt`.
- `web/middleware/` — security headers, CSRF, audit-context, request-id assignment.
- `web/templates/` — Jinja templates organised by feature (`subscribers/`, `settings/`,
  `email/`).
- `web/models/` — view-models and request/response shapes used by routes; **not** ORM
  models.

### 2.8 Cross-package rules

1. `core/` imports nothing internal. Period.
2. `crypto/` imports only `core/`.
3. `db/` imports only `core/` and `crypto/`.
4. `services/*` imports `db/`, `core/`, `crypto/`, and other `services/*` only via public
   surfaces.
5. `scanner/` imports `db/`, `core/`, `crypto/`, `services/*`. Never `web/`.
6. `web/*` may import anything below it. `web/routes/*` must not import another route module.
7. A new top-level package needs a documented purpose, allowed-deps list, and forbidden
   patterns added to this section in the same PR.

## 3. File Organisation

### 3.1 Hard limits

- **Files: target 300 lines, ceiling 500.** Above 500 lines a file is no longer scannable
  in one screen-pair; the table of contents lives in your head and drifts. The fix is
  *always* to extract a sibling module or promote the file to a package, never to keep
  growing.
- **Functions: target 30 lines, ceiling 60.** A 60-line function does not fit on a laptop
  screen with the call site visible; the reviewer cannot hold both the surrounding context
  and the body in working memory.
- **Imports: max 30 per file.** More usually means the module is doing too much; ask
  whether two modules are hiding inside the file.

Exceptions to either ceiling require a one-line `# rationale:` comment at the top of the
file or function explaining why no decomposition is possible. "It would be awkward" is not
a rationale; "this is a single SQL statement that resists splitting and is exhaustively
tested at the public boundary" is.

### 3.2 One concept per file

A file is named for the *one* concept it owns. `session_store.py` persists sessions;
`session_fingerprint.py` derives fingerprints; `session.py` orchestrates the two for the
middleware. Each is testable, replaceable, and reviewable on its own. A file called
`utils.py` or `helpers.py` is a code smell — its absence of a topic is the topic.

### 3.3 When you approach the ceiling, prefer a package

Sibling-dump (`foo.py` → `foo.py` + `foo_helpers.py` + `foo_more.py`) loses the navigability
the file boundary buys you. The right move is:

```
foo.py          →    foo/
                       __init__.py     (re-exports the public surface)
                       _io.py          (private helpers)
                       _validate.py    (private helpers)
                       core.py         (the public engine)
```

The `__init__.py` becomes a thin re-export barrel; callers continue to import
`from mediaman.scanner import foo`. See `scanner/repository/` and
`services/arr/fetcher/` for working examples.

### 3.4 Public vs private

A name with no leading underscore is a promise to outside callers. A name with a leading
underscore is private to the module. A function or class that is only used inside its own
module gets the underscore. Tests for a private helper should normally exercise it through
the public function it supports — see [§11](#11-testing).

### 3.5 `__all__`

Set `__all__` only on re-export barrels and on modules that document a non-obvious public
surface (e.g. when a leading-underscore name must remain importable for back-compat tests).
Otherwise omit it; an absent `__all__` lets Python's default rules apply and avoids the
"forgot to update `__all__`" review nit.

### 3.6 Imports

Three blocks, in order, separated by a single blank line:

1. Standard library.
2. Third-party.
3. First-party (`mediaman.*`).

```python
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from fastapi import APIRouter, Depends, Request

from mediaman.audit import log_audit
from mediaman.core.time import parse_iso_utc
from mediaman.db import get_db
```

- `from __future__ import annotations` is mandatory in every new module. Annotations are
  strings; cycles do not bite.
- Cross-package imports are absolute (`from mediaman.services.arr.base import ArrClient`).
- Relative imports are allowed only for intra-package internals (`from ._helpers import x`)
  and only when the import would otherwise be more than one line.
- Wildcard imports (`from foo import *`) are forbidden except inside re-export barrels that
  define `__all__`.
- Aliasing (`as`) is for collision avoidance and short, conventional names
  (`numpy as np` if numpy were ever added). Aliasing to hide a verbose first-party name is
  a smell — fix the name instead.

### 3.7 `__init__.py`

- An empty `__init__.py` in a non-package directory is forbidden — see
  [§16](#16-deletion-checklist).
- A package `__init__.py` either re-exports its public surface (with an explicit
  `__all__`) or declares its purpose in a one-paragraph docstring.
- Side-effects at import time (singletons, registrations, settings reads) are forbidden in
  `__init__.py`. Bootstrap belongs in `bootstrap/`.

### 3.8 Module-level constants

Group constants at the top of the file, after imports, separated by a blank line. Use
`SCREAMING_SNAKE_CASE`. Mirror the unit in the name: `_TOKEN_TTL_DAYS`, never `_TTL`.
Constants used by tests must be importable directly; tests must not duplicate the literal.

```python
_TOKEN_TTL_DAYS = 30
_KEEP_GET_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)
```

## 4. Naming

Naming is the densest API surface a module exposes. A renamed function is a refactor; a
renamed concept is a migration. Choose names with the assumption that you cannot change
them.

### 4.1 Functions are verbs; nouns are reserved for data

A function name starts with a verb. A class or dataclass name is a noun.

```python
# Good
def fetch_pending_deletions(conn: sqlite3.Connection) -> list[ScheduledAction]: ...
def schedule_deletion(...) -> None: ...

@dataclass(frozen=True)
class ScheduledAction: ...

# Bad — noun-as-function obscures the action.
def pending_deletions(conn: sqlite3.Connection) -> list[ScheduledAction]: ...
```

A function that returns a derived value uses `compute_*`, `derive_*`, or `make_*`. A
function that mutates returns `None` and uses `mark_*`, `apply_*`, `record_*`. A function
that asks a yes/no question uses a predicate prefix (see [§4.2](#42-predicate-prefixes)).

### 4.2 Predicate prefixes

Boolean returns must start with `is_`, `has_`, `should_`, `can_`, or `was_`. The shape of
the answer is then visible at the call site without checking the signature.

```python
# Good
if is_protected(conn, media_id): ...
if has_active_session(user): ...
if should_send_newsletter(now): ...

# Bad — looks like it returns the protection record.
if protected(conn, media_id): ...
```

### 4.3 Forbidden generic names

The following names are forbidden at module or class scope:

- `data`, `info`, `result`, `value`, `obj`, `item`, `tmp`, `x`, `y`, `z`
- `helper`, `util`, `utils`, `manager`, `handler`, `processor`
- `do_*`, `_run`, `process`

`item` is allowed only as a loop variable when the iterable is named (`for item in
media_items:`). `result` is allowed only as a local accumulator immediately followed by a
named return (`result = []; ... ; return result`). Anywhere else, choose the domain word.

### 4.4 Domain language wins

When the database column says `scheduled_actions.action`, the Python word is `action`. When
the user-facing label says "Keep", the Python word is `keep`, not `retain` or `protect`.
Cross-checking against the schema and the templates is part of code review.

| Domain word         | Synonyms forbidden                          |
|---------------------|---------------------------------------------|
| `scheduled_action`  | task, job, todo                             |
| `media_item`        | record, entry, asset, content               |
| `keep_token`        | save_token, retain_link, hold_link          |
| `snooze`            | defer, postpone, hold, pause                |
| `delete_intent`     | delete_request, deletion_plan, removal_job  |
| `subscriber`        | recipient, member, follower                 |
| `audit_log`         | history, log, journal (the *table*)         |

### 4.5 Constants

Constants are `SCREAMING_SNAKE_CASE`. The unit must appear in the name when one exists:

```python
# Good
_TOKEN_TTL_DAYS = 30
_LOGIN_LOCKOUT_SECONDS = 600
_MAX_POSTER_BYTES = 5 * 1024 * 1024

# Bad — what unit is 30?
_TOKEN_TTL = 30
```

Module-private constants take a leading underscore. Public constants (rare) do not.

### 4.6 No abbreviations

`config`, not `cfg`; `connection`, not `conn` *unless* the variable is a SQLite connection
object — `conn` is the project-wide convention there. `database`, not `db`, in prose;
`mediaman.db` is the package name and stays as is. `request`, not `req`; `response`, not
`resp`. The handful of permitted abbreviations are: `conn` (sqlite), `req`/`resp` only as
HTTP-test fixture names, `tmp_path` as a pytest fixture name, `id` as the standard PK
suffix.

### 4.7 Files match concepts

A file's name is the singular form of the concept it owns. `session_store.py`, not
`sessions.py`. `repository/scheduled_actions.py` is plural because it operates on the table
(group of rows); `web/models/scheduled_action.py` is singular because it defines one
view-model. Mismatches are a tell-tale sign of conflated responsibilities.

### 4.8 Test names describe behaviour

Test names are sentences:

```python
# Good
def test_keep_token_rejects_replay(): ...
def test_scheduled_deletion_skips_protected_items(): ...
def test_login_lockout_releases_after_window(): ...

# Bad
def test_keep_token_2(): ...
def test_engine_run(): ...
```

`test_<unit>_<behaviour>` reads at the top of a stack trace and tells the next maintainer
exactly what regressed.

### 4.9 Loggers, lockers, and singletons

A module-level logger is named `logger` (`logger = logging.getLogger(__name__)`). A
module-level `threading.Lock()` is named `_<resource>_lock`. A cached singleton is
`_<resource>` and the helper that returns it is `get_<resource>()`. Consistency here means
grep finds every owner.

## 5. Type System & Data Shapes

Types are not decoration. They are the shape contract a function exports to its callers and
to mypy. A function with imprecise types pushes the burden of correctness onto every caller.

### 5.1 Public functions are fully typed

Every public function and method must annotate every parameter and the return type. Private
helpers (leading underscore) should also be annotated unless the function is a one-line
trivial wrapper. `mypy src/mediaman` passes on `main` — partial annotations cost more than
they save because they hide the error class behind weaker neighbours.

```python
# Good
def fetch_pending_deletions(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[ScheduledAction]: ...

# Bad — the caller has no idea what comes back.
def fetch_pending_deletions(conn, *, limit=100): ...
```

### 5.2 Frozen dataclasses for pure I/O shapes

A function that takes structured input or returns structured output uses a frozen dataclass.
Mutability is opt-in, not opt-out:

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class ScheduledAction:
    id: int
    media_item_id: str
    action: str
    execute_at: datetime
    token_used: bool
```

`slots=True` shaves memory and prevents accidental attribute creation. `frozen=True`
forbids accidental mutation; the price is `dataclasses.replace()` for updates, which is
cheap.

### 5.3 `TypedDict` for external API response shapes

When mediaman talks to a foreign API (Sonarr, Radarr, TMDB, OMDb, Mailgun, OpenAI), the
parsed JSON is described as a `TypedDict`. This pins the field names and types without
copying values into a dataclass when no transformation is needed.

```python
from typing import TypedDict, NotRequired

class TmdbMovieDetail(TypedDict):
    id: int
    title: str
    release_date: str
    runtime: NotRequired[int]
    poster_path: NotRequired[str | None]
```

`NotRequired` marks fields that the upstream may omit. The route handler or service
translates the `TypedDict` into a domain dataclass; downstream consumers never see the raw
foreign shape.

### 5.4 `Optional` only for genuine absence

`X | None` (the project uses PEP 604 unions; do not write `Optional[X]`) is reserved for
cases where `None` is a meaningful value: "not yet set", "user opted out", "expired and
cleared". A function that returns `X | None` because *some failure path* might want to
return early is using `None` as a half-baked exception — raise instead.

```python
# Good — None means "no row exists yet".
def find_active_session(conn: sqlite3.Connection, token: str) -> Session | None: ...

# Bad — None hides "the call to Sonarr failed".
def fetch_series_metadata(arr: SonarrClient, tvdb_id: int) -> SeriesMetadata | None: ...
# Better: raise SonarrError, return SeriesMetadata.
```

### 5.5 `Any` is a code smell

`typing.Any` is the escape hatch. Every use needs a one-line comment immediately above
explaining why a tighter type is impossible. Common reasons: "Plex `MediaItem` lacks a
stable upstream type", "ad-hoc dict from XML-RPC". Without a comment, mypy's strictness is
cosmetic — fix the type.

### 5.6 Pydantic at boundaries only

Pydantic models are for HTTP-boundary parsing in `web/routes/*` and `web/models/*`. Once
the request is validated, services and the scanner work with plain dataclasses or
`TypedDict`s. A Pydantic model in `services/` or `scanner/` is wrong; its validation cost
is paid for every internal call and its schema couples internals to the wire format.

```python
# Good — boundary parses Pydantic; service receives a dataclass.
class KeepRequest(BaseModel):
    duration: Literal["7", "30", "90", "forever"]

def keep_post(body: KeepRequest, ...) -> Response:
    apply_keep_snooze(conn, action_id=..., duration_days=int(body.duration))

# Bad — Pydantic leaks into the service.
def apply_keep_snooze(conn: ..., body: KeepRequest) -> None: ...
```

### 5.7 `from __future__ import annotations`

Mandatory in every new module. All annotations become strings; circular type-only imports
disappear; `list[int]` works on every supported version. The cost is a single import at the
top of the file.

### 5.8 `TYPE_CHECKING` to break cycles

Type-only imports that would otherwise cycle are placed under `TYPE_CHECKING`:

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mediaman.scanner.engine import ScanEngine

def schedule(engine: ScanEngine) -> None: ...
```

Runtime cycles are a design problem; type-only cycles are routine and fixed this way.

### 5.9 No tuple returns of more than two items

`tuple[X, Y, Z]` reads as a positional puzzle at the call site. Two items is a pair (often
`(value, found)` or `(start, end)`); three or more must be a dataclass.

```python
# Bad
def parse_token(s: str) -> tuple[str, int, datetime, bool]: ...

# Good
@dataclass(frozen=True)
class ParsedToken:
    raw: str
    action_id: int
    expires_at: datetime
    is_admin: bool
```

### 5.10 Iterables in, sequences out

Function inputs declare the weakest type that suffices: `Iterable[T]`,
`Mapping[K, V]`, `Collection[T]`. Outputs declare the strongest type the caller needs:
typically `list[T]` or `tuple[T, ...]`. This avoids "I have a generator but the function
wants a list" friction at the call site.

### 5.11 No `str` for things that aren't text

A media-item ID, a Plex section ID, a token — these are not "strings". Use a
`typing.NewType` if the cost is justified, or a small dataclass when the value carries
related fields. At minimum, name parameters precisely (`media_item_id: str`, never `id:
str`).

## 6. Errors

Exceptions are part of the API. They are designed at the same time as the success path.
Unexpected exceptions propagate; expected ones are caught at the layer that knows the
remediation.

### 6.1 Domain exceptions live where they are raised

Each subsystem owns its exception hierarchy in the same module that raises it.
`services/arr/base.py` defines `ArrError`, `ArrAuthError`, `ArrTimeoutError`. The HTTP
layer in `services/infra/http/client.py` defines `HttpError`, `HttpRetryExhausted`. The
scanner's deletion executor defines `DeletionError` next to the executor.

Generic `RuntimeError`, `Exception`, `ValueError` (outside argument validation), and
`AssertionError` (outside development-time invariants) are not domain errors. Raising one
in production code is a bug — the caller cannot meaningfully handle it.

### 6.2 Exception class hierarchy

A subsystem's exception base inherits from `Exception`, never from `BaseException`. Each
specific case is a subclass:

```python
class ArrError(Exception):
    """Base for all Sonarr/Radarr client failures."""

class ArrAuthError(ArrError):
    """API key invalid or insufficient permission."""

class ArrTimeoutError(ArrError):
    """Upstream did not respond within the configured budget."""

class ArrUpstreamError(ArrError):
    """Upstream returned a 5xx after retries were exhausted."""
```

Subclasses are domain-shaped, not HTTP-status-shaped. `ArrAuthError` covers 401 and 403
because the *caller's* response is the same — show the API-key warning in settings.

### 6.3 `raise X from original` is mandatory

When converting one exception to another, always use `raise NewError(...) from original` (or
`from None` if the original is sensitive — see [§10](#10-security)). The traceback is
forensic evidence; severing it is destruction of evidence.

```python
# Good
try:
    response = self._session.get(url, timeout=self._timeout)
except requests.Timeout as e:
    raise ArrTimeoutError(f"timeout after {self._timeout}s") from e

# Bad — caller cannot diagnose.
try:
    response = self._session.get(url, timeout=self._timeout)
except requests.Timeout:
    raise ArrTimeoutError("timed out")
```

### 6.4 `except Exception:` is reserved for outermost loops

The only legitimate uses of bare `except Exception:` are:

1. The outermost retry loop in `services/infra/http/client.py`.
2. The scheduler's job runner — a job must not crash the scheduler.
3. The FastAPI exception handler — turning anything we missed into a 500.
4. The recovery routines that run on cold start (`_recover_stuck_deletions`,
   `_recover_pending_notifications`).

Every such site has a `# rationale:` comment above it explaining why the broad catch is
correct, and inside the handler the exception is *both logged with traceback* and either
re-raised, returned, or recorded.

```python
# Good — outermost; logs + records to the audit log; does not swallow silently.
for action in pending:
    try:
        executor.delete(action)
    except Exception as exc:  # rationale: scheduler must survive a single bad row
        logger.exception("scheduler.delete_failed", extra={"action_id": action.id})
        repository.mark_action_errored(conn, action.id, repr(exc))
```

### 6.5 Validation errors at the boundary, once

A request body is validated by Pydantic at the route boundary. The 4xx response is produced
once, there. Inner functions trust the input shape. Re-raising a Pydantic `ValidationError`
from a service layer means the contract is wrong — split the function or validate at a
narrower boundary.

### 6.6 Distinguish expected from unexpected

For every `except` clause, ask: *did I expect this could happen?* If yes, you must have a
plan: log at INFO, return a fallback, retry, present a user-facing error. If no, the
exception must propagate untouched. There is no third option.

```python
# Good — expected: stale Plex item; plan: skip and continue.
try:
    plex_item = plex.fetch_item(media_id)
except PlexNotFound:
    logger.info("scan.item_disappeared", extra={"media_id": media_id})
    continue

# Good — unexpected: bug; let it propagate.
plex_item = plex.fetch_item(media_id)
```

### 6.7 Never swallow without re-raising or logging

A `try/except` whose `except` block contains only `pass` is forbidden. The minimum legal
shape is `logger.exception(...)` *or* `return fallback`; both is allowed. Silent swallow is
the largest single source of latent bugs in any codebase.

### 6.8 No catching `KeyboardInterrupt` or `SystemExit`

These inherit from `BaseException`, not `Exception`, and `except Exception:` is correct in
not catching them. If you ever write `except BaseException:`, you are wrong.

### 6.9 Errors carry context

An exception's message is consumed by humans reading logs. Include the inputs that produced
it, never the secrets:

```python
# Good
raise SonarrError(f"add_series failed for tvdb_id={tvdb_id}: status={status}")

# Bad — operator cannot tell which series.
raise SonarrError("add_series failed")

# Bad — leaks the API key.
raise SonarrError(f"add_series failed; url={full_url_with_apikey}")
```

### 6.10 Exception types are testable

A test that asserts a failure path uses `pytest.raises(SpecificError)` with `match=` for
the relevant phrase. `pytest.raises(Exception)` is forbidden in non-test code paths and a
review nit in tests — see `pyproject.toml`'s `PT011` ignore for the historical exception in
`tests/`.

## 7. Logging & Observability

Logs are the only window the operator has into a process they cannot attach a debugger to.
A noisy log buries the signal; a quiet log hides the bug. Both are failures.

### 7.1 One logger per module

```python
import logging
logger = logging.getLogger(__name__)
```

`__name__` produces `mediaman.scanner.engine`, `mediaman.web.routes.keep`, and so on. The
operator can raise the level for a single noisy area without touching the rest. The
historical `logging.getLogger("mediaman")` is permitted only in modules that pre-date this
guideline; new modules must use `__name__`.

### 7.2 Levels with concrete examples

- **`DEBUG`** — for the developer running the process locally. Shape of payloads, branch
  taken, cache hit/miss. Disabled in production by default.
- **`INFO`** — domain events the operator wants to see in the steady state. Scan started,
  scan finished with N actions, newsletter sent to M subscribers, login succeeded for
  user.
- **`WARNING`** — recoverable anomaly. Sonarr returned 502, retrying; rate-limit budget
  exhausted; deprecated config value used.
- **`ERROR`** — operation failed; user impact possible. Includes a stack trace via
  `logger.exception(...)`.
- **`CRITICAL`** — process integrity at risk. Trusted-proxy wildcard rejected; secret-key
  entropy too low; canary decryption failed.

A log line at the wrong level is a bug. A `WARNING` that fires in the steady state is
either a real warning (fix the root cause) or wrongly classified (lower it).

### 7.3 Stable message strings, structured context

The first argument to `logger.info` is a stable, dotted *event name*. Variables go in
`extra=` or as keyword arguments routed through a structured handler — never interpolated
into the message string.

```python
# Good — greppable; Splunk-friendly.
logger.info(
    "scan.started",
    extra={"library_id": library_id, "library_type": library_type},
)

# Bad — every run is a different string.
logger.info(f"Started scan of library {library_id} ({library_type})")
```

The dotted form (`scan.started`, `keep.token_consumed`, `arr.add_series_failed`) makes
queries portable across dashboards and tests-on-logs.

### 7.4 Never log secrets

The following are forbidden as either log values or substrings of log values:

- API keys, passwords, password hashes, session tokens, keep tokens, download tokens.
- Encryption keys, HMAC secrets, IVs, ciphertexts.
- Full request bodies on POST endpoints.

Email addresses are *not* secrets in mediaman's threat model: the operator
hosts the instance and is the only audience for both operational logs and
the audit log. Logging full subscriber and admin emails is intentional —
operators need them to triage delivery failures and to read the audit
trail. Do not introduce email-masking helpers.

When forensic context is needed, use a length-bounded, irreversible identifier:

```python
def _scrub_token(token: str) -> str:
    return token[:6] + "..."  # enough to correlate two log lines, not enough to replay.
```

A leak through logs is a security finding. Treat it as one.

### 7.5 The audit log is for user-visible / security-relevant events

`mediaman.audit.log_audit` writes to a SQLite table that operators and (in some surfaces)
admins read. It is *not* a debug log. Entries are required for:

- Authentication (login success/failure, password change, session revocation).
- Authorisation (admin promotion, forced password change).
- Destructive actions (manual delete, scheduled deletion executed).
- State changes initiated via signed token (keep snooze, keep forever, download
  confirmation).
- Subscriber lifecycle (added, removed, opted-out).

Entries are **not** required for read traffic, scan steps that do not change state, or
internal recovery loops.

### 7.6 Don't log full request bodies

`request.json()` in a log line will eventually contain a credential, a token, or PII.
Log only the fields the audit policy names. The middleware's request-id is sufficient to
correlate; bodies do not belong in logs.

### 7.7 `logger.exception` is the right call inside an `except`

`logger.exception(message)` includes the active stack trace. `logger.error(message,
exc_info=True)` is equivalent. Plain `logger.error(str(exc))` discards the stack — never do
this inside an `except` block.

### 7.8 No `print` in production code

`print(...)` is a tool for one-off scripts. Production code uses the logger. The CI lint
gate fails on a `print` call inside `src/mediaman/`.

### 7.9 Observability surfaces

- `GET /healthz` — liveness only. Always 200 if the loop is responsive.
- `GET /readyz` — readiness; reports each dependency's state. 503 with structured JSON when
  a subsystem is down.
- The audit log — user-visible events.
- The scheduler log — scan results, deletion outcomes.

A new dashboard or alerting concern adds either a `/readyz` field or an audit-log event
type — never an out-of-band metric file.

### 7.10 Sentry-equivalent only at the outer handler

If a crash reporter is added, it lives at the FastAPI exception handler and the scheduler
job runner, never sprinkled inside services. The reporter is a sink, not a printf.

## 8. Concurrency & I/O

mediaman is single-worker by design ([§1.12](#112-one-process-one-truth)). That choice buys
simplicity but it does not buy permission to ignore concurrency. The scheduler runs jobs on
its own threads; FastAPI runs sync handlers in a thread pool; the rate-limit buckets are
touched from every request. State that crosses a thread is shared state.

### 8.1 Outbound HTTP only via the shared client

All outbound HTTP traffic — Sonarr, Radarr, NZBGet, TMDB, OMDb, Mailgun, OpenAI, poster
fetch — goes through `services/infra/http/client.py`. A new `requests.get(...)`,
`urllib.request.urlopen(...)`, or `httpx.Client()` outside that module is a code-review
blocker.

The shared client owns: connection pooling, the SSRF allowlist
([§10](#10-security)), retry/backoff, the timeout budget, the user-agent header, and TLS
verification. A bespoke client gets one of those wrong eventually; the shared one gets all
of them right by construction.

```python
# Good
from mediaman.services.infra.http.client import get_http_client

client = get_http_client()
response = client.get(url, timeout=10.0)

# Bad
import requests
response = requests.get(url, timeout=10.0)
```

### 8.2 SQLite only via `db/connection.py`

`get_db()` returns the request-scoped or job-scoped connection. The only `sqlite3.connect`
call in the production codebase is inside `db/connection.py`. A bare `sqlite3.connect(...)`
elsewhere bypasses the WAL configuration, the row factory, the foreign-keys pragma, and the
busy-timeout — all of which are silent correctness bugs in production.

Tests are allowed to construct in-memory connections via the `tmp_db` fixture; that fixture
goes through the same configuration code.

### 8.3 File I/O only inside `data_dir`

Production code reads and writes only paths under `MEDIAMAN_DATA_DIR` and the configured
delete roots in `MEDIAMAN_DELETE_ROOTS`. A new `open(...)` against `/tmp` or `/etc` in
`src/mediaman/` is a code-review blocker. Test fixtures use `tmp_path`; that is fine.

### 8.4 Blocking I/O in FastAPI runs in the thread pool

Route handlers are written `def` (sync), not `async def`, unless the body is genuinely
non-blocking (no I/O, no DB). A sync handler is dispatched by Starlette to a thread pool;
mixing in an `async def` that calls `requests.get` blocks the event loop and starves every
other request.

```python
# Good — handler is sync, the underlying I/O is blocking, dispatch handles it.
@router.get("/search")
def search(q: str) -> SearchResponse:
    return run_search(q)

# Bad — async handler that calls blocking I/O.
@router.get("/search")
async def search(q: str) -> SearchResponse:
    return requests.get(...)  # blocks the loop
```

If the project ever adopts an async upstream client, the handler signature must change
*and* the entire call chain must be `async def` end-to-end. No half-async code.

### 8.5 Module-level mutable state is forbidden by default

A module-level `dict` or `list` mutated at runtime is a global. It is, in practice,
worker-affine and untestable in isolation. The only legitimate uses are:

1. A documented in-process cache with either a TTL or a max size, declared with both at
   construction.
2. A documented singleton resource (HTTP session, scheduler) that owns a `threading.Lock`
   and a one-line comment explaining why a global is required.
3. A rate-limit bucket store, single-worker by design, with the same comment.

Each such site looks like:

```python
_LIBRARY_TITLE_CACHE: dict[int, str] = {}
_LIBRARY_TITLE_CACHE_LOCK = threading.Lock()
# rationale: per-process cache; rebuilt on settings change. Single-worker invariant.
```

Anything else moves to SQLite or to a request-scoped dependency.

### 8.6 Threads are named

Every `threading.Thread` is constructed with `name=`. Unnamed threads make `py-spy` and the
log message format meaningless. Same applies to `concurrent.futures.ThreadPoolExecutor` —
construct it with `thread_name_prefix=`.

### 8.7 Locks are narrow

A lock guards the smallest possible critical section. Holding a lock across an outbound
HTTP call, a SQLite write, or a long computation is a deadlock waiting to happen.

```python
# Good — read-modify-write, lock dropped before I/O.
with _CACHE_LOCK:
    cached = _CACHE.get(key)
if cached is not None:
    return cached
value = expensive_call()
with _CACHE_LOCK:
    _CACHE.setdefault(key, value)

# Bad — holds the lock across the network call.
with _CACHE_LOCK:
    if key in _CACHE:
        return _CACHE[key]
    _CACHE[key] = expensive_call()
```

### 8.8 Don't mix asyncio and threads

mediaman's threading model is "FastAPI thread pool + APScheduler thread pool + main thread".
There is no asyncio event loop in our code; FastAPI's exists, but we do not put work on it.
A new `asyncio.run`, `loop.create_task`, or `asyncio.to_thread` call requires an explicit
written justification — the failure modes of mixed paradigms are extremely hard to
diagnose.

### 8.9 Timeouts everywhere

Every outbound call has a timeout. `requests.get(url)` without a timeout hangs the worker
until the OS kernel decides. The shared HTTP client enforces a default; tests must not
mock around it.

### 8.10 Cancel-safe by default

Long-running scanner work checkpoints into SQLite at safe points. A `KeyboardInterrupt` or
a container SIGTERM that arrives mid-scan must leave the database in a consistent state —
the recovery routines on cold start exist precisely for this. New long-running operations
either commit incrementally or document why they cannot.

## 9. Database

mediaman is a SQLite shop. Every persistent fact lives in `mediaman.db`, which is
WAL-mode, encrypted-at-rest for sensitive columns via `crypto/`, and migrated forward by a
sequence-numbered runner. The database is the source of truth; in-memory state is a cache.

### 9.1 Schema is declared, not derived

The schema lives in `db/schema_definition.py` as a literal sequence of `CREATE TABLE` and
`CREATE INDEX` statements. There is no ORM. Reading the schema file tells you what shapes
exist; reading a Python class never does as authoritatively. Any change to the schema goes
through a migration — never by editing the definition file alone.

### 9.2 Migrations are append-only

`db/migrations/` contains zero-padded files (`0001_initial.py`, `0002_*.py`, …). Numbers
are never reused; gaps are forbidden. A migration:

1. Has a single `apply(conn: sqlite3.Connection) -> None` function.
2. Is idempotent against a partially-applied state (use `IF NOT EXISTS`, `INSERT OR
   IGNORE`, etc.).
3. Imports nothing outside `db/` and the standard library — migrations must run on a
   bare process before the rest of the package is loaded.
4. Carries a header docstring naming the change in plain English: "0014: Add
   `keep_tokens_used.consumed_at` to support replay-window forensics."

A merged migration is immutable. If a bug ships, write a follow-up migration that fixes
the data; never edit the original.

### 9.3 No hand-edits outside a migration

`sqlite3 mediaman.db "UPDATE ..."` against a production database is a one-way ticket to a
divergent fleet. Every state change is either:

- a migration, or
- code running through a repository function and recorded in `audit_log`.

If a one-off correction is needed, write a migration. The next deployment carries it
forward, and the change is auditable.

### 9.4 One repository module per table-group

A package that owns persistent state owns its own `repository/` subpackage:

```
scanner/repository/
    __init__.py           (re-exports the public surface)
    media_items.py        (CRUD on media_items)
    scheduled_actions.py  (CRUD on scheduled_actions, kept_shows, snoozes)
    settings.py           (CRUD on settings — shared but scanner is the writer)
```

A repository function does **one** SQL operation. A function that runs three SELECTs and
two UPDATEs is composing logic; lift the composition out into a service or a phase. The
repository is the boundary, not the brain.

### 9.5 Repository returns dataclasses, never raw rows

`sqlite3.Row` is convenient and leaky. Once a row leaves the repository, every consumer
becomes coupled to the column order and the integer-vs-text handling.

```python
# Good
def fetch_pending_deletions(conn: sqlite3.Connection) -> list[ScheduledAction]:
    rows = conn.execute(
        "SELECT id, media_item_id, action, execute_at, token_used "
        "FROM scheduled_actions "
        "WHERE action = ? AND token_used = 0",
        (DELETION_ACTION,),
    ).fetchall()
    return [_row_to_scheduled_action(row) for row in rows]

# Bad — caller now reaches into row["execute_at"] in five places.
def fetch_pending_deletions(conn) -> list[sqlite3.Row]: ...
```

### 9.6 Parameter-substitute, always

```python
# Good
conn.execute("SELECT 1 FROM scheduled_actions WHERE id = ?", (action_id,))

# Catastrophic — string concatenation into SQL is a security incident.
conn.execute(f"SELECT 1 FROM scheduled_actions WHERE id = {action_id}")
```

Even if the value is "obviously safe" — a counter, an enum, a known integer — use a
parameter. The cost is one tuple; the savings is "we never had a SQL injection". A `f`-string
or `%`-formatting that builds SQL is a code-review blocker.

The only legitimate dynamic-SQL pattern is interpolating a constant column name from a
small allowlist:

```python
_SORTABLE = {"added_at", "title", "size_bytes"}
if sort_key not in _SORTABLE:
    raise ValueError(f"unsortable column: {sort_key!r}")
sql = f"SELECT * FROM media_items ORDER BY {sort_key} DESC"
```

### 9.7 Transactions are explicit

A multi-statement repository operation runs inside a `with conn:` block. SQLite's implicit
transaction on the first DML statement is not a contract — be explicit.

```python
def apply_keep_snooze(conn: sqlite3.Connection, action_id: int, days: int) -> None:
    with conn:
        cursor = conn.execute(
            "UPDATE scheduled_actions SET ... WHERE id = ? AND token_used = 0",
            (..., action_id),
        )
        if cursor.rowcount != 1:
            raise StaleAction(action_id)
        log_audit(conn, "keep.snooze", {"action_id": action_id, "days": days})
```

If the audit log fails, the snooze rolls back. The two are part of one fact.

### 9.8 Connection lifecycle

- One connection per request, opened in a FastAPI dependency, closed by the dependency.
- One connection per scheduled job, opened by the runner, closed when the job ends.
- Long-lived connections are forbidden — they hold file locks across the WAL checkpoint
  and break the backup story.
- A bare `sqlite3.connect(...)` outside `db/connection.py` is a review-blocker
  ([§8.2](#82-sqlite-only-via-dbconnectionpy)).

### 9.9 Encrypted columns

Columns that store integration credentials or personal data are encrypted at rest via
`crypto/`. The repository function that reads such a column also decrypts it; the caller
sees plaintext. The repository function that writes it accepts plaintext and encrypts.
Plaintext never lands on disk; ciphertext never escapes the repository.

### 9.10 Foreign keys, on

`PRAGMA foreign_keys = ON` is set on every connection in `db/connection.py`. Tests must
respect it. A migration that introduces a new FK pre-checks the data and fails loudly if
the FK would be violated.

### 9.11 Schema queries (`sqlite_master`) are forbidden in production paths

A production code path that introspects the schema is a sign someone wanted a migration
and reached for runtime introspection instead. Schema is declared, migrations apply it;
the code path runs against a known shape.

## 10. Security

mediaman handles credentials for half a dozen external services and the user's media
library on disk. Every change to a route, a token format, or a deletion path is a
security change. Treat the threat model in `DESIGN.md` as a living constraint, not a
backstop.

### 10.1 URL, path, and command inputs go through `core/url_safety.py`

A URL accepted from configuration or a request must be validated for SSRF before any
outbound call. A filesystem path accepted from anywhere must be validated against
`MEDIAMAN_DELETE_ROOTS` before a write or delete. There is no "the user is the admin so we
can skip this" — defence in depth means the admin is treated as the threat too.

```python
# Good
from mediaman.core.url_safety import validate_outbound_url

url = validate_outbound_url(raw_url)  # raises SsrfRefused on private IPs, schemes, ...
response = client.get(url, timeout=10.0)

# Bad
response = client.get(raw_url, timeout=10.0)
```

Subprocess invocation (`subprocess.run`, `os.system`) of an external program with
user-supplied arguments is forbidden in `src/`. If a future feature requires it, the call
goes through a hardened helper with `shlex.split`-rejected arguments and a strict
allowlist of binaries.

### 10.2 Tokens are short-lived, signed, single-use

Every token mediaman emits — keep tokens, download confirmation tokens, session tokens,
password-reset tokens (when added) — is:

1. **Short-lived.** A documented TTL (`_TOKEN_TTL_DAYS = 30` for keep tokens, 24h for
   sessions). The TTL is a constant in the module, named with the unit.
2. **Signed.** HMAC-SHA256 with the per-install key derived via HKDF from
   `MEDIAMAN_SECRET_KEY`. Verification uses `hmac.compare_digest` — never `==`.
3. **Single-use unless documented.** Single-use tokens record their consumption in a
   `*_used` table (`keep_tokens_used`, `used_download_tokens`); replay is rejected by the
   primary-key constraint, not by application logic.

A token persisted in the DB is stored as `SHA-256(token)`, never raw. The raw token is
recoverable only from the email or the URL the user holds.

### 10.3 Secrets live in env or in encrypted DB

The only plaintext secret on disk is `MEDIAMAN_SECRET_KEY`, supplied via environment
variable. Every other secret (Plex token, Sonarr API key, NZBGet password, Mailgun key,
TMDB key, OMDb key, OpenAI key) is encrypted at rest via `crypto/` and decrypted at the
repository boundary. A new secret follows the same pattern:

1. Add a column in a migration.
2. Encrypt on write, decrypt on read inside the repository.
3. Configure via the web UI; never via env, never in `working_directory/`.

A `.env`-stored credential other than `MEDIAMAN_SECRET_KEY` is a regression.

### 10.4 Cookies are HTTP-only, Secure, SameSite=Strict

Session cookies set:

- `HttpOnly` — JS cannot read them.
- `Secure` — HTTPS only.
- `SameSite=Strict` — no cross-site delivery.
- `Path=/` and a TTL matching the session hard expiry.

A new cookie (a feature flag, a UI preference) defaults to the same attributes unless a
written justification documents the divergence.

### 10.5 CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy

Security headers are set globally by `web/middleware/`. Route-specific overrides require a
written justification. The current CSP includes `'unsafe-inline'`; eliminating it is on
the roadmap and must not regress further.

### 10.6 SSRF allowlist for outbound

Outbound traffic is allowed only to:

- The configured Plex, Sonarr, Radarr, NZBGet hosts (validated at config-write time).
- `api.themoviedb.org`, `image.tmdb.org`, `www.omdbapi.com`.
- `api.mailgun.net` and the configured Mailgun region.
- `api.openai.com`.

A new outbound destination is added to the allowlist in the same PR that introduces it.
Wildcards are forbidden; the deny-by-default rule for outbound IP ranges
(RFC1918, link-local, loopback, IPv6 ULA) is enforced in `core/url_safety.py`.

### 10.7 Rate limiting at the boundary

Rate limits are enforced at the route handler — that is, at the trust boundary —
*before* any DB lookup that could leak timing. The shared `RateLimiter` is bucketed by
`/24` (IPv4) or `/64` (IPv6) prefix; per-IP buckets are bypassable by IPv6 attackers
otherwise. A handler that does work before checking the rate limit is a finding.

### 10.8 No timing oracles

Anywhere two secrets are compared, use `hmac.compare_digest`. This includes session
tokens, keep tokens, download tokens, and password hashes (bcrypt's `checkpw` is itself
constant-time; do not pre-strip or pre-compare).

The login path includes a *dummy* bcrypt verification on a missing user to keep the
timing equivalent to a wrong password. New auth-shaped endpoints follow the same pattern.

### 10.9 `MEDIAMAN_DELETE_ROOTS` fails closed

A deletion call validates the target path against the configured roots in
`MEDIAMAN_DELETE_ROOTS`. If the env var is unset, the validator returns *no* allowed
roots and every deletion fails with a `DeletionRefused`. This is intentional. A misconfig
that turns deletion into a no-op is a recoverable annoyance; one that deletes from `/etc`
is a catastrophe.

### 10.10 Audit log on every authenticated state change

Every authenticated mutation produces an `audit_log` row with: actor, action, target,
timestamp, source IP, user agent. Read-only routes do not log. Anonymous routes (keep
tokens, download confirmations) log against the token's identity, never the IP alone.

### 10.11 Mandatory security tests for new code

A change that touches authentication, token signing, deletion, SSRF, CSRF, rate limiting,
or audit logging adds or extends a test in `tests/unit/test_security_hardening*.py`. The
test asserts the *negative* case — that the bad input is rejected — not just the happy
path.

### 10.12 Trusted proxies are explicit

`X-Forwarded-For` and `X-Forwarded-Proto` are honoured only when the immediate peer
matches `MEDIAMAN_TRUSTED_PROXIES`. Wildcards (`*`, `0.0.0.0/0`, `::/0`) are rejected
with a `CRITICAL` log line; a wildcard would let any peer spoof the source IP and bypass
per-IP rate limits.

### 10.13 No `eval`, no `exec`, no `pickle` on untrusted input

`eval`, `exec`, and `pickle.loads` on data that crossed a trust boundary are forbidden.
JSON, `dataclasses.asdict`/`from_dict` (manually written), and `defusedxml` cover every
real need we have.

### 10.14 No new XML parsing without `defusedxml`

NZBGet speaks XML-RPC; mediaman parses it via `defusedxml`. `xml.etree.ElementTree.parse`
on untrusted input is forbidden; XXE is a real attack class.

### 10.15 Bandit clean

The bandit gate runs `-ll` (low confidence, low severity included). New code lands clean.
A `# nosec` is allowed only with a `# rationale: ...` on the same or preceding line.

## 11. Testing

Tests are not a chore; they are the executable specification of the system. A change with
no test is a change with no documented expectation.

### 11.1 Pyramid: 90% unit, 10% integration, 0% end-to-end

- **Unit tests** dominate. They run in milliseconds, take no network, take no real
  filesystem outside `tmp_path`, and exercise one function or one cohesive unit.
- **Integration tests** are reserved for module boundaries: a route through middleware,
  through a service, through the repository, against an in-memory SQLite. They use the
  FastAPI `TestClient` and the `tmp_db` fixture.
- **End-to-end tests** that hit live external services do not belong in this repository.
  External APIs are mocked at the HTTP layer.

### 11.2 One test file per source file

`src/mediaman/scanner/repository/scheduled_actions.py` ↔
`tests/unit/scanner/repository/test_scheduled_actions.py`. The mirroring is mechanical;
diffs of "moved a function from A to B" come with "moved its test from A to B" in the same
PR.

A test that spans two source files (because two modules co-evolved) lives next to the
*more concrete* of the two — the leaf, not the orchestrator.

### 11.3 Tests are named for behaviour

```python
def test_keep_token_rejects_replay() -> None: ...
def test_scheduled_deletion_skips_protected_items() -> None: ...
def test_login_lockout_releases_after_window() -> None: ...
```

A name that contains a function name (`test_apply_keep_snooze_2`) is a smell; the test is
coupled to *how* the behaviour is implemented. When the implementation moves, the test
either stops compiling or starts asserting the wrong thing. See [§4.8](#48-test-names-describe-behaviour).

### 11.4 Fixtures over setup helpers

Pytest fixtures compose; setup helpers do not. A new shared piece of test scaffolding goes
in `tests/conftest.py` (project-wide) or `tests/<area>/conftest.py` (scoped). A
`setup_module` or `setUp` is a smell — those are unittest patterns and they hide the
dependency tree.

### 11.5 Factories over hand-built objects

`tests/helpers/factories.py` exposes `make_scheduled_action(...)`,
`make_media_item(...)`, `make_session(...)`. A test that instantiates a dataclass with
seven positional arguments is reading like a riddle; the factory takes the values that
*matter* for the test and fills the rest with deterministic defaults.

```python
# Good
action = make_scheduled_action(action="scheduled_deletion", token_used=False)

# Bad — six fields the test does not care about distract from the one that matters.
action = ScheduledAction(
    id=1, media_item_id="m1", action="scheduled_deletion",
    execute_at=datetime(2026, 5, 1, tzinfo=UTC), token_used=False,
    token_hash="...", reason="...",
)
```

### 11.6 More than five mocks: wrong layer

A test that needs `MagicMock` six times for one assertion is testing the wiring, not the
behaviour. The fix is almost always to push the test down a layer (test the service, not
the route) or up a layer (use `TestClient` and let the wiring run for real).

### 11.7 No `time.sleep` in tests

`time.sleep` is an admission that the test is racing with itself. Replace with:

- A monotonic-clock fake passed in as a dependency.
- `freezegun` for `datetime.now(UTC)` callers.
- A condition-based wait against the actual signal (`assert queue.empty()` polled with a
  bounded retry helper).

A test with a sleep is flaky on a slow CI runner; we have lived with that and we are done
with it.

### 11.8 Coverage floor

The coverage floor is enforced in `pyproject.toml` via `fail_under`. The floor moves up,
never down. A PR that lowers the floor must contain a written justification — usually
"deleted code with no behaviour change" — and the lowered number applies only to that PR's
diff base, not subsequent work.

A test that exists only to make the line counter happy is a liability — see
[§11.3](#113-tests-are-named-for-behaviour). Coverage is a floor, not a target.

### 11.9 Don't test private helpers directly

If a function with a leading underscore is reachable from a public function, test through
the public one. Private helpers exist because they are implementation; testing them
freezes the implementation in place. The exception is a private helper that encapsulates a
notably tricky algorithm — those get one focused test that names the algorithm and a
comment pointing at the public callers.

### 11.10 Deterministic by construction

A test that depends on `datetime.now()`, `random.random()`, or filesystem ordering is
flaky waiting to happen. Inject the clock or the RNG, sort the iteration, or set a seed.
The test must produce the same result on the first run, the hundredth, and on a Friday at
17:59 UTC.

### 11.11 Parametrise instead of duplicating

```python
# Good
@pytest.mark.parametrize(
    ("duration", "expected_days"),
    [("7", 7), ("30", 30), ("90", 90)],
)
def test_keep_snooze_persists_duration(duration: str, expected_days: int) -> None: ...

# Bad — three near-identical tests with one literal changed.
def test_keep_snooze_7_days(): ...
def test_keep_snooze_30_days(): ...
def test_keep_snooze_90_days(): ...
```

### 11.12 Markers separate fast from slow

Tests are marked `@pytest.mark.unit` (default) or `@pytest.mark.integration`. CI runs both;
local `make test` runs both; a developer iterating on one area uses
`pytest -m unit -k <area>`. New markers require a written justification — the system
already has too many.

### 11.13 Assertion shape

`assert actual == expected` is the default. `assert actual` (truthy check) is permitted
only when the *type* is the assertion (e.g. `assert isinstance(x, ScheduledAction)`).
`assert not actual` for "should be empty/None" is permitted but `assert actual is None` /
`assert actual == []` is clearer and fails better.

### 11.14 Test doubles, not real upstreams

Mock at the HTTP boundary using `responses` or a fixture wrapping the shared HTTP client.
Never hit Sonarr, Radarr, NZBGet, TMDB, OMDb, Mailgun, or OpenAI from a unit test. An
integration test may use a recorded fixture (`tests/fixtures/<service>/<scenario>.json`)
checked into the repository.

### 11.15 No conditional logic in tests

A test with `if` or `try/except` is testing two things. Split it into two tests, or
parametrise. A test that asserts something only "if the platform is Linux" probably
belongs in a different test module entirely.

## 12. Documentation

Documentation has three audiences and three forms. The ops audience reads `README.md` and
`SECURITY.md`. The architecture audience reads `DESIGN.md`. The implementation audience
reads source code, docstrings, and this file. Each form has a job; do not conflate them.

### 12.1 Public function and class docstrings

Every public function and class has a docstring. The shape:

```python
def apply_keep_snooze(
    conn: sqlite3.Connection, *, action_id: int, days: int, token: str
) -> None:
    """Snooze a scheduled deletion for ``days`` days.

    Marks the original scheduled action's token as consumed and inserts a
    new ``snoozed`` row whose ``execute_at`` is ``now() + days``. The two
    writes share a transaction; either both land or neither does.

    Args:
        conn: Open SQLite connection.
        action_id: PK of the row in ``scheduled_actions`` to snooze.
        days: Snooze duration; must be one of ``VALID_KEEP_DURATIONS``.
        token: The raw keep token (used for replay-protection lookup).

    Raises:
        StaleAction: The action was already processed or no longer exists.
        ReplayedToken: The token was already consumed.
    """
```

A one-line summary on its own line, a blank line, then `Args:` / `Returns:` / `Raises:`
sections only when they add information. A trivial getter does not need an `Args:`
section.

### 12.2 Package `__init__.py` carries a paragraph

A package's `__init__.py` opens with a paragraph docstring describing what the package is
for, what it depends on, and what it forbids. This is the entry point for someone reading
the source tree for the first time.

```python
"""Sonarr/Radarr (`*arr`) HTTP client.

Driven by a single ``ArrSpec`` (see ``spec.py``); concrete clients
(``SonarrClient``, ``RadarrClient``) are thin shims pre-bound to the
right spec. The base client owns retries, auth headers, and timeout
budget; depends on ``services/infra/http``.

Forbidden: importing ``web/`` or ``scanner/``; calling ``requests``
directly; storing API keys in module state.
"""
```

### 12.3 Comments answer "why", never "what"

If the *what* needs explaining, rename. If the *why* is non-obvious — a workaround, a
domain rule, a security finding being closed, an upstream quirk — that goes in a
comment. Reference the finding by its plain-English invariant rather than its internal
label:

```python
# Good
# protected_forever wins over a later-id snooze; the schema does not
# enforce one-row-per-item, so we must not rely on row order.

# Bad
# Domain-05 finding #4: row-order bug.
```

The plain-English form survives a rename of the audit-finding system; the cross-reference
does not.

### 12.4 `TODO`, `FIXME`, `HACK`

- `# TODO(name):` — a known follow-up. Must include an issue link or a tracking ID. A
  bare `# TODO:` is forbidden.
- `# FIXME:` — a bug acknowledged inline. Same rules: link or tracking ID.
- `# HACK:` — an intentional workaround. Must include a *removal date* or removal
  trigger ("remove once setuptools>=77 is pinned").

A `TODO` older than six months is, by policy, a feature. Either close the issue, do the
work, or rewrite the comment as a permanent design note.

### 12.5 Markdown docs are the cookbook

`README.md` is for getting mediaman running. `SECURITY.md` is for reporting a
vulnerability. `DESIGN.md` is the architecture rationale. `CODE_GUIDELINES.md` (this
file) is how we agree to write code. Operational docs (deployment, backups) live in
README; architectural docs (threat model, scanner phases) live in DESIGN.

A new markdown file at the root requires a written justification. Sub-docs that drift
from the four canonical files become stale; they are deleted.

### 12.6 Inline ASCII diagrams over external assets

A diagram that fits in 80 columns lives in the source as ASCII art. A diagram that does
not fit that constraint usually wants to be split. `.png` and `.svg` assets in `docs/`
require a corresponding ASCII fallback for terminal readers.

### 12.7 Examples in docstrings are doctests or marked

A code block in a docstring is either a doctest (importable and runnable) or marked as
illustrative (a comment line `# illustrative` above the block). Examples that drift from
behaviour are worse than no examples; `make test` keeps them honest.

### 12.8 Changelogs

Significant behaviour changes are mentioned in the PR description and, for user-visible
changes, in `CHANGELOG.md` if one exists. A new release tag includes a changelog entry.
"Significant" is a judgement call; a one-character bug fix is not, a rate-limit threshold
change is.

## 13. Performance

mediaman's hot paths are: scan over a Plex library, render the library page, render the
dashboard, send a newsletter. None are CPU-bound. Performance work that is not against a
measured bottleneck is speculative; performance work that complicates the call site
without a number is harmful.

### 13.1 Don't optimise without a measurement

A PR that claims "this is faster" includes a number: a `cProfile` output, a `timeit`
benchmark, a request-latency histogram. "I think this is faster" is rejected. The
benchmark goes in the PR description; if the optimisation lands, the benchmark stays in
`tests/benchmarks/` (when one exists) so the next change can verify the regression has
not crept back.

### 13.2 Caches need a TTL or a max size

Every in-process cache declares one of:

- A TTL (`functools.lru_cache(maxsize=...)` or a custom TTL wrapper).
- A max size with an LRU eviction.
- A documented "rebuild on event" trigger (settings change, scan completion).

An unbounded cache is a memory leak. A cache without invalidation is a stale-data bug.
See [§8.5](#85-module-level-mutable-state-is-forbidden-by-default) for the structural
requirements.

### 13.3 N+1 queries are bugs

A loop that issues one SQL query per iteration is an N+1 and is treated as a bug at code
review. The fix is a single query with a `JOIN` or an `IN (?, ?, ?)` clause, or a
batched fetch. SQLite is fast; the round-trip is the cost.

```python
# Bad — N+1.
for action in actions:
    item = conn.execute(
        "SELECT * FROM media_items WHERE id = ?", (action.media_item_id,)
    ).fetchone()
    ...

# Good — one query, joined.
rows = conn.execute(
    "SELECT a.id, a.execute_at, m.title "
    "FROM scheduled_actions a JOIN media_items m ON a.media_item_id = m.id "
    "WHERE a.action = ? AND a.token_used = 0",
    (DELETION_ACTION,),
).fetchall()
```

### 13.4 Concurrent fan-out lives behind a budget and a worker cap

The TMDB poster fetch fans out to fetch posters for many items in parallel. Such a fan-out
declares:

- A maximum worker count (`max_workers=` on the executor).
- A per-call timeout from the shared HTTP client.
- A total deadline ("give up the whole batch after N seconds").
- Graceful degradation: a missing poster does not abort the page render.

A new fan-out without those four properties is rejected.

### 13.5 Profile with tools, not eyeballs

`cProfile`, `py-spy`, and `tracemalloc` are the trusted tools. Eyeballing a function and
declaring it "the hot path" is a recipe for optimising the wrong thing. The slow part of
a request is almost never the part that *looks* slow.

### 13.6 Lazy imports for heavy optional deps

`openai`, `plexapi`, and other multi-MB dependencies are imported at module top-level
when the module is on every code path; lazy-imported inside a function body when the
module is on a rare path (CLI helpers, optional features). A blanket "lazy-import
everything" is the wrong rule; use it where the import cost matters.

### 13.7 Pagination at the boundary

A route that returns a list of media items paginates server-side. A "return everything"
endpoint becomes a denial-of-service against the renderer the day the library exceeds a
few thousand items. Default page sizes are documented in the route docstring and pinned
to a constant.

### 13.8 SQLite-specific knobs

- WAL mode is set in `db/connection.py`; do not change it per-connection.
- `PRAGMA synchronous = NORMAL` for the main DB (durable enough; faster than `FULL`).
- `PRAGMA journal_size_limit` keeps the WAL bounded.
- `VACUUM` runs as a maintenance task, not in the request path.

A new pragma is added centrally, with a one-line comment naming the trade-off.

### 13.9 Don't pre-allocate for a hypothetical scale

mediaman targets one user, one library, a few hundred to a few thousand items.
"What if the library has a million items" is not a scale we serve. A change that
complicates the code in pursuit of theoretical scale is rejected; the simple version
ships, and the day a user reports it as slow we measure and decide.

## 14. Dependencies

Every runtime dependency is a permanent commitment to track its security advisories,
update its pinned version, and absorb its breaking changes. Treat new dependencies as
expensive.

### 14.1 New runtime dependencies require a written justification

A PR that adds a runtime dependency to `pyproject.toml` includes a paragraph in the
description covering:

- **What it does** for mediaman.
- **What was considered first.** stdlib first, an existing dependency second, custom
  code third. State each option and why it lost.
- **Long-term maintenance.** Who maintains the upstream, how often it releases, what
  CVE history it has.
- **Surface area.** How much of the dependency are we actually using.

A dependency that wins a single utility function should usually be inlined as a small,
tested helper. A dependency that becomes structural (FastAPI, Pydantic, SQLite, bcrypt,
cryptography) is a long-term partner; choose deliberately.

### 14.2 Pin with `~=`

Runtime and dev dependencies pin to the compatible-release operator (`~=`). Major
versions are bumped deliberately, not by Dependabot's whim. Patch and minor versions
flow in via Dependabot PRs that are reviewed like any other change.

```toml
dependencies = [
    "fastapi~=0.136",
    "uvicorn[standard]~=0.46",
    ...
]
```

### 14.3 Lockfile is authoritative for CI

`requirements.lock` is the single source of truth for CI installs. It is regenerated
inside a `python:3.12-slim` container via `bash scripts/pin-lock.sh`; the result is
reproducible across hosts. A PR that edits `pyproject.toml` and not the lockfile fails
the lock-freshness CI gate.

`pip-audit -r requirements.lock --require-hashes` runs in CI; a vulnerability with a
fix opens a Dependabot PR and a fix-or-justify deadline.

### 14.4 Vendor only when upstream is dead

A pinned third-party module is preferred over a vendored copy. Vendor only when:

- Upstream has not had a release in over a year *and* mediaman uses a custom patch.
- The dependency is so small (under 100 lines) that vendoring is cheaper than tracking
  it.

A vendored module lives under `src/mediaman/_vendor/<name>/` with the upstream license
file alongside.

### 14.5 No dev dependencies in production code paths

`pytest`, `httpx` (the test client), `mypy`, `ruff` — these are dev-only. A production
import from a dev-only package is a packaging bug. The `pyproject.toml` separation is
the canonical list; any drift is a code-review blocker.

### 14.6 Optional features stay optional

OpenAI is optional. The `openai` import is local to the recommendations service; the
rest of mediaman runs without it. A new optional feature follows the same pattern: lazy
import, a clean degradation path when the dependency is missing or misconfigured, and a
test that covers the disabled state.

### 14.7 Platform support

mediaman targets Python **3.12** (`requires-python = ">=3.12,<3.13"`). New code uses
3.12 features without apology. When Python 3.13 becomes the supported floor, the upper
bound moves in a single PR with a green CI run.

The container is `python:3.12-slim`; the host is Linux. A dependency that does not ship
a Linux wheel for our supported Python is rejected unless a written justification covers
the build cost in CI.

### 14.8 No dynamic dependency installation

`pip install` at runtime is forbidden. A feature that needs an extra package adds it to
`pyproject.toml` and ships it in the container; runtime installation is a deployment
attack surface and a startup-flake source.

## 15. Workflow

The workflow is the contract between contributors and the project. It is short on
purpose; rules that aren't enforced by tooling are rules that aren't enforced.

### 15.1 Each commit builds and tests on its own

A PR is reviewed commit-by-commit; bisect is reviewed commit-by-commit. Every commit on
`main` is a green commit. WIP work that interleaves "broken" and "fixed" states is
squashed before merge.

A "stack of small commits each green" is the goal. A "single mega-commit" is the
fallback when the change cannot be cleanly split. A "stack of red commits" is rejected.

### 15.2 PR description: what / why / tested

Every PR opens with three sections:

```
## What
One paragraph: the user-visible or developer-visible change.

## Why
One paragraph: the reason. A bug fix names the bug; a feature names the user need.

## Tested
A bulleted list: which tests cover the change, which ad-hoc verification was done.
```

A PR description that is only a bullet list of file changes is rejected. The diff
already shows what changed; the description names *why*.

### 15.3 `make check` is the pre-push gate

`make check` runs lint, format-check, typecheck, and the test suite in the same
configuration CI uses. Local pass means CI pass, modulo Python patch version drift. A
contributor who pushes a PR that fails CI on a gate `make check` would have caught is
expected to apologise to the bots and fix it.

### 15.4 Pre-commit on, ruff first

`pre-commit install` is a one-time setup; afterwards, `git commit` runs `ruff` on
staged files. mypy is intentionally not in pre-commit (cold-cache cost); CI runs it on
every PR. New hooks need a written justification.

### 15.5 Branch naming

`feat/<topic>` for features, `fix/<topic>` for bug fixes, `chore/<topic>` for
infra, `refactor/<topic>` for code-shape changes with no behaviour delta,
`docs/<topic>` for documentation. The topic is kebab-case, ASCII, descriptive
(`feat/keep-token-replay-window`, not `feat/stuff`).

### 15.6 Commit-message style

The first line is imperative, present tense, under 72 characters, no trailing period:

```
Add replay window to keep tokens

The keep-token verifier currently allows infinite replay until expiry.
Add a per-token used-flag table; reject second use with a 410.
```

A blank line separates subject from body. The body wraps at 72 columns and explains the
*why* (which the diff cannot show on its own).

### 15.7 Never `--no-verify` without a documented reason

Skipping pre-commit hooks (`git commit --no-verify`) bypasses the formatter and the
linter. The next CI run catches the omission, costing time and review trust. The only
legitimate use is committing inside a hook that calls back into git; if that ever
happens, the commit message names the reason.

`--no-gpg-sign` is forbidden if commit signing is configured for the repository.

### 15.8 CI gates are not optional

| Gate           | Tool                                       | Failure means                              |
|----------------|--------------------------------------------|--------------------------------------------|
| Tests          | `pytest -q --cov=mediaman`                 | Behaviour regression or coverage drop      |
| Lint           | `ruff check .`                             | Lint violation                             |
| Format         | `ruff format --check .`                    | Formatter drift                            |
| Types          | `mypy src/mediaman`                        | Type contract violation                    |
| Security       | `bandit -r src/ -c bandit.yaml -ll`        | Security smell                             |
| Audit          | `pip-audit -r requirements.lock`           | Known CVE in pinned dep                    |
| Lock freshness | regenerate-and-diff                        | `pyproject.toml` and lockfile out of sync  |
| Docker build   | multi-stage, digest-pinned                 | Image won't build reproducibly             |

A red gate blocks merge. "Re-run until green" is forbidden — flakiness is a defect, file
an issue and fix the test.

### 15.9 Reviews

A PR needs at least one approval from someone other than the author. The reviewer's
job is to apply the rules in this document, not to admire the diff. A reviewer who
spots a violation must request a change; a reviewer who approves a violation has
broken the contract.

A PR that touches security-relevant code (auth, tokens, deletion, SSRF) needs an
approval from a reviewer with security context. The label `security` flags such PRs;
applying it accurately is part of the author's responsibility.

### 15.10 Force-push policy

Force-push is allowed on a developer's own feature branch up to the moment a reviewer
starts. After review begins, force-pushes destroy review context; prefer additional
commits, then squash at merge time. Force-push to `main` is forbidden.

### 15.11 Merge style

PRs merge with a squash. The squash commit message is the PR description's "What"
section, edited to imperative form. The PR title becomes the squash subject; keep titles
under 72 characters.

### 15.12 Hotfix path

A hotfix branches from the latest release tag, not `main`. It carries the smallest
possible diff, lands the fix, ships a patch release, and is rebased into `main` as the
authoritative copy. A hotfix that drifts from `main` is reconciled in the same week.

## 16. Deletion Checklist

A merge-time checklist. Every box is the *removal* of something that pretends to be
useful. Each item below has been a real source of churn in this codebase or in
neighbours of this codebase. Run the list against every PR before approving.

### 16.1 Dead branches

- [ ] No `if False:` or `if 0:` blocks.
- [ ] No `if DEBUG:` checks against constants that are always False in production.
- [ ] No `unreachable` paths after a `raise` or `return`.

### 16.2 Commented-out code

- [ ] No code commented out "in case we need it later". Git remembers; comments do not
      enforce semantics.
- [ ] No `# old_implementation_v2 = ...` blocks.
- [ ] A reference implementation in a comment is allowed only if it is markdown-fenced
      and explicitly *illustrative* — not runnable code.

### 16.3 Defensive guards on impossible conditions

- [ ] No `if x is None: return None` immediately after a function whose return type
      excludes `None`.
- [ ] No `try/except KeyError` around a dict access that the surrounding code has just
      established.
- [ ] No `assert isinstance(x, T)` at the top of a function with a typed signature —
      either trust the type or document the boundary.

### 16.4 Wrappers that just call stdlib

- [ ] No `def get_now() -> datetime: return datetime.now(UTC)`. Inline.
- [ ] No `def to_json(x): return json.dumps(x)`. Inline.
- [ ] A wrapper earns its name only when it adds behaviour: validation, structured
      logging, retry, a domain-specific default.

### 16.5 Audit-finding cross-references replaced with invariants

- [ ] No `# finding 22` or `# Domain-06 #7` style comments. Replace with the
      plain-English invariant ([§12.3](#123-comments-answer-why-never-what)).
- [ ] No `# fixed in P3` comments — the git history is the audit trail.

### 16.6 Re-exports unused by any test or call site

- [ ] No `__all__` entries that grep finds zero callers for.
- [ ] No `from foo import bar` at module top-level when `bar` is never referenced inside
      the module — it is either an accidental re-export or dead code.

### 16.7 Empty `__init__.py` in non-package directories

- [ ] An `__init__.py` is present *only* on directories that are Python packages.
- [ ] An `__init__.py` that is empty in a package with a non-trivial public surface
      either gets a paragraph docstring ([§12.2](#122-package-initpy-carries-a-paragraph))
      or its package is reconsidered.

### 16.8 Files over the 500-line ceiling

- [ ] No source file exceeds 500 lines without a `# rationale:` header
      ([§3.1](#31-hard-limits)).
- [ ] If a file is between 400 and 500 lines, a tracking issue exists for the planned
      decomposition.

### 16.9 Functions over the 60-line ceiling

- [ ] No function body exceeds 60 lines without a `# rationale:` comment.
- [ ] A 60-line function that contains a `for` loop with three or more responsibilities
      is decomposed before merge.

### 16.10 Module-level mutable state without a Lock + comment

- [ ] Every module-level mutable container has a paired `threading.Lock` *and* a
      one-line `# rationale:` comment naming why it is in-process state
      ([§8.5](#85-module-level-mutable-state-is-forbidden-by-default)).
- [ ] Caches declare a TTL or a max size at construction.

### 16.11 `Any` annotation without a comment

- [ ] Every `typing.Any` carries an immediately-preceding comment explaining why a
      tighter type is impossible ([§5.5](#55-any-is-a-code-smell)).
- [ ] No `cast(Any, x)` without the same documentation.

### 16.12 Bare `except Exception:` outside an outer retry boundary

- [ ] Every `except Exception:` is at one of the four legitimate sites
      ([§6.4](#64-except-exception-is-reserved-for-outermost-loops)) *and* logs with
      `logger.exception(...)` *and* either re-raises or records to the audit log.
- [ ] Every `except BaseException:` is removed.

### 16.13 `try/except` that swallows

- [ ] No `except SomeError: pass` block. Either log, re-raise, or take a documented
      action.
- [ ] No `except SomeError: return None` masking a real failure that should be a
      domain exception.

### 16.14 Tests with more than five mocks

- [ ] Count `MagicMock`, `patch`, and `monkeypatch.setattr` calls in each test. More
      than five means the test is wired to the wrong layer
      ([§11.6](#116-more-than-five-mocks-wrong-layer)).

### 16.15 `time.sleep` in tests

- [ ] No `time.sleep` in `tests/`. Replace with `freezegun`, an injectable clock, or a
      condition-based wait ([§11.7](#117-no-timesleep-in-tests)).

### 16.16 Print statements

- [ ] No `print(...)` in `src/mediaman/`. Use the logger
      ([§7.8](#78-no-print-in-production-code)).
- [ ] No `pprint`, no `pdb.set_trace()`, no `breakpoint()` left behind.

### 16.17 String-built SQL

- [ ] Every `conn.execute` argument is a string literal — no f-strings, no `%`
      formatting, no `+` concatenation of user-provided values into the SQL
      ([§9.6](#96-parameter-substitute-always)).

### 16.18 Logging secrets

- [ ] No log line interpolates a token, password, hash, or key
      without going through a scrub helper ([§7.4](#74-never-log-secrets)).
- [ ] No `logger.debug(f"request body: {body}")`.

### 16.19 New outbound HTTP not via the shared client

- [ ] No new `requests.get`, `requests.post`, `urllib.request`, or `httpx.Client`
      outside `services/infra/http/client.py`
      ([§8.1](#81-outbound-http-only-via-the-shared-client)).

### 16.20 New `sqlite3.connect`

- [ ] No new `sqlite3.connect(...)` outside `db/connection.py`
      ([§8.2](#82-sqlite-only-via-dbconnectionpy)).

### 16.21 New env vars

- [ ] Every new env var is documented in `README.md` with name, type, default, and
      purpose. An undocumented env var is invisible.
- [ ] Bootstrap-only env vars are validated in `bootstrap/`. Operational env vars
      probably belong in the encrypted settings table instead.

### 16.22 New dependency without justification

- [ ] A new entry in `pyproject.toml` carries a written justification in the PR
      description ([§14.1](#141-new-runtime-dependencies-require-a-written-justification)).
- [ ] The lockfile is regenerated in the same PR.

### 16.23 Naming smells

- [ ] No `data`, `info`, `result`, `tmp`, `obj`, `helper`, `util` at module or class
      scope ([§4.3](#43-forbidden-generic-names)).
- [ ] No abbreviated parameter names except the project-wide conventions
      ([§4.6](#46-no-abbreviations)).

### 16.24 TODOs without ownership

- [ ] Every `TODO` includes `(name)` and a tracking link
      ([§12.4](#124-todo-fixme-hack)).
- [ ] No `TODO` older than six months survives unaddressed.

### 16.25 Speculative generality

- [ ] No parameter that no caller supplies.
- [ ] No subclass that has no second concrete instance.
- [ ] No protocol/ABC introduced "for future flexibility" without a concrete second
      implementation in the same PR.

