<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: services-infra

## Purpose

Foundational, domain-agnostic infrastructure primitives that every higher
mediaman service (`scanner`, `services.arr`, `services.downloads`,
`services.mail`, `services.media_meta`, `services.openai`, `web` routes)
depends on. Two packages: **`services/infra/`** supplies the SSRF-safe outbound
HTTP client (DNS-rebind pinning, retry/backoff, size-cap + content-type
streaming guard), outbound-URL safety analysis, TOCTOU-hardened recursive
deletion plus read-only disk-usage queries, a DB-settings decrypt/JSON-unwrap
reader, and safe filesystem path resolution; **`services/rate_limit/`** supplies
IP-bucketed and per-actor sliding-window rate limiters plus trusted-proxy-aware
client-IP extraction. Both are **plumbing only**: no business logic, and they
must not import from `web`, `scanner`, or `services.arr`. Entrypoints — two
package surfaces: `from mediaman.services.infra import …`
(`SafeHTTPClient`, `resolve_safe_outbound_url`, `is_safe_outbound_url`,
`allowed_outbound_hosts`, `PINNED_EXTERNAL_HOSTS`, `get_setting` /
`get_string_setting` / `get_int_setting` / `get_bool_setting`, `get_media_path`,
`delete_path`, `get_aggregate_disk_usage`, `resolve_safe_readonly_path`, and the
error types `SafeHTTPError` / `SSRFRefused` / `DeletionRefused` /
`ConfigDecryptError`) and `from mediaman.services.rate_limit import …`
(`RateLimiter`, `ActionRateLimiter`, `get_client_ip`, `peer_is_trusted`,
`trusted_proxies`, `reset_all_limiters`). Each package's `__init__.py` `__all__`
is the import contract; the primary runtime entrypoint for outbound calls is
`SafeHTTPClient(base_url, allowed_hosts=…).get/post/put/delete()`.

## Key files

