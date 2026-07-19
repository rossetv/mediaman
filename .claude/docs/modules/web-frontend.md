<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: web-frontend

<!-- One concern per file — split when a second concern appears; line count is
never the trigger. -->

## Purpose

Server-rendered UI layer for Mediaman: Jinja2 HTML templates plus a
dependency-free vanilla-JS/CSS static bundle. Every page is a full HTML
document (no SPA router); dynamic areas (downloads, recommended, search,
settings) are progressively enhanced by page-specific JS that polls JSON APIs
and patches the DOM in place. The look is a cinematic dark theme governed by the
repo's `DESIGN.md`, expressed through CSS custom-property tokens and a single
Jinja macro library.

**Entry points.** `base.html` is the inheritance root every page template
extends; route handlers under `src/mediaman/web/routes/` (outside this module)
call `request.app.state.templates.TemplateResponse(...)` to render them.
`base.html` loads the `MM.*` core JS on every page; each page template then
loads its own scripts, which self-register on `window.MM` and are wired by a
thin bootstrap. Static assets are served from the `/static` mount configured in
`src/mediaman/app_factory.py` (`_STATIC_DIR` / `_TEMPLATE_DIR`).

## Key files

| File | Role |
|------|------|
| `src/mediaman/web/templates/base.html` | Root layout every page extends. Declares the ordered CSS fragment links and the globally-loaded, `defer`'d core JS (all external — the strict CSP forbids executable inline script). Provides the skip-link, `<main id="main-content">` landmark, `body[data-page]=nav_active`, and the `{% block title/head/nav/content %}` extension points. |
| `src/mediaman/web/templates/_components.html` | Jinja macro library — single source of truth for UI primitives: `icon`, `btn`, `pill`, `fpill`, `countdown`, `poster`, `tile`, `tog`, `inp`, `conn`, `storage`, `empty`, `eyebrow`, `sec_hd`, plus the settings-v2 primitives (`setg_rail_item`, `setg_block`, `setg_card`, `setg_row`, `inp_secret`, `inp_select`, `fld`, `conn_pill`, `intg_card`, `service_card`, `abandon_btn`). Carries an explicit XSS-boundary contract. |
| `src/mediaman/web/templates/_nav.html` | Three-surface responsive nav: `.nav-glass` desktop top rail (≥700px), `.nav-topbar` mobile brand+title, `.nav-tabs` mobile bottom bar + `.nav-more-sheet` overflow drawer. Active item keyed off `nav_active` (`dashboard`\|`library`\|`search`\|`recommended`\|`downloads`\|`history`\|`settings`). Logout is a POST form to `/api/auth/logout`. |
| `src/mediaman/web/static/css/_tokens.css` | Design tokens: surface elevation scale (`--bg`,`--s1`..`--s4`,`--hair`), text opacities (`--t1`..`--t4`), accents, semantic chip colours, radii, the single allowed `--shadow-card`, spacing, `--nav-h`, the named z-index scale (`--z-dropdown`..`--z-toast`), semantic rgba background helpers, and third-party brand colours (`--brand-*`). `DESIGN.md` is the referenced law. |
| `src/mediaman/web/static/css/_base.css` | Reset + layout primitives. Documents the canonical horizontal-row inventory (`.setg-row` / `.settings-subrow` / `.form-row` — "do not create a fourth variant") and a deliberately narrow flex-utility set. Sets the global type scale and the `img[data-broken]` fallback hook. |
| `src/mediaman/web/static/js/core/api.js` | `MM.api` — centralised fetch wrapper (`get`/`post`/`put`/`patch`/`delete`/`postForm`) returning parsed-JSON promises; `MM.api.APIError` carries `.error`/`.message`/`.status`/`.issues`/`.data`. Cookie auth via `credentials:'same-origin'`; honours both HTTP ≥400 and the codebase's `{"ok":false}` envelope; supports `AbortController` signals. |
| `src/mediaman/web/static/js/core/dom.js` | `MM.dom` — `q`/`qa`/`setText`/`findByAttr`/`on`/`delegate` helpers. No cross-module calls, load-order independent. |
| `src/mediaman/web/static/js/core/modal.js` | `MM.modal.setupDetail(modalEl, opts)` — reusable modal open/close lifecycle: backdrop-click, ESC (via `ModalA11y` or a self-removing fallback listener), body-overflow lock, `[data-close-modal]` wiring, `ModalA11y` registration. |
| `src/mediaman/web/static/js/core/tiles.js` | `MM.tiles.render` — poster-card grid renderer. All DOM built via `createElement`/`textContent` (never `innerHTML`), so it is XSS-safe with untrusted API fields. Clickable cards get `role=button`, `tabindex=0`, and Enter/Space activation. |
| `src/mediaman/web/static/js/core/reauth.js` | `MM.reauth` — centred password re-authentication modal and `MM.reauth.run(fn)` wrapper that opens it when an `APIError` has `status 403` + `data.reauth_required`, POSTs `/api/auth/reauth`, and retries the original request once. Consumed by the settings savebar/users. |
| `src/mediaman/web/static/js/modal-a11y.js` | `ModalA11y` global — Tab focus-trap, ESC-to-close via a registered `closeFn`, initial focus, and focus-restore to the trigger. Maintains an open-stack so only the topmost modal handles keys; delegates `[data-close-modal]` clicks. |
| `src/mediaman/web/static/js/ui-feedback.js` | `window.UIFeedback` — styled `toast()` and `confirm()` replacements for native `alert()`/`confirm()`; `confirm()` integrates with `ModalA11y` and returns a `Promise<boolean>`. |
| `src/mediaman/web/static/js/broken-poster.js` | Document-capture `error` listener that stamps `dataset.broken=''` on `img[data-broken-on-error]` — the CSP-safe replacement for the old inline `onerror=` attribute. Loaded globally in `base.html`. |
| `src/mediaman/web/static/js/downloads/poll.js` | Representative poll-loop pattern: `MM.downloads.poll.start(onData)` — 2s cadence (`POLL_MS`), exponential backoff capped at 30000ms on failure, pauses on `document.hidden`, resumes on `visibilitychange`, listens for a `mediaman:poll:now` event, and redirects to `/login` on 401/403. Recommended has a parallel poll module. |
| `src/mediaman/web/templates/settings/_sec_integrations.html` | Settings section rendered via the `service_card` macro for all 8 third-party integrations (Plex, Sonarr, Radarr, NZBGet, TMDB, OMDb, OpenAI, Mailgun) — the macro-driven pattern the rest of the `settings/_sec_*.html` partials follow. |

