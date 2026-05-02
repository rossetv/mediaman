"""Static accessibility guardrails for templates (Domain 10).

These tests do not render the templates — they treat the Jinja source as
the contract surface. They lock in the WCAG fixes from the Domain 10 wave
so that a future refactor doesn't silently undo a landmark, an h1, an
alt tweak, or the keyboard-accessible table sort.

Each test names the finding it guards in a comment so a future engineer
who breaks one can read FINDINGS.md for context.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "src/mediaman/web/templates"
CSS_DIR = REPO / "src/mediaman/web/static/css"


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _css() -> str:
    parts = [path.read_text(encoding="utf-8") for path in sorted(CSS_DIR.glob("_*.css"))]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# base.html — landmarks and locale
# ---------------------------------------------------------------------------


def test_base_html_uses_british_english_locale():
    """Finding 3: lang attribute should be en-GB, matching the British
    English copy convention used everywhere else in the codebase."""
    base = _tpl("base.html")
    assert '<html lang="en-GB">' in base


def test_base_html_wraps_content_in_main_landmark():
    """Finding 1: the page content must live inside a <main> landmark with
    a focus-target id so the skip-link can land on it."""
    base = _tpl("base.html")
    assert '<main id="main-content" tabindex="-1">' in base
    assert "</main>" in base


def test_base_html_starts_with_skip_to_content_link():
    """Finding 2: the very first focusable element must be a skip link
    that targets the main landmark."""
    base = _tpl("base.html")
    body_idx = base.index("<body")
    skip_idx = base.index('<a class="skip-link"', body_idx)
    nav_idx = base.index("{% block nav %}", body_idx)
    main_idx = base.index('<main id="main-content"', body_idx)
    # skip-link must come before both the nav block and main, so that
    # a Tab from the address bar lands on it first.
    assert skip_idx < nav_idx
    assert skip_idx < main_idx
    assert 'href="#main-content"' in base


def test_skip_link_styled_in_base_css():
    """Finding 2: the skip-link must be visually hidden by default and
    revealed on focus. The transform-on-focus is the contract that
    keyboard users actually see something appear when they tab in."""
    css = _css()
    assert ".skip-link {" in css
    skip_block = css[css.index(".skip-link {") :]
    skip_block = skip_block[: skip_block.index("}") + 1]
    assert "transform: translateY(-200%)" in skip_block
    # And the :focus rule reveals it.
    focus_idx = css.index(".skip-link:focus")
    focus_block = css[focus_idx : focus_idx + 400]
    assert "translateY(0)" in focus_block


# ---------------------------------------------------------------------------
# Per-page h1 contracts
# ---------------------------------------------------------------------------


def test_login_page_has_h1():
    """Finding 6: login.html had no h1; the brand div was the de-facto
    heading. We add an sr-only h1 so AT users get a real page heading."""
    login = _tpl("login.html")
    assert '<h1 class="sr-only">Sign in to mediaman</h1>' in login


def test_keep_page_active_state_has_h1():
    """Finding 4: the active state's title must be an h1, not a div."""
    keep = _tpl("keep.html")
    # All three states (active, already_kept, expired) rebuild an
    # h1.item-title; collapse the assertion onto count.
    assert keep.count('<h1 class="item-title">') == 2  # active, already_kept
    assert '<h1 class="item-title item-title--secondary">' in keep  # expired


def test_download_page_has_h1():
    """Finding 5: download.html had no h1. The cinematic title is now an
    h1, the queued state has an sr-only h1, and expired state has its
    own h1."""
    dl = _tpl("download.html")
    assert '<h1 class="dl-cinema-title">' in dl
    assert '<h1 class="sr-only">{{ hero_item.title }}</h1>' in dl
    assert '<h1 class="dl-expired-title">' in dl


# ---------------------------------------------------------------------------
# Image alt text — empty when caption is adjacent
# ---------------------------------------------------------------------------


def test_keep_page_posters_use_empty_alt():
    """Finding 7: the visible title is rendered immediately below the
    poster, so the poster's alt text must be empty (decorative) — not
    ``alt={{ item.title }}`` which would double-announce."""
    keep = _tpl("keep.html")
    assert 'alt="{{ item.title }}"' not in keep
    # All three poster <img> blocks now use alt="".
    poster_imgs = [line for line in keep.splitlines() if "/api/poster/" in line and "<img" in line]
    assert len(poster_imgs) >= 3
    for line in poster_imgs:
        assert 'alt=""' in line, f"poster <img> must have empty alt: {line}"


def test_dashboard_posters_use_empty_alt():
    """Finding 8: scheduled-items tiles + recently-deleted thumbs each
    render the title as adjacent text, so the alt must be empty."""
    dash = _tpl("dashboard.html")
    assert 'alt="{{ item.title }}"' not in dash


def test_protected_uses_poster_macro():
    """Finding 9: three identical poster blocks were collapsed to a
    shared c.poster macro to keep the alt-empty contract in one place."""
    prot = _tpl("protected.html")
    # No raw posters left.
    assert 'alt="{{ item.show_title }}"' not in prot
    assert 'alt="{{ item.title }}"' not in prot
    # Macro is used in three spots (kept-shows, forever, snoozed).
    assert prot.count("{{ c.poster(item) }}") == 3


