"""Static accessibility guardrails for templates.

These tests do not render the templates — they treat the Jinja source as
the contract surface. They lock in the WCAG fixes so that a future refactor
doesn't silently undo a landmark, an h1, an alt tweak, or the
keyboard-accessible table sort.
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


def test_base_html_uses_british_english_locale():
    """Lang attribute should be en-GB, matching the British
    English copy convention used everywhere else in the codebase."""
    base = _tpl("base.html")
    assert '<html lang="en-GB">' in base


def test_base_html_wraps_content_in_main_landmark():
    """The page content must live inside a <main> landmark with
    a focus-target id so the skip-link can land on it."""
    base = _tpl("base.html")
    assert '<main id="main-content" tabindex="-1">' in base
    assert "</main>" in base


def test_base_html_starts_with_skip_to_content_link():
    """The very first focusable element must be a skip link
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
    """The skip-link must be visually hidden by default and
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


def test_login_page_has_h1():
    """Login.html had no h1; the brand div was the de-facto
    heading. We add an sr-only h1 so AT users get a real page heading."""
    login = _tpl("login.html")
    assert '<h1 class="sr-only">Sign in to mediaman</h1>' in login


def test_keep_page_active_state_has_h1():
    keep = _tpl("keep.html")
    # The active, already_kept and deleting states each rebuild an
    # h1.item-title; the expired state uses the secondary variant.
    assert keep.count('<h1 class="item-title">') == 3  # active, already_kept, deleting
    assert '<h1 class="item-title item-title--secondary">' in keep  # expired


def test_download_page_has_h1():
    """Download.html had no h1. The cinematic title is an
    h1, the queued state has an sr-only h1, and expired state has its
    own h1."""
    dl = _tpl("download.html")
    assert '<h1 class="dl-cinema-title">' in dl
    assert '<h1 class="sr-only">{{ hero_item.title }}</h1>' in dl
    assert '<h1 class="dl-expired-title">' in dl


def test_keep_page_posters_use_empty_alt():
    """The visible title is rendered immediately below the
    poster, so the poster's alt text must be empty (decorative) — not
    ``alt={{ item.title }}`` which would double-announce."""
    keep = _tpl("keep.html")
    assert 'alt="{{ item.title }}"' not in keep
    # All three poster <img> blocks use alt="".
    poster_imgs = [
        line for line in keep.splitlines() if "{{ poster_url }}" in line and "<img" in line
    ]
    assert len(poster_imgs) >= 3
    for line in poster_imgs:
        assert 'alt=""' in line, f"poster <img> must have empty alt: {line}"


def test_dashboard_posters_use_empty_alt():
    """Scheduled-items tiles + recently-deleted thumbs each
    render the title as adjacent text, so the alt must be empty."""
    dash = _tpl("dashboard.html")
    assert 'alt="{{ item.title }}"' not in dash


def test_protected_uses_poster_macro():
    """Poster rendering for protected items is delegated to
    c.tile (which owns the alt-empty contract via the poster_alt param).  Raw
    inline img tags and the intermediate c.poster calls are gone."""
    prot = _tpl("protected.html")
    # No raw poster img tags with a non-empty alt on these plex-proxied images.
    assert 'alt="{{ item.show_title }}"' not in prot
    assert 'alt="{{ item.title }}"' not in prot
    # c.tile is used in three spots (kept-shows, forever, snoozed); each passes
    # poster_url so the macro's img (with empty poster_alt) handles rendering.
    assert prot.count("call c.tile(") == 3
    assert prot.count('poster_url="/api/poster/"') == 3
    # The standalone c.poster calls are retired — the macro is the single source.
    assert "{{ c.poster(item) }}" not in prot


def test_components_exposes_poster_macro():
    """The macro is the new single source of truth — without
    it, future templates would need to know the alt-empty rule."""
    comps = _tpl("_components.html")
    assert "{% macro poster(item)" in comps
    assert 'alt=""' in comps  # macro emits empty alt


def test_components_warns_about_safe_filter():
    """Macros use ``|safe`` for trusted slots. The header
    comment must warn future contributors to escape user-supplied
    content before passing it through one of those slots."""
    comps = _tpl("_components.html")
    assert "SECURITY" in comps
    assert "user-supplied" in comps


def test_library_table_sort_uses_buttons():
    """<th onclick=...> is not keyboard-activatable. Each
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
    """The .th-sort button must visually match the cell — the
    rule sets are what guarantees that."""
    css = _css()
    assert ".tbl thead th.sortable .th-sort" in css


