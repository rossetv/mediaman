<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: services-mail

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

Outbound email for mediaman, split across two concerns. `mailgun.py` is a low-level
Mailgun HTTP API wrapper with transparent EU/US region fallback and RFC-2822
header-injection defence. The `newsletter/` package assembles a per-subscriber weekly
digest (scheduled-deletion, recently-deleted, disk/reclaimed-space, and AI
recommendation cards), renders a Jinja2 HTML email with signed per-recipient tokens,
dispatches via Mailgun, and reconciles `scheduled_actions.notified` against actual
per-subscriber delivery. Public API is `send_newsletter(conn, secret_key, dry_run=False,
grace_days=14, *, recipients=None, mark_notified=True)`; the transport is
`MailgunClient(domain, api_key, from_address, region="eu")` with `.send(to=, subject=,
html=)` / `.is_reachable()`. Dispatch runs inside the scanner pipeline, never the
request layer.

## Key files

| File | Role |
|------|------|
| `src/mediaman/services/mail/mailgun.py` | `MailgunClient`: EU/US region fallback (`_other_base`), per-send recipient + header validation (`_validate_header_value`), POST retry via `SafeHTTPClient` with a 500-also-retryable override (`_RETRYABLE_POST_STATUSES`) and abort after 2 consecutive 5xx (`_CONSECUTIVE_5XX_ABORT`). |
| `src/mediaman/services/mail/newsletter/__init__.py` | Thin aggregator + orchestrator: `send_newsletter`, `NewsletterConfigError`, the `MailgunSettings`/`SendContext` dataclasses, `_load_mailgun_settings` (base_url scheme guard; all-missing = silent skip vs some-missing = raise), `_build_send_context`, `_has_content_to_report`. |
| `src/mediaman/services/mail/newsletter/subscribers.py` | Subscriber resolution (`_load_subscribers`: `None` = skip vs `[]` = send to nobody) and the per-subscriber render/send loop (`_send_to_subscribers`). Mints per-recipient unsubscribe + download/redownload tokens on shallow item copies (`_render_for_subscriber`, `_mint_deleted_tokens`, `_mint_rec_tokens`); records one `newsletter_deliveries` row per (action, subscriber), best-effort (`_record_delivery_attempt`). |
| `src/mediaman/services/mail/newsletter/summary.py` | Disk-usage/by-type stats + reclaimed week/month/total totals (`_load_storage_stats` → `StorageSummary`), recently-deleted cards (`_load_deleted_items`: 7-day window, `LIMIT 10`, excludes items re-downloaded after deletion via `_build_redownload_index`, batched `tmdb_id` lookup from `suggestions`), recommendation-batch loader (`_load_recommendations`). |
| `src/mediaman/services/mail/newsletter/schedule.py` | `_load_scheduled_items` (mode-dependent `WHERE`: `notified=0` vs `token_used=0`; oldest-first sort, corrupt timestamps last) and `_mark_notified` (flips `notified=1` only for action ids whose delivered-subscriber set is a superset of the active recipients). |
| `src/mediaman/services/mail/newsletter/enrich.py` | `_annotate_rec_download_states`: builds Radarr/Sonarr caches (degrades to an empty cache on Arr failure), stamps `download_state` on recommendation cards that carry a `tmdb_id`. |
| `src/mediaman/services/mail/newsletter/render.py` | Lazy thread-safe (double-checked lock) shared Jinja2 `Environment` with `autoescape=True` (`_get_jinja_env`, guarded by `_JINJA_ENV_LOCK`); `_build_subject` formats the size-to-reclaim subject line with a `[DRY RUN]` prefix. |
| `src/mediaman/services/mail/newsletter/_types.py` | TypedDicts: `ScheduledNewsletterItem`, `DeletedNewsletterItem`, `NewsletterRecItem`, `StorageStats`. `NotRequired` fields are stamped by later stages (enrich / render loop). |
| `src/mediaman/services/mail/newsletter/_time.py` | `_parse_days_ago` (ISO → days-before-now; `None` on empty/unparseable, with a warning), shared by `schedule.py` and `summary.py`. |
| `src/mediaman/services/mail/newsletter/templates/newsletter.html` | Jinja2 email template. Inline colours only (email clients strip `<link>` / CSS custom properties); deliberately NOT wired to `static/css/_tokens.css` — kept in sync with DESIGN.md §2 (Color Palette & Roles) by hand. |