def test_components_exposes_poster_macro():
    """Finding 9: the macro is the new single source of truth — without
    it, future templates would need to know the alt-empty rule."""
    comps = _tpl("_components.html")
    assert "{% macro poster(item)" in comps
    assert 'alt=""' in comps  # macro emits empty alt


def test_components_warns_about_safe_filter():
    """Finding 12: macros use ``|safe`` for trusted slots. The header
    comment must warn future contributors to escape user-supplied
    content before passing it through one of those slots."""
    comps = _tpl("_components.html")
    assert "SECURITY" in comps
    assert "user-supplied" in comps


# ---------------------------------------------------------------------------
# library.html — sortable table headers must be buttons
# ---------------------------------------------------------------------------


def test_library_table_sort_uses_buttons():
    """Finding 10: <th onclick=...> is not keyboard-activatable. Each
    sortable header now wraps its content in a real <button> with an
    aria-sort attribute."""
    lib = _tpl("library.html")
    # Inline onclick on the <th> must be gone.
    assert "onclick=\"toggleSort('name')\"" not in lib
    assert "onclick=\"toggleSort('size')\"" not in lib
    assert "onclick=\"toggleSort('watched')\"" not in lib
    # The buttons must exist and carry the sort metadata.
    assert lib.count('class="th-sort"') == 3
    assert 'data-sort-col="name"' in lib
    assert 'data-sort-col="size"' in lib
    assert 'data-sort-col="watched"' in lib
    # aria-sort lets AT announce the current sort state.
    assert "aria-sort=" in lib


def test_library_table_sort_button_styled():
    """Finding 10: the .th-sort button must visually match the cell — the
    rule sets are what guarantees that."""
    css = _css()
    assert ".tbl thead th.sortable .th-sort" in css


# ---------------------------------------------------------------------------
# _detail_modal.html — labelled by the visible title
# ---------------------------------------------------------------------------


def test_detail_modal_labelled_by_visible_title():
    """Finding 11: the dialog's aria-labelledby points at #detail-modal-title
    (the visible <h2> injected by the page script). The hidden literal
    ``<h2 class="sr-only">Details</h2>`` is gone."""
    modal = _tpl("_detail_modal.html")
    assert 'aria-labelledby="detail-modal-title"' in modal
    # The placeholder sr-only h2 was removed.
    assert '<h2 id="detail-modal-title" class="sr-only">Details</h2>' not in modal


def test_search_inline_script_renders_modal_title_as_h2():
    """Finding 11: search.html's modal-render must build an
    <h2 id="detail-modal-title"> so aria-labelledby resolves. The script
    now lives in static/js/search.js after the CSP-nonce extraction; the
    template references it via a <script src> tag."""
    search = _tpl("search.html")
    assert '<script src="/static/js/search.js" defer></script>' in search
    search_js = (REPO / "src/mediaman/web/static/js/search.js").read_text(encoding="utf-8")
    assert '<h2 id="detail-modal-title" class="detail-modal-hero-title">' in search_js


def test_recommended_inline_script_renders_modal_title_as_h2():
    """Finding 11: recommended.html builds the title via DOM methods —
    it must instantiate an h2 with the matching id. The script now lives
    in static/js/recommended.js after the CSP-nonce extraction; the
    template references it via a <script src> tag."""
    rec = _tpl("recommended.html")
    assert '<script src="/static/js/recommended.js" defer></script>' in rec
    rec_js = (REPO / "src/mediaman/web/static/js/recommended.js").read_text(encoding="utf-8")
    assert "createElement('h2')" in rec_js
    assert "heroTitle.id = 'detail-modal-title'" in rec_js


# ---------------------------------------------------------------------------
# force_password_change.html — alert announces every issue
# ---------------------------------------------------------------------------


def test_force_password_change_policy_issues_announce_each_item():
    """Finding 13: role=alert alone often only announces the container's
    first text node. aria-live=polite + role=list on the <ul> guarantees
    every issue line is announced to AT."""
    fpc = _tpl("force_password_change.html")
    assert 'class="policy-issues" role="alert" aria-live="polite"' in fpc
    assert '<ul role="list">' in fpc


# ---------------------------------------------------------------------------
# keep.html — public token page must not show a button the server rejects
# ---------------------------------------------------------------------------


def test_keep_page_does_not_offer_forever_button():
    """Finding 14: POST /keep/{token} returns 400 for ``duration=forever``
    (forever-keep is admin-only and lives on a dedicated authenticated
    endpoint). Showing the button anyway gave admins a button that
    always failed; remove it."""
    keep = _tpl("keep.html")
    # The button no longer exists.
    assert "snooze-forever" not in keep
    assert 'value="forever"' not in keep
    # And the admin-only label is gone.
    assert 'Keep <span class="duration">forever</span>' not in keep