def test_detail_modal_labelled_by_visible_title():
    """The dialog's aria-labelledby points at #detail-modal-title
    (the visible <h2> injected by the page script). The hidden literal
    ``<h2 class="sr-only">Details</h2>`` is gone."""
    modal = _tpl("_detail_modal.html")
    assert 'aria-labelledby="detail-modal-title"' in modal
    # There is no placeholder sr-only h2.
    assert '<h2 id="detail-modal-title" class="sr-only">Details</h2>' not in modal


def test_search_inline_script_renders_modal_title_as_h2():
    """Search.html's modal-render must build an
    <h2 id="detail-modal-title"> so aria-labelledby resolves. The detail-modal rendering lives in
    static/js/search/detail_modal.js; search.html must load that sub-module
    before search.js so the h2 is still guaranteed to be built."""
    search = _tpl("search.html")
    assert '<script src="/static/js/search/detail_modal.js" defer></script>' in search
    assert '<script src="/static/js/search.js" defer></script>' in search
    detail_js = (REPO / "src/mediaman/web/static/js/search/detail_modal.js").read_text(
        encoding="utf-8"
    )
    assert '<h2 id="detail-modal-title" class="detail-modal-hero-title">' in detail_js


def test_recommended_inline_script_renders_modal_title_as_h2():
    """Recommended.html builds the title via DOM methods —
    it must instantiate an h2 with the matching id. The script lives
    in static/js/recommended.js; the template references it via a
    <script src> tag."""
    rec = _tpl("recommended.html")
    assert '<script src="/static/js/recommended.js" defer></script>' in rec
    rec_js = (REPO / "src/mediaman/web/static/js/recommended.js").read_text(encoding="utf-8")
    assert "createElement('h2')" in rec_js
    assert "heroTitle.id = 'detail-modal-title'" in rec_js


def test_force_password_change_policy_issues_announce_each_item():
    """Role=alert alone often only announces the container's
    first text node. aria-live=polite + role=list on the <ul> guarantees
    every issue line is announced to AT."""
    fpc = _tpl("force_password_change.html")
    assert 'class="policy-issues" role="alert" aria-live="polite"' in fpc
    assert '<ul role="list">' in fpc


def test_keep_page_does_not_offer_forever_button():
    """POST /keep/{token} returns 400 for ``duration=forever``
    (forever-keep is admin-only and lives on a dedicated authenticated
    endpoint). Showing the button anyway gave admins a button that
    always failed; remove it."""
    keep = _tpl("keep.html")
    # The button is absent.
    assert "snooze-forever" not in keep
    assert 'value="forever"' not in keep
    # And the admin-only label is gone.
    assert 'Keep <span class="duration">forever</span>' not in keep


def test_settings_save_status_is_a_live_region():
    """A save confirmation in the bar must be spoken, not only shown."""
    assert 'id="save-status" role="status"' in _tpl("settings.html")


def test_disk_threshold_inputs_name_their_library():
    """The JS-built path and threshold inputs have no visible <label>, so each
    needs an aria-label that says which library it belongs to."""
    js = (REPO / "src/mediaman/web/static/js/settings/disk_thresholds.js").read_text("utf-8")
    assert "pathInput.setAttribute('aria-label', 'Filesystem path, ' + lib.title)" in js
    assert "thrInput.setAttribute('aria-label'" in js
    assert "lib.title" in js.split("thrInput.setAttribute('aria-label'")[1].split("\n")[0]


def test_savebar_ignores_the_add_subscriber_row():
    """The subscriber email has its own Add button and is not a saved setting."""
    js = (REPO / "src/mediaman/web/static/js/settings/savebar.js").read_text("utf-8")
    assert ".sub-add" in js
    # Nothing the savebar sends may live inside .sub-add.
    sub_add = _tpl("settings/_sec_newsletter.html").split('class="sub-add"')[1].split("</div>")[0]
    assert "data-field" not in sub_add and 'name="' not in sub_add
