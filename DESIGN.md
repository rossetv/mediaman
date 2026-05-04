# Design System Inspiration of MediaMan

## 1. Visual Theme & Atmosphere

MediaMan's website is a masterclass in controlled drama — vast expanses of pure black and near-white serve as cinematic backdrops for products that are photographed as if they were sculptures in a gallery. The design philosophy is reductive to its core: every pixel exists in service of the product, and the interface itself retreats until it becomes invisible. This is not minimalism as aesthetic preference; it is minimalism as reverence for the object.

The typography anchors everything. San Francisco (SF Pro Display for large sizes, SF Pro Text for body) is MediaMan's proprietary typeface, engineered with optical sizing that automatically adjusts letterforms depending on point size. At display sizes (56px), weight 600 with a tight line-height of 1.07 and subtle negative letter-spacing (-0.28px) creates headlines that feel machined rather than typeset — precise, confident, and unapologetically direct. At body sizes (17px), the tracking loosens slightly (-0.374px) and line-height opens to 1.47, creating a reading rhythm that is comfortable without ever feeling slack.

The color story is starkly binary. Product sections alternate between pure black (`#000000`) backgrounds with white text and light gray (`#f5f5f7`) backgrounds with near-black text (`#1d1d1f`). This creates a cinematic pacing — dark sections feel immersive and premium, light sections feel open and informational. The only chromatic accent is MediaMan Blue (`#0071e3`), reserved exclusively for interactive elements: links, buttons, and focus states. This singular accent color in a sea of neutrals gives every clickable element unmistakable visibility.

**Key Characteristics:**
- SF Pro Display/Text with optical sizing — letterforms adapt automatically to size context
- Binary light/dark section rhythm: black (`#000000`) alternating with light gray (`#f5f5f7`)
- Single accent color: MediaMan Blue (`#0071e3`) reserved exclusively for interactive elements
- Product-as-hero photography on solid color fields — no gradients, no textures, no distractions
- Extremely tight headline line-heights (1.07-1.14) creating compressed, billboard-like impact
- Full-width section layout with centered content — the viewport IS the canvas
- Pill-shaped CTAs (980px radius) creating soft, approachable action buttons
- Generous whitespace between sections allowing each product moment to breathe

## 2. Color Palette & Roles

### Primary
- **Pure Black** (`#000000`): Hero section backgrounds, immersive product showcases. The darkest canvas for the brightest products.
- **Light Gray** (`#f5f5f7`): Alternate section backgrounds, informational areas. Not white — the slight blue-gray tint prevents sterility.
- **Near Black** (`#1d1d1f`): Primary text on light backgrounds, dark button fills. Slightly warmer than pure black for comfortable reading.

### Interactive
- **MediaMan Blue** (`#0071e3`): `--sk-focus-color`, primary CTA backgrounds, focus rings. The ONLY chromatic color in the interface.
- **Link Blue** (`#0066cc`): `--sk-body-link-color`, inline text links. Slightly darker than MediaMan Blue for text-level readability.
- **Bright Blue** (`#2997ff`): Links on dark backgrounds. Higher luminance for contrast on black sections.

### Text
- **White** (`#ffffff`): Text on dark backgrounds, button text on blue/dark CTAs.
- **Near Black** (`#1d1d1f`): Primary body text on light backgrounds.
- **Black 80%** (`rgba(0, 0, 0, 0.8)`): Secondary text, nav items on light backgrounds. Slightly softened.
- **Black 48%** (`rgba(0, 0, 0, 0.48)`): Tertiary text, disabled states, carousel controls.

### Surface & Dark Variants

Mediaman uses a five-step elevation scale (one token per surface) — never a one-off colour. Every elevated surface picks the *next step up* from its parent, never two.