## Invariants

- **Header-injection defence fails closed.** `from_address` is validated for CR/LF/NUL at `MailgunClient` construction; `subject` and recipient are re-validated on every `.send` (`_validate_header_value`, `_validate_recipient`) even though routes may validate at ingress.
- **Region-fallback semantics** (`MailgunClient.send`): a 404 means the domain lives in the other region — retry the alternate base and remember it; a 401 means a bad API key — never retry (it would only confuse the log). The working base is cached on the instance (`self._base`).
- **`scheduled_actions.notified` flips to 1 only when EVERY active subscriber was delivered.** `_mark_notified` runs a superset check against `newsletter_deliveries` rows with `sent_at IS NOT NULL`. Any partial failure leaves the row at `notified=0` for re-attempt on the next scan tick.
- **Per-subscriber isolation.** Each subscriber gets shallow-copied deleted/recommendation item dicts before token minting (`_render_for_subscriber`, `dict(item)` copies), so per-recipient download URLs and unsubscribe tokens never bleed between subscribers.
- **`base_url` must start with `http://` or `https://`.** Any other scheme raises `NewsletterConfigError` (`_load_mailgun_settings`), preventing `javascript:`/`data:` links in emails.
- **The unsubscribe email is carried inside the signed token, never as a query parameter** (`_render_for_subscriber`, `generate_unsubscribe_token`), so subscriber PII never lands in server access logs.
- **All four Mailgun settings missing → silent DEBUG skip** (early return); a partial subset missing → `NewsletterConfigError` (admin must fix, do not auto-retry) — `_load_mailgun_settings`.
- **`now_utc()` is computed once in `send_newsletter`** and threaded through every section (the `now` parameter) so cards cannot straddle midnight and report inconsistent ages.
- **A re-download button/URL is minted only when a stable `tmdb_id` resolves** (`_mint_deleted_tokens`); without one the template's `{% if item.redownload_url %}` guard hides the button rather than enqueue a wrong title via title-only lookup.
- **Delivery is best-effort and never aborts the scan.** Per-subscriber send failures and delivery-record write failures are logged and swallowed (`_send_to_subscribers`, `_record_delivery_attempt`); the scanner also wraps `_send_newsletter` in try/except.
- **`_action_id` is an internal handle** for the `notified` bookkeeping and must never be rendered into outbound email HTML (absent from `newsletter.html`; documented on `ScheduledNewsletterItem`).
- **No imports from `mediaman.web`** (forbidden by design; stated in the `mail/__init__.py` package docstring) — dispatch runs inside the scanner pipeline, not the request layer.

## Gotchas