| File | Role |
|------|------|
| `src/mediaman/services/infra/__init__.py` | Public surface / barrel (CODE_GUIDELINES §1.7). Re-exports `SafeHTTPClient`, the settings readers, storage ops, path-safety helpers, and the `url_safety` guard; `__all__` is what callers import. |
| `src/mediaman/services/infra/url_safety.py` | Public SSRF API (`is_safe_outbound_url`, `resolve_safe_outbound_url`, `allowed_outbound_hosts`, `PINNED_EXTERNAL_HOSTS`, `SSRFRefused`) and allowlist composition. Owns the check ordering: DNS-free parse rejects → allowlist → resolver-touching IP checks (fail-fast before `getaddrinfo`). |
| `src/mediaman/services/infra/_url_safety_blocks.py` | Private deny-list constants + stateless predicates (all underscore-prefixed): `_METADATA_IPS` / `_METADATA_HOSTNAMES`, `_BLOCKED_V4_NETS` / `_BLOCKED_V6_NETS`, `_STRICT_BLOCKED_V4_NETS` / `_STRICT_BLOCKED_V6_NETS`, `_ALLOWED_DOCKER_HOSTNAMES`, `_ip_is_blocked`, `_resolve_all` (5 s-bounded `getaddrinfo` on a one-shot thread), `_normalise_host` (IDNA UTS-46), `_strict_egress_enabled`. |
| `src/mediaman/services/infra/http/client/_core.py` | `SafeHTTPClient` class + `get`/`post`/`put`/`delete` verb methods and the `_request` orchestration that threads each call through SSRF re-validation, DNS pin, no-redirects, split timeout, size cap and retry. |
| `src/mediaman/services/infra/http/client/_request.py` | Single-attempt transport (`_dispatch`); per-call SSRF / dispatch indirection resolved via `sys.modules` for test-monkeypatchability (`_resolve_outbound`, `_invoke_dispatch`); the timeout `(5, 30)` / size-cap 8 MiB (`_DEFAULT_MAX_BYTES`) / `User-Agent` defaults. |
| `src/mediaman/services/infra/http/client/_errors.py` | `SafeHTTPError` exception: stores the query-stripped `host/path` URL (credential-leak defence) plus status code and body snippet; `json_error()` parses the snippet. |
| `src/mediaman/services/infra/http/dns_pinning.py` | Process-global monkeypatch of `socket.getaddrinfo` installed at import (`_install_dns_pin_hook`); per-thread pin table on `threading.local`; `pin()` context manager (the only supported way to install a pin); `ensure_hook_installed()` re-verifies / re-installs per request. |
| `src/mediaman/services/infra/http/retry.py` | Retry / backoff engine (`dispatch_loop`): transport-error + retryable-status (429/502/503/504) handling, `Retry-After` parsing (delta + HTTP-date, capped 60 s), fixed vs full-jitter backoff, consecutive-5xx early abort. Re-attaches the buffered body onto `requests.Response._content` / `_content_consumed`. |
| `src/mediaman/services/infra/http/streaming.py` | `_read_capped`: size cap (`Content-Length` fast-fail + chunk-loop pre-check), content-type prefix validation, and unconditional rejection of non-identity `Content-Encoding` (gzip-bomb defence). Signals via private `_SizeCapExceeded` / `_ContentTypeMismatch`. |
| `src/mediaman/services/infra/storage/deletion.py` | `delete_path` (mandatory allowlist, absolute + strict-descendant target, no-symlink) and the private `_safe_rmtree` (re-resolve, same-device pin, `os.fwalk(follow_symlinks=False)`, per-entry device + symlink checks); `path_within_delete_roots` pure containment predicate. |
| `src/mediaman/services/infra/storage/_delete_roots.py` | `PathSafetyError` / `DeletionRefused` hierarchy, `_FORBIDDEN_ROOTS` frozenset, and pre-deletion allowlist validation incl. the atomic `O_NOFOLLOW | O_DIRECTORY` symlink check (`_check_symlink_via_nofollow`) that closes the TOCTOU window. |
| `src/mediaman/services/infra/storage/disk_usage.py` | Read-only queries: `get_aggregate_disk_usage` / `get_disk_usage_for_paths` (de-dup disks by the `(total, used, free)` byte tuple, **not** `st_dev`, for container bind-mount correctness) and `get_directory_size` (`os.walk(followlinks=False)`, `lstat`, regular files only). |
| `src/mediaman/services/infra/settings_reader.py` | Unified DB settings reader: `get_setting` (decrypt-then-`json.loads`), `get_int_setting` / `get_bool_setting` / `get_string_setting` coercers, `get_media_path`, `ConfigDecryptError`. Single home for the decrypt / JSON-unwrap pattern. |
| `src/mediaman/services/infra/path_safety.py` | `parse_delete_roots_env` (colon canonical, comma deprecated), `disk_usage_allowed_roots`, and `resolve_safe_readonly_path` (per-component symlink walk; **READ-ONLY callers only** — carries an accepted TOCTOU window). |
| `src/mediaman/services/rate_limit/__init__.py` | Public barrel: `RateLimiter`, `ActionRateLimiter`, `reset_all_limiters`, `get_client_ip`, `peer_is_trusted`, `trusted_proxies`. The FastAPI-coupled `rate_limit` decorator is deliberately **not** re-exported (lives in `web.middleware.rate_limit`). |
| `src/mediaman/services/rate_limit/limiters.py` | `ActionRateLimiter` (per-actor username key, burst window + sliding 24 h cap via `deque`) and `RateLimiter` (IP-bucketed /24-or-/64, `OrderedDict` LRU eviction at `_MAX_BUCKETS` = 10k). `WeakSet` registry (`_LIMITER_REGISTRY`) powers `reset_all_limiters` (test-only). Each limiter owns a `threading.Lock`. |
| `src/mediaman/services/rate_limit/instances.py` | Shared module-level limiter singletons (`NEWSLETTER_LIMITER`, `SETTINGS_WRITE_LIMITER`, `SETTINGS_TEST_LIMITER`, `SUBSCRIBER_WRITE_LIMITER`, `SCAN_TRIGGER_LIMITER`, `POSTER_PUBLIC_LIMITER`) so limits don't silently diverge across route modules. |
| `src/mediaman/services/rate_limit/ip_resolver.py` | `get_client_ip` trust hierarchy (peer-trust → `cf-connecting-ip` → XFF chain → `x-real-ip` → peer fallback); `_parse_proxy_env` (wildcard `*` → refuse-all, CRITICAL log); separate cached `MEDIAMAN_TRUSTED_PROXIES` and `MEDIAMAN_CLOUDFLARE_PROXIES` allowlists. |

## Invariants