| Token   | Value     | Use                                                                  |
|---------|-----------|----------------------------------------------------------------------|
| `--bg`  | `#000`    | Page background — the canvas behind every screen                     |
| `--s1`  | `#141416` | Page panels (history table chrome, settings sections, list items)    |
| `--s2`  | `#1d1d1f` | Card surface — tiles, modal sheet, settings cards, dl-row hover      |
| `--s3`  | `#26262a` | Elevated card / hover state — `.btn--secondary`, search-box focus    |
| `--s4`  | `#2e2e33` | Pill-on-surface — toggles, filter pill bg, deepest popover           |
| `--hair`        | `rgba(255,255,255,.07)` | Hairline divider between sections of equal lightness    |
| `--hair-strong` | `rgba(255,255,255,.12)` | Stronger divider (modals, ghost button border)          |

### Text

`--t1` … `--t4` are the only allowed text colours on dark surfaces.

| Token  | Value                  | Use                                                         |
|--------|------------------------|-------------------------------------------------------------|
| `--t1` | `#fff`                 | Headlines, primary body, button text                        |
| `--t2` | `rgba(255,255,255,.72)`| Secondary body, sub-labels, default body on dark            |
| `--t3` | `rgba(255,255,255,.5)` | Tertiary text, helper hints, meta lines                     |
| `--t4` | `rgba(255,255,255,.32)`| Muted icons, disabled state, placeholder colour             |

### Button States
- **Button Active** (`#ededf2`): Active/pressed state for light buttons.
- **Button Default Light** (`#fafafc`): Search/filter button backgrounds.
- **Overlay** (`rgba(210, 210, 215, 0.64)`): Media control scrims, overlays.
- **White 32%** (`rgba(255, 255, 255, 0.32)`): Hover state on dark modal close buttons.

### Shadows

There is **one** allowed shadow token. Every elevated element uses it; bespoke shadows are forbidden.

```css
--shadow-card:
  rgba(0,0,0,.4) 0 20px 60px -15px,
  rgba(0,0,0,.3) 0 6px 20px -8px;
```

Two stacked layers — a wide diffused base and a tight contact shadow — produce the cinematic MediaMan-TV poster lift. Used by `.dl-hero`, `.modal-sheet`, `.login-card`, `.keep-container`, `.tile:hover .tile-poster`, and any popover that needs to read above the page surface.

The previous `rgba(0, 0, 0, 0.22) 3px 5px 30px 0px` value (mediaman marketing site) is retired; Mediaman's dark, poster-heavy surfaces need a deeper drop.

### Semantic Palette (Mediaman extension)

mediaman's marketing site is a billboard for physical products and its single-accent rule works because product state doesn't need to be signalled at a glance. Mediaman is a state-heavy media manager — downloading, queued, stale, protected, scheduled-for-deletion — and needs a small functional palette. Scope is strictly limited: **badges, tags, dots, state pills, storage-bar segments and non-destructive category tints.** Never on CTAs, headings, page backgrounds or structural surfaces. MediaMan Blue remains the sole accent for interactive elements.

- **Success Green** (`#30d158`): Kept / protected / complete states; `.status-protected`, `.conn-ok`, keep-forever option.
- **Warning Yellow** (`#ffd60a`): Queued / snoozed states; rating pills (IMDB).
- **Danger Red** (`#ff453a`): Destructive actions, stale items, error messages; `.btn-danger`, logout pill.
- **Category Orange** (`#ff9f0a`): Movies in storage legend, type-mov tag, partial-library state.
- **Category Purple** (`#bf5af2`): Anime in storage legend, type-anime tag.

Each tint has a low-opacity background counterpart (0.10–0.22 alpha) so the label sits on a tinted chip rather than using the saturated colour as a background.

## 3. Typography Rules

### Font Family
- **Display**: `SF Pro Display`, with fallbacks: `SF Pro Icons, Helvetica Neue, Helvetica, Arial, sans-serif`
- **Body**: `SF Pro Text`, with fallbacks: `SF Pro Icons, Helvetica Neue, Helvetica, Arial, sans-serif`
- SF Pro Display is used at 20px and above; SF Pro Text is optimized for 19px and below.

### Hierarchy