## Invariants

- **Strict CSP forbids page-level inline script.** `_build_csp()` in `src/mediaman/web/middleware/security_headers.py` sets `script-src 'self' 'nonce-{nonce}'` with **no** `'unsafe-inline'`. Every executable page script must be an external file under `static/js/`. Inline `<script type="application/json">` data islands ARE allowed (non-executable, not gated by `script-src`) and are the sanctioned way to pass server data to JS (settings/download/recommended/force_password_change bootstraps).
- **`style-src` + `style-src-attr` split.** `style-src` carries the nonce; because a Chromium quirk disables the implicit inline-style-attribute allowance once a nonce is present, a **separate** `style-src-attr 'unsafe-inline'` directive is required and present so scattered inline `style="display:none"` attributes and modal style writes keep working.
- **One global namespace, no build step.** All client JS is dependency-free vanilla, namespaced under `window.MM` (`MM.api`, `MM.dom`, `MM.modal`, `MM.tiles`, `MM.reauth`, `MM.downloads`, `MM.search`, `MM.settings`) — no framework, no bundler. Modules are IIFEs that self-register; sub-modules must register before the page bootstrap wires them, so `<script>` load order in the template is load-bearing.
- **Untrusted data never reaches `innerHTML`.** API-derived data is built with `createElement` + `textContent` (`tiles.js`, `ui-feedback.js`). Jinja autoescape is on for `.html`; macro slots piped through `|safe` (`icon`, `pills_html`, `sub`, `header_actions`, `poster_overlay_html`) are documented as **server-controlled literals only** — a stored-XSS boundary, not a convenience. `label`/`value` slots stay auto-escaped.
- **Every full-page template extends `base.html`** and sets `nav_active` for nav highlighting; `_nav.html`, `_detail_modal.html`, etc. are included partials — a leading-underscore filename marks a partial/macro file.
- **Poll loops share one contract:** fixed cadence, exponential backoff on failure, pause when the tab is hidden, and redirect to `/login` on 401/403.
- **Styling is centralised.** `_components.html` macros plus the CSS token/primitive files are the single source of truth; `DESIGN.md` at the repo root is the visual law and `CODE_GUIDELINES.md` the code law (both human-owned, read-only).