- **The `__init__.py` barrel is the public surface** (CODE_GUIDELINES §1.7):
  callers import from `mediaman.services.infra`, never reach into sub-modules
  except at a site with an explicit `# rationale:` comment.
- **The SSRF deny-list is always on** — metadata IPs / hostnames, unspecified
  (wildcard), link-local, IPv6 ULA, Teredo, 6to4, multicast, broadcast, and
  CGNAT (`_ip_is_blocked`). Loopback (`127.0.0.0/8`, `::1`) and RFC1918 are
  **allowed by default** and refused **only** under `MEDIAMAN_STRICT_EGRESS=1`
  or `strict_egress=True` (the `_STRICT_BLOCKED_V4_NETS` /
  `_STRICT_BLOCKED_V6_NETS` sets).
- **A hostname that fails to resolve is refused** (fail-closed): a
  non-resolving name cannot be proven safe. Every returned address is checked;
  `getaddrinfo` is bounded to 5 s (`_RESOLVE_TIMEOUT_SECONDS`) on a one-shot thread.
- **IPv4-mapped-IPv6 addresses are unwrapped before all IP checks** in
  `_ip_is_blocked`, so `::ffff:169.254.169.254` hits the same rule path as the
  bare v4 form.
- **DNS pinning is the DNS-rebind defence**: the validated IP is pinned for the
  request's duration; `pin()` is the **only** supported way to install a pin.
  `socket.getaddrinfo` is monkeypatched process-globally at import and
  re-verified every request via `ensure_hook_installed()`.
- **`SafeHTTPClient` enforces six properties, in order**: (1) SSRF re-validation
  per call, (2) DNS pin, (3) `allow_redirects=False`, (4) split timeout
  (connect 5, read 30), (5) 8 MiB size cap, (6) retry only on idempotent methods
  — GET retries 429/5xx by default; POST/PUT/DELETE never retry unless
  `retry=True`.
- **Any non-identity `Content-Encoding` is rejected unconditionally on every
  read** (decompression-bomb defence); `SafeHTTPClient` sends
  `Accept-Encoding: identity`.
- **`delete_path` requires a non-empty `allowed_roots`**; the target must be
  absolute and a **strict** descendant of a validated root (never equal to a
  root); symlink targets are refused; deletion is same-device-pinned and walks
  with `os.fwalk(follow_symlinks=False)`.
- **`_FORBIDDEN_ROOTS` refuses configuring a delete root at any system dir**,
  including the macOS `/private/{tmp,var,etc}` resolved forms and mediaman's own
  `/media` and `/data` mounts.
- **`get_setting` returns the JSON-decoded type** (an all-digit credential comes
  back as `int`, `"true"` as `bool`); credential / string fields **must** be
  read via `get_string_setting`. An encrypted row with no `secret_key` raises
  `ConfigDecryptError` rather than silently returning the default.
- **Rate-limiter state lives in module-level singletons** (CODE_GUIDELINES §8.5
  / §1.12 single-worker invariant); per-request instantiation would discard the
  sliding window. Each limiter's own `threading.Lock` is the required lock.
  `ActionRateLimiter` uses a **sliding** 24 h window (not calendar-day) to close
  the midnight-boundary double-quota bug; `RateLimiter` caps at 10k buckets with
  O(1) LRU eviction and buckets IPv4 by /24, IPv6 by /64.
- **`cf-connecting-ip` is honoured ONLY when the peer is in
  `MEDIAMAN_CLOUDFLARE_PROXIES`** (deliberately separate from
  `MEDIAMAN_TRUSTED_PROXIES`). A literal `*` in either proxy env var causes
  refuse-all-proxies (logged CRITICAL) to prevent forged-header IP spoofing.
- **The `rate_limit` decorator is intentionally NOT part of this package**
  (FastAPI-coupled) — it lives in `mediaman.web.middleware.rate_limit`.

## Gotchas

- **The infra `__init__.py` docstring understates its dependencies.** It states
  "Allowed dependencies: Python standard library, `mediaman.crypto`", but the
  package also depends on third-party `requests`, `idna` and `cryptography`
  (`idna` is imported directly in `_url_safety_blocks.py` but is only a
  transitive dependency via `requests`, not a direct `pyproject.toml` entry),
  and on `mediaman.core.time` (`now_utc` in `retry.py`). Read the docstring as
  "no higher mediaman layers", not literally.