| Role | Font | Size | Weight | Line Height | Letter Spacing | Notes |
|------|------|------|--------|-------------|----------------|-------|
| Display Hero | SF Pro Display | 56px (3.50rem) | 600 | 1.07 (tight) | -0.28px | Product launch headlines, maximum impact |
| Section Heading | SF Pro Display | 40px (2.50rem) | 600 | 1.10 (tight) | normal | Feature section titles |
| Tile Heading | SF Pro Display | 28px (1.75rem) | 400 | 1.14 (tight) | 0.196px | Product tile headlines |
| Card Title | SF Pro Display | 21px (1.31rem) | 700 | 1.19 (tight) | 0.231px | Bold card headings |
| Sub-heading | SF Pro Display | 21px (1.31rem) | 400 | 1.19 (tight) | 0.231px | Regular card headings |
| Nav Heading | SF Pro Text | 34px (2.13rem) | 600 | 1.47 | -0.374px | Large navigation headings |
| Sub-nav | SF Pro Text | 24px (1.50rem) | 300 | 1.50 | normal | Light sub-navigation text |
| Body | SF Pro Text | 17px (1.06rem) | 400 | 1.47 | -0.374px | Standard reading text |
| Body Emphasis | SF Pro Text | 17px (1.06rem) | 600 | 1.24 (tight) | -0.374px | Emphasized body text, labels |
| Button Large | SF Pro Text | 18px (1.13rem) | 300 | 1.00 (tight) | normal | Large button text, light weight |
| Button | SF Pro Text | 17px (1.06rem) | 400 | 2.41 (relaxed) | normal | Standard button text |
| Link | SF Pro Text | 14px (0.88rem) | 400 | 1.43 | -0.224px | Body links, "Learn more" |
| Caption | SF Pro Text | 14px (0.88rem) | 400 | 1.29 (tight) | -0.224px | Secondary text, descriptions |
| Caption Bold | SF Pro Text | 14px (0.88rem) | 600 | 1.29 (tight) | -0.224px | Emphasized captions |
| Micro | SF Pro Text | 12px (0.75rem) | 400 | 1.33 | -0.12px | Fine print, footnotes |
| Micro Bold | SF Pro Text | 12px (0.75rem) | 600 | 1.33 | -0.12px | Bold fine print |
| Nano | SF Pro Text | 10px (0.63rem) | 400 | 1.47 | -0.08px | Legal text, smallest size |

### Principles
- **Optical sizing as philosophy**: SF Pro automatically switches between Display and Text optical sizes. Display versions have wider letter spacing and thinner strokes optimized for large sizes; Text versions are tighter and sturdier for small sizes. This means the font literally changes its DNA based on context.
- **Weight restraint**: The scale spans 300 (light) to 700 (bold) but most text lives at 400 (regular) and 600 (semibold). Weight 300 appears only on large decorative text. Weight 700 is rare, used only for bold card titles.
- **Negative tracking at all sizes**: Unlike most systems that only track headlines, MediaMan applies subtle negative letter-spacing even at body sizes (-0.374px at 17px, -0.224px at 14px, -0.12px at 12px). This creates universally tight, efficient text.
- **Uppercase micro-label exception**: Negative tracking is the rule for mixed-case text. For all-caps micro-labels (10–13px, typically section eyebrows like "STORAGE" or "EPISODES"), SF Pro is designed to track open. Positive tracking in the 0.3–1.2 px range is permitted **only** on genuinely all-caps micro-labels. Mixed-case at any size stays negative.
- **Extreme line-height range**: Headlines compress to 1.07 while body text opens to 1.47, and some button contexts stretch to 2.41. This dramatic range creates clear visual hierarchy through rhythm alone.

## 4. Component Stylings

### Mediaman component layer (single source of truth)

`static/style.css` and `templates/_components.html` expose a small primitive set. **Every page composes from these — no per-page variants.** If a button needs to behave differently on one screen, change the token, not a sibling class.