- **STALE DOCSTRING (defect):** the `newsletter/__init__.py` module docstring says callers import `send_newsletter`/`NewsletterConfigError` from `mediaman.services.newsletter` — that path does not exist. The real import path is `mediaman.services.mail.newsletter` (no shim module; every real caller uses the `mail.newsletter` path).
- The newsletter always constructs `MailgunClient` with the default region (`"eu"`) — `_build_send_context` passes only domain/api_key/from_address, so region selection relies entirely on the 404 fallback, not on a configured region.
- `newsletter_deliveries` and its SQL use the column name `recipient` — a legacy name kept because it is half the composite `PRIMARY KEY (scheduled_action_id, recipient)`, to avoid a destructive migration. Noted in-code so it is not "fixed".
- `_load_subscribers` distinguishes `None` (no explicit recipients → query subscribers; a `None` return means skip quietly) from `[]` (explicit empty recipients → send to nobody). The `recipients is not None` check is load-bearing (F-07).
- `_load_recommendations` is called with `check_enabled=False` from the send path because `send_newsletter` has already queried `suggestions_enabled` (F-12) — do not add a `suggestions_enabled` check inside it or you re-introduce the duplicate query.
- The f-string placeholder interpolation in `_mark_notified`, `_record_delivery_attempt`, and `_load_deleted_items` is injection-safe ONLY because the interpolated fragments are placeholder lists (`?`, `(?,?)`) sized to validated ints / DB-sourced tuples — never raw user input.
- Scheduled-item selection differs by call mode: automated (`mark_notified=True`) loads `notified=0` rows; manual (`mark_notified=False`) loads `token_used=0` rows — the two paths intentionally see different item sets.
- The email template intentionally hardcodes colours and ignores the design-token CSS; changing DESIGN.md colours requires a manual edit here (email clients strip external / custom-property CSS). See DESIGN.md §2.
- `media_items` has no `tmdb_id` column; deleted-item `tmdb_id`s are resolved by a `(title, media_type)` join against the `suggestions` table, so items downloaded outside the recommendation flow get no re-download button.
- Jinja2 is an optional import in `render.py` (`ImportError` → `Environment = None`); `_get_jinja_env` then returns `None` and `_build_send_context` has a `pragma: no cover` fallback that rebuilds an `Environment` inline.

## Extension points

- **New digest section:** add a `_load_*` loader (in `summary.py` or `schedule.py`), a TypedDict in `_types.py`, and a template block in `newsletter.html`, then wire it into `_build_send_context` and the `_send_to_subscribers` render call.
- **Direct transport use:** build `MailgunClient(domain, api_key, from_address, region=...)` — as `downloads/notifications.py` and `web/routes/settings/testers.py` do — and call `.send(to=, subject=, html=)` / `.is_reachable()`.
- **Configured region:** today only the 404 fallback selects the region; to honour a stored region, thread a `region` argument through the `MailgunClient(...)` construction in `_build_send_context`.
- **Arr download-state annotation** lives solely in `enrich.py` (`_annotate_rec_download_states`); new state sources plug in there.

## Related

- Consumed by: `mediaman.scanner.engine` (automated post-scan, default args → all active subscribers, marks notified); `mediaman.web.routes.subscribers` (`api_send_newsletter`: manual, `recipients=[...]`, `mark_notified=False` → all pending items, no flag write).
- Direct `MailgunClient` builders: `mediaman.services.downloads.notifications`, `mediaman.web.routes.settings.testers`.
- Consumes: `mediaman.services.infra` (`SafeHTTPClient`, `SafeHTTPError`, `get_string_setting`, `get_aggregate_disk_usage`, `get_media_path`; `SafeHTTPClient.post` supports `jitter_strategy` / `abort_after_consecutive_5xx` / `retryable_statuses` overrides); `mediaman.crypto` (`sign_poster_url`, `generate_download_token`, `generate_unsubscribe_token`); `mediaman.core` (`email_validation.validate_email_address`, `time.now_utc`/`now_iso`/`parse_iso_strict_utc`, `format.format_day_month`/`rk_from_audit_detail`/`title_from_audit_detail`); `mediaman.services.arr` (`build_radarr_from_db`, `build_sonarr_from_db`, `build_radarr_cache`, `build_sonarr_cache`, `compute_download_state`, `ArrError` — `enrich.py` only); `requests` / `RequestException`; `jinja2` (optional).
- Modules: [services-infra](services-infra.md) (`SafeHTTPClient`, settings, disk usage), [services-arr](services-arr.md) (`enrich.py` download-state caches), [services-downloads](services-downloads.md) (shares `MailgunClient`).
- SQLite tables: reads `scheduled_actions`, `media_items`, `subscribers`, `audit_log`, `suggestions`, `settings`; reads/writes `newsletter_deliveries`; writes `scheduled_actions.notified`.
- Decisions: none yet.
- Specs: none yet.