- **`resolve_safe_readonly_path` carries a documented, accepted TOCTOU window
  and is for READ-ONLY (stat-only) callers only** — the disk-usage endpoint is
  its sole caller. Destructive callers MUST use storage's fd-based `O_NOFOLLOW`
  validation (`_check_symlink_via_nofollow`) instead; do not reuse this helper
  for deletion.
- **Docker bridge hostnames** (`host.docker.internal`, `gateway.docker.internal`)
  are exempt from the `.internal` suffix block AND bypass the resolved-IP block,
  returning **no** pinned IP (re-resolved at request time). This exemption
  applies only in non-strict mode and does **not** skip the allowlist gate.
- **`retry.dispatch_loop` mutates `requests.Response` private attributes**
  (`_content`, `_content_consumed`) so `.json()` / `.text` work after the capped
  streamed read. This is pinned to `requests~=2.34` (`pyproject.toml`); widening
  the pin requires re-verifying those attrs (`test_json_after_capped_read`
  guards the contract).
- **`SafeHTTPClient` forces `Accept-Encoding: identity`.** An upstream that
  ignores it and gzips anyway will FAIL the streaming guard (Plex was confirmed
  to do this before the header was added).
- **`allowed_outbound_hosts` fails closed to the pinned-only set on ANY
  `sqlite3.Error`** while reading integration rows — a partial DB read never
  widens the allowlist. Integration URL settings are read as stored **plaintext**
  (not decrypted); a future schema that encrypts URL fields would need a
  `secret_key` parameter added.
- **`reset_all_limiters()` and each limiter's `reset()` are TEST-ONLY** helpers
  backed by the `_LIMITER_REGISTRY` `WeakSet`; they have zero production effect
  and must not be called on the hot path.
- **`get_bool_setting` reads the raw settings row directly** (not via
  `get_setting`) and treats only `false` / `0` / `no` / `off` (case-insensitive)
  as `False`; any other non-empty value returns `True`, and a missing / empty
  row returns the caller's `default` (which itself defaults to `True`).
- **An in-code line-count rationale comment has drifted.** `url_safety.py`'s
  `# rationale: 451 lines` block now sits on a 473-line file; the reasoning
  still holds but the cited number is stale. (`retry.py`'s `# rationale: 525 lines`
  matches its 525-line file and is **not** drifted.)

## Extension points

- **A new always-trusted external host** → add to `PINNED_EXTERNAL_HOSTS` in
  `url_safety.py`. This is a security change — review CODE_GUIDELINES §10.6 first.
- **A new configured-integration URL key** in the allowlist →
  `_INTEGRATION_URL_SETTING_KEYS` in `url_safety.py`.
- **A new deny-list network / range** → `_BLOCKED_V4_NETS` / `_BLOCKED_V6_NETS`
  (always-on) or `_STRICT_BLOCKED_V4_NETS` / `_STRICT_BLOCKED_V6_NETS`
  (strict-only) in `_url_safety_blocks.py`.
- **A new forbidden delete-root** → `_FORBIDDEN_ROOTS` in `_delete_roots.py`.
- **A new shared limiter** → add a singleton to `instances.py` (never
  instantiate per-request); tune existing caps there so they don't diverge.
- **A new typed setting coercer** → extend `settings_reader.py` alongside
  `get_int_setting` / `get_bool_setting` / `get_string_setting`.
- **Retry-policy knobs** (jitter strategy, early-abort on consecutive 5xx,
  retryable-status override) are already parameters on `dispatch_loop` and the
  `SafeHTTPClient` verb methods — thread them through, don't fork the engine.

## Related

- Law: [`CODE_GUIDELINES.md`](../../../CODE_GUIDELINES.md) — §1.7 (the barrel is
  the public surface, enforced here), §8.5 / §1.12 (module-level globals + the
  single-worker lock invariant the limiters rely on), and §10.6 (the SSRF
  allowlist for outbound this package implements).
- Consumers (depend on this package; must never be imported *by* it):
  `mediaman.scanner`, `mediaman.services.arr`, `mediaman.services.downloads`,
  `mediaman.services.mail`, `mediaman.services.media_meta`,
  `mediaman.services.openai`, `mediaman.web`. The FastAPI `rate_limit` decorator
  lives in `mediaman.web.middleware.rate_limit`.
- Decisions: none recorded yet.
- Specs: none recorded yet.