| Primitive  | Class root        | Variants / modifiers                                            | Notes |
|------------|-------------------|-----------------------------------------------------------------|-------|
| Button     | `.btn`            | `--primary --secondary --ghost --success --danger --accent-soft --icon`, sizes `--sm --lg` | Pill-shaped (`--r-pill`); `transform: scale(.97)` on `:active` |
| Split btn  | `.split`          | wraps a `.btn` + `.caret`                                        | Used on Library "Keep + dropdown" |
| Pill       | `.pill`           | `--movie --tv --anime --kept --stale --queued --neutral`         | 3 px × 10 px chip; tinted bg + colour pair |
| Filter pill| `.fpill`          | `--movie --tv --anime --kept --stale` plus `.on` for active state| Larger touch target than `.pill`; segmented pill bars |
| Countdown  | `.countdown`      | (single)                                                         | Pulsing red dot; lives inside `.tile-pills` |
| Tile       | `.tile` + `.tile-poster .tile-pills .tile-title .tile-meta .tile-rating .tile-actions` | — | Used by Dashboard scheduled, Library grid, Search, Recommended, Downloads recent |
| Card       | `.card`           | `--flat --bordered`, `.card-pad` for 24 px padding               | Container for tables, settings panels |
| Table      | `.tbl`            | rows take `.thumb`, `.title-cell .sub`                            | Library + History tables |
| Storage    | `.storage`        | `.storage-hd .storage-bar > .seg.seg--{mov,tv,anime,other}`       | Same math everywhere it appears |
| Stats      | `.stats / .lib-stats` with `.stat / .lib-stat`                  | `.on` for active filter; `.stat--stale` for danger tint | Library type filters |
| Hero (DL)  | `.dl-hero`        | `.dl-hero-bg .dl-hero-poster .dl-hero-info .dl-bar .dl-state-pill` | Cinematic backdrop + poster + progress |
| Compact row| `.dl-row` (alias `.dl-compact-row`) | `.dl-row-poster .dl-row-info .dl-row-pct`               | Queue + upcoming downloads |
| History row| `.hist`           | `.hist-date .hist-msg .hist-type`                                 | Audit log timeline |
| Settings   | `.setg-card .setg-row .setg-hd .setg-row-lbl .setg-row-sub`     | `.setg-nav` for the side rail | Used by every section in `settings.html` |
| Toggle     | `.tog` / `.toggle-switch` | `.on` for checked state                                  | 44 × 26 px iOS-style switch |
| Input      | `.inp` / `.form-input`   | (focus turns border to `--accent`)                       | Used for all text/number/select inputs |
| Status dot | `.conn` + `.conn-dot` | `.off .warn`                                              | Settings + nav connection indicator |
| Modal      | `.modal-backdrop` + `.modal-sheet` | `.modal-close-bar`, `.modal-actions`            | Search/Recommended detail sheet |
| Empty state| `.empty`          | `.empty-ico` icon + `h3` + `p`                                   | Replace bespoke `.empty-state__*` markup |

Macros for the most-used primitives live in `templates/_components.html`:

```jinja
{% import "_components.html" as c %}
{{ c.btn("Add", variant="primary", size="sm") }}
{{ c.pill("Movie", variant="movie") }}
{{ c.fpill("All", on=true) }}
{{ c.eyebrow("Your library, at a glance") }}
{{ c.sec_hd("Storage", "Across your selected libraries") }}
```

Anything that doesn't already have a primitive **must** be added to this layer rather than written inline. A second instance of the same pattern is the trigger.

### Buttons

**Primary Blue (CTA)**
- Background: `#0071e3` (MediaMan Blue)
- Text: `#ffffff`
- Padding: 8px 15px
- Radius: 8px
- Border: 1px solid transparent
- Font: SF Pro Text, 17px, weight 400
- Hover: background brightens slightly
- Active: `#ededf2` background shift
- Focus: `2px solid var(--sk-focus-color, #0071E3)` outline
- Use: Primary call-to-action ("Buy", "Shop iPhone")

**Primary Dark**
- Background: `#1d1d1f`
- Text: `#ffffff`
- Padding: 8px 15px
- Radius: 8px
- Font: SF Pro Text, 17px, weight 400
- Use: Secondary CTA, dark variant

**Pill Link (Learn More / Shop)**
- Background: transparent
- Text: `#0066cc` (light bg) or `#2997ff` (dark bg)
- Radius: 980px (full pill)
- Border: 1px solid `#0066cc`
- Font: SF Pro Text, 14px-17px
- Hover: underline decoration
- Use: "Learn more" and "Shop" links — the signature MediaMan inline CTA