## Gotchas

- **Static-URL styles diverge.** `downloads.html` references its scripts via `{{ url_for('static', path='…') }}`, while every other page hardcodes literal `/static/…` paths. Both resolve to the same mount; the inconsistency is cosmetic.
- **Token-name inversion (documented in `_tokens.css`).** `--accent` is the link blue `#2997ff` and `--accent-cta` is the CTA/focus blue `#0071e3` — flipped from `DESIGN.md` §3's naming. Intentional (a full rename would cascade repo-wide); don't "fix" it.
- **`c.btn` auto-escapes `data_attrs` with `| e`,** which corrupts `| tojson`-encoded payloads that JS reads back via `JSON.parse`. `dashboard.html` (re-download / keep buttons) and `library.html` therefore keep those buttons as inline HTML rather than using the macro; inline TODOs flag this as pending macro work.
- **`c.btn` exposes no `aria-haspopup`/`aria-expanded`,** so dropdown-trigger carets (the dashboard snooze menu) are written inline — another documented macro gap.
- **Scattered inline `style="display:none"`** remains across templates (`base.html`'s comment calls migrating them to CSS classes "the final cleanup step"); they work only because of the `style-src-attr 'unsafe-inline'` directive above.
- **Mixed JS dialect.** Most modules are ES5 (`var`, `function`) for the stated old-browser posture, but a few newer bootstraps use ES6 (`search.js` uses arrow functions/`const`/destructuring). No transpile step, so this relies on modern-browser support.
- **`src/mediaman/web/static/js/core/_self_test.html`** is a dev-only offline smoke test for the `MM` core namespace and is explicitly NOT shipped as part of any page.
- **FontAwesome is fully self-hosted** under `static/fonts/fontawesome` (CSS + web fonts + `LICENSE.txt`) — the only vendored front-end asset. The strict CSP already serves it: `_build_csp()` declares no named `font-src` directive, so the web fonts fall through to the `default-src 'self'` fallback (same-origin, permitted); no CSP change is needed for it.
- **The dashboard Storage block is bespoke inline markup** (not the `c.storage` macro) because the macro's `{color, value}` legend schema doesn't fit the nested-`<small>` number + CSS-classed legend dots — flagged with a TODO to extend the macro.

## Extension points

- **New page:** create a template that extends `base.html`, set `nav_active`, and load page-specific scripts that self-register on `window.MM`; the route handler renders it via `app.state.templates.TemplateResponse(...)`.
- **New UI primitive:** add a macro to `_components.html` (respect the `|safe` server-controlled-literal contract) rather than hand-rolling markup; new tokens go in `_tokens.css` under the `DESIGN.md` scheme.
- **New third-party integration:** add a `c.service_card(...)` call to `settings/_sec_integrations.html` — the same macro pattern the other `settings/_sec_*.html` partials use.
- **New polled/dynamic area:** follow the `downloads/poll.js` contract (fixed cadence, exponential backoff, pause on hidden, redirect to `/login` on 401/403) and pass server data through an inline `<script type="application/json">` island, never inline executable script.

## Related

- Route handlers under `src/mediaman/web/routes/` render these templates and back the JSON APIs the JS polls (`/api/downloads`, `/api/auth/*`, `/api/poster/*`).
- CSP/nonce owner: `src/mediaman/web/middleware/security_headers.py` (`_build_csp`) — the constraint the whole front-end is built around.
- App wiring: `src/mediaman/app_factory.py` (`_STATIC_DIR`, `_TEMPLATE_DIR`, `app.state.templates`, `/static` mount).
- Law (human-owned, read-only): `DESIGN.md` (visual system) and `CODE_GUIDELINES.md` (code) at the repo root.