**Filter / Search Button**
- Background: `#fafafc`
- Text: `rgba(0, 0, 0, 0.8)`
- Padding: 0px 14px
- Radius: 11px
- Border: 3px solid `rgba(0, 0, 0, 0.04)`
- Focus: `2px solid var(--sk-focus-color, #0071E3)` outline
- Use: Search bars, filter controls

**Media Control**
- Background: `rgba(210, 210, 215, 0.64)`
- Text: `rgba(0, 0, 0, 0.48)`
- Radius: 50% (circular)
- Active: scale(0.9), background shifts
- Focus: `2px solid var(--sk-focus-color, #0071e3)` outline, white bg, black text
- Use: Play/pause, carousel arrows

### Cards & Containers
- Background: `#f5f5f7` (light) or `#272729`-`#2a2a2d` (dark)
- Border: none (borders are rare in MediaMan's system)
- Radius: 5px-8px
- Shadow: `rgba(0, 0, 0, 0.22) 3px 5px 30px 0px` for elevated product cards
- Content: centered, generous padding
- Hover: no standard hover state — cards are static, links within them are interactive

### Navigation

The shipped nav uses **four surfaces** that activate based on viewport width:

| Surface | Element | Visible at |
|---------|---------|-----------|
| `.nav-glass` | Sticky top bar + full link rail | ≥ 700 px |
| `.nav-topbar` | Sticky top bar, brand + page title only | < 700 px |
| `.nav-tabs` | Fixed bottom tab bar (5 primary destinations) | < 700 px |
| `.nav-more-sheet` | Slide-up overflow drawer (Recommended, History, Settings, Logout) | < 700 px |

Glass spec — both `.nav-glass` and `.nav-topbar` use:
- Background: `rgba(0,0,0,0.8)` with `backdrop-filter: saturate(180%) blur(20px)`.
- Height: `.nav-glass` = `--nav-h` (52 px); `.nav-topbar` = 48 px. Bottom hairline `var(--hair)`.
- Brand left (`.brand` — `media<b>man</b>` with the bold mark in `--accent`).
- `.nav-glass` centre `.nav-links`: horizontal flex row of `.nav-btn` pills (7 px × 12 px, font-size 13 px, `--r-pill`). Active link gets `.on` (`color: var(--t1)`, `background: var(--s3)`).
- Logout sits in `.nav-logout-form` inside `.nav-links` at the far right of the desktop rail.
- The nav floats above content, maintaining its dark translucent glass regardless of section background.

### Image Treatment
- Products on solid-color fields (black or white) — no backgrounds, no context, just the object
- Full-bleed section images that span the entire viewport width
- Product photography at extremely high resolution with subtle shadows
- Lifestyle images confined to rounded-corner containers (12px+ radius)

### Distinctive Components

**Product Hero Module**
- Full-viewport-width section with solid background (black or `#f5f5f7`)
- Product name as the primary headline (SF Pro Display, 56px, weight 600)
- One-line descriptor below in lighter weight
- Two pill CTAs side by side: "Learn more" (outline) and "Buy" / "Shop" (filled)

**Product Grid Tile**
- Square or near-square card on contrasting background
- Product image dominating 60-70% of the tile
- Product name + one-line description below
- "Learn more" and "Shop" link pair at bottom

**Feature Comparison Strip**
- Horizontal scroll of product variants
- Each variant as a vertical card with image, name, and key specs
- Minimal chrome — the products speak for themselves

## 5. Layout Principles

### Spacing System
- Base unit: 8px
- Scale: 2px, 4px, 5px, 6px, 7px, 8px, 9px, 10px, 11px, 14px, 15px, 17px, 20px, 24px
- Notable characteristic: the scale is dense at small sizes (2-11px) with granular 1px increments, then jumps in larger steps. This allows precise micro-adjustments for typography and icon alignment.

### Grid & Container
- Max content width: approximately 980px (the recurring "980px radius" in pill buttons echoes this width)
- Hero: full-viewport-width sections with centered content block
- Product grids: 2-3 column layouts within centered container
- Single-column for hero moments — one product, one message, full attention
- No visible grid lines or gutters — spacing creates implied structure

### Whitespace Philosophy
- **Cinematic breathing room**: Each product section occupies a full viewport height (or close to it). The whitespace between products is not empty — it is the pause between scenes in a film.
- **Vertical rhythm through color blocks**: Rather than using spacing alone to separate sections, MediaMan uses alternating background colors (black, `#f5f5f7`, white). Each color change signals a new "scene."
- **Compression within, expansion between**: Text blocks are tightly set (negative letter-spacing, tight line-heights) while the space surrounding them is vast. This creates a tension between density and openness.

### Dark-only variant (Mediaman)

Mediaman is a media management tool, not a marketing site. It follows the **MediaMan TV / MediaMan Music / System Settings** model — dark-only, high-contrast, cinematic — rather than the mediaman marketing-site alternation. The cinematic rhythm rule therefore does NOT apply: there is no light `#f5f5f7` counterpart. Instead, rhythm comes from:

1. **Surface elevation** — `#000` body → `#1d1d1f` surface → `#2a2a2d` surface-2 → `#323236` surface-3.
2. **Hairline dividers** at `rgba(255,255,255,0.06)` between sections where colour contrast alone is insufficient.
3. **Vertical spacing** (`--space-section: clamp(32px, 6vw, 80px)`) carries the pause-between-scenes role that alternating colour blocks play on mediaman.

Cards and containers therefore sit on a dark surface and may use a 1 px hairline border at `rgba(255,255,255,0.06)` when adjacent to a same-coloured parent. Prefer surface-colour contrast; use a hairline only when the two surfaces would otherwise be indistinguishable.

### Border Radius Scale

Tokens (`:root` in `style.css`):

| Token       | Value  | Use                                                              |
|-------------|--------|------------------------------------------------------------------|
| `--r-xs`    | 6 px   | Thumbnails, tiny inset chips                                     |
| `--r-sm`    | 8 px   | Form inputs, snooze options, tile/poster small                   |
| `--r-md`    | 10 px  | Tile posters, compact rows, modal sub-blocks                     |
| `--r-lg`    | 12 px  | Cards, hero, settings panels                                     |
| `--r-pill`  | 980 px | All CTA buttons, pills, filter pills, status dots                |
| 50%         | —      | Media controls, avatars, toggle thumb                            |

**Never use a literal radius value.** Pick the closest token. The five-step scale is intentionally narrow — if a new component needs a radius outside this set, change the design, not the radius.

## 6. Depth & Elevation

| Level | Treatment | Use |
|-------|-----------|-----|
| Flat (Level 0) | No shadow, solid background | Standard content sections, text blocks |
| Navigation Glass | `backdrop-filter: saturate(180%) blur(20px)` on `rgba(0,0,0,0.8)` | Sticky navigation bar — the glass effect |
| Subtle Lift (Level 1) | `rgba(0, 0, 0, 0.22) 3px 5px 30px 0px` | Product cards, floating elements |
| Media Control | `rgba(210, 210, 215, 0.64)` background with scale transforms | Play/pause buttons, carousel controls |
| Focus (Accessibility) | `2px solid #0071e3` outline | Keyboard focus on all interactive elements |

**Shadow Philosophy**: MediaMan uses shadow extremely sparingly. The primary shadow (`3px 5px 30px` with 0.22 opacity) is soft, wide, and offset — mimicking a diffused studio light casting a natural shadow beneath a physical object. This reinforces the "product as physical sculpture" metaphor. Most elements have NO shadow at all; elevation comes from background color contrast (dark card on darker background, or light card on slightly different gray).

### Decorative Depth
- Navigation glass: the translucent, blurred navigation bar is the most recognizable depth element, creating a sense of floating UI above scrolling content
- Section color transitions: depth is implied by the alternation between black and light gray sections rather than by shadows
- Product photography shadows: the products themselves cast shadows in their photography, so the UI doesn't need to add synthetic ones

## 7. Do's and Don'ts

### Do
- Use SF Pro Display at 20px+ and SF Pro Text below 20px — respect the optical sizing boundary
- Apply negative letter-spacing at all text sizes (not just headlines) — MediaMan tracks tight universally
- Use MediaMan Blue (`#0071e3`) ONLY for interactive elements — it must be the singular accent
- Alternate between black and light gray (`#f5f5f7`) section backgrounds for cinematic rhythm
- Use 980px pill radius for CTA links — the signature MediaMan link shape
- Keep product imagery on solid-color fields with no competing visual elements
- Use the translucent dark glass (`rgba(0,0,0,0.8)` + blur) for sticky navigation
- Compress headline line-heights to 1.07-1.14 — MediaMan headlines are famously tight

### Don't
- Don't introduce additional accent colors beyond MediaMan Blue and the sanctioned semantic palette (§2 Semantic Palette); never extend the palette to CTAs, headings, or structural surfaces
- Don't use heavy shadows or multiple shadow layers — MediaMan's shadow system is one soft diffused shadow or nothing. Use the `--shadow-card` token; never invent a bespoke shadow.
- Don't use borders on cards or containers as a decorative device. The dark-only variant permits a 1 px hairline at `rgba(255,255,255,0.06)` only when adjacent surfaces would otherwise be indistinguishable (see §5 Dark-only variant).
- Don't apply wide letter-spacing to SF Pro mixed-case text — it is designed to run tight at every size. Positive tracking is permitted only on all-caps micro-labels (§3 uppercase exception).
- Don't use weight 800 or 900 — the maximum is 700 (bold), and even that is rare. At 20px+ (Display sizes) weight should be 400 or 600, never 700.
- Don't add textures, patterns, or gradients to UI backgrounds — solid colors only. Gradient scrims OVER media imagery (posters, hero backdrops) are permitted since they sit over photography, not over UI chrome.
- Don't make the navigation opaque — the glass blur effect is essential to the MediaMan UI identity. The translucent nav must be `rgba(0,0,0,0.8)` with `backdrop-filter: saturate(180%) blur(20px)`; do not vary opacity between top-nav surfaces.
- Don't center-align body text — MediaMan body copy is left-aligned; only headlines center
- Don't use rounded corners larger than 12px on rectangular elements (980px is for pills; 20px top radii are permitted on bottom/top sheet surfaces only).
- Don't layer a box-shadow glow ring on focused inputs — the global 2 px solid MediaMan Blue outline (`:focus-visible`) is the focus treatment everywhere. No exceptions.

## 8. Responsive Behavior

### Breakpoints
| Name | Width | Key Changes |
|------|-------|-------------|
| Small Mobile | <360px | Minimum supported, single column |
| Mobile | 360-480px | Standard mobile layout |
| Mobile Large | 480-640px | Wider single column, larger images |
| Tablet Small | 640-834px | 2-column product grids begin |
| Tablet | 834-1024px | Full tablet layout, expanded nav |
| Desktop Small | 1024-1070px | Standard desktop layout begins |
| Desktop | 1070-1440px | Full layout, max content width |
| Large Desktop | >1440px | Centered with generous margins |

**Production tokens used in CSS** (subset we actually branch on):

| Token | Min-width | Purpose |
|-------|-----------|---------|
| `--bp-sm` | 480 px | Large phones: 4-up stats grid, denser cards |
| `--bp-md` | 640 px | **Primary phone/tablet split** — nav surface swap, library goes tabular, forms go 2-col |
| `--bp-lg` | 1024 px | Desktop — full library grid, wider containers |
| `--bp-xl` | 1440 px | Large desktop — centred with generous margins |

All media queries use **mobile-first** `min-width`. The descriptive table above is kept for design reference; production CSS branches on the four tokens.

### Touch Targets
- Primary CTAs: 8px 15px padding creating ~44px touch height
- Navigation links: 48px height with adequate spacing
- Media controls: 50% radius circular buttons, minimum 44x44px
- "Learn more" pills: generous padding for comfortable tapping
- Interactive elements below `--bp-md` (filter pills, sort headers, tab-bar tabs, modal close, form inputs, nav more-sheet rows) must meet a minimum 44 × 44 px hit area per MediaMan HIG.

### Collapsing Strategy
- Hero headlines: 56px Display → 40px → 28px on mobile, maintaining tight line-height proportionally
- Product grids: 3-column → 2-column → single column stacked
- Navigation: full horizontal top nav (≥640 px) → bottom tab bar + compact mobile top bar (<640 px). Five primary destinations fit the tab bar (Home, Library, Search, Downloads, More); overflow (Recommended, History, Settings, Logout) lives in a More sheet.
- Product hero modules: full-bleed maintained at all sizes, text scales down
- Section backgrounds: maintain full-width color blocks at all breakpoints — the cinematic rhythm never breaks
- Image sizing: products scale proportionally, never crop — the product silhouette is sacred

### Image Behavior
- Product photography maintains aspect ratio at all breakpoints
- Hero product images scale down but stay centered
- Full-bleed section backgrounds persist at every size
- Lifestyle images may crop on mobile but maintain their rounded corners
- Lazy loading for below-fold product images

## 9. Agent Prompt Guide

### Quick Color Reference
- Primary CTA: MediaMan Blue (`#0071e3`)
- Page background (light): `#f5f5f7`
- Page background (dark): `#000000`
- Heading text (light): `#1d1d1f`
- Heading text (dark): `#ffffff`
- Body text: `rgba(0, 0, 0, 0.8)` on light, `#ffffff` on dark
- Link (light bg): `#0066cc`
- Link (dark bg): `#2997ff`
- Focus ring: `#0071e3`
- Card shadow: `rgba(0, 0, 0, 0.22) 3px 5px 30px 0px`

### Example Component Prompts
- "Create a hero section on black background. Headline at 56px SF Pro Display weight 600, line-height 1.07, letter-spacing -0.28px, color white. One-line subtitle at 21px SF Pro Display weight 400, line-height 1.19, color white. Two pill CTAs: 'Learn more' (transparent bg, white text, 1px solid white border, 980px radius) and 'Buy' (MediaMan Blue #0071e3 bg, white text, 8px radius, 8px 15px padding)."
- "Design a product card: #f5f5f7 background, 8px border-radius, no border, no shadow. Product image top 60% of card on solid background. Title at 28px SF Pro Display weight 400, letter-spacing 0.196px, line-height 1.14. Description at 14px SF Pro Text weight 400, color rgba(0,0,0,0.8). 'Learn more' and 'Shop' links in #0066cc at 14px."
- "Build the MediaMan navigation: sticky, 48px height, background rgba(0,0,0,0.8) with backdrop-filter: saturate(180%) blur(20px). Links at 12px SF Pro Text weight 400, white text. MediaMan logo left, links centered, search and bag icons right."
- "Create an alternating section layout: first section black bg with white text and centered product image, second section #f5f5f7 bg with #1d1d1f text. Each section near full-viewport height with 56px headline and two pill CTAs below."
- "Design a 'Learn more' link: text #0066cc on light bg or #2997ff on dark bg, 14px SF Pro Text, underline on hover. After the text, include a right-arrow chevron character (>). Wrap in a container with 980px border-radius for pill shape when used as a standalone CTA."

### Iteration Guide
1. Every interactive element gets MediaMan Blue (`#0071e3`) — no other accent colors
2. Section backgrounds alternate: black for immersive moments, `#f5f5f7` for informational moments
3. Typography optical sizing: SF Pro Display at 20px+, SF Pro Text below — never mix
4. Negative letter-spacing at all sizes: -0.28px at 56px, -0.374px at 17px, -0.224px at 14px, -0.12px at 12px
5. The navigation glass effect (translucent dark + blur) is non-negotiable — it defines the MediaMan web experience
6. Products always appear on solid color fields — never on gradients, textures, or lifestyle backgrounds in hero modules
7. Shadow is rare and always soft: `3px 5px 30px 0.22 opacity` or nothing at all
8. Pill CTAs use 980px radius — this creates the signature MediaMan rounded-rectangle-that-looks-like-a-capsule shape
