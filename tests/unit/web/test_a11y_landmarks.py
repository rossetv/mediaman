"""Static accessibility guardrails for templates.

These tests do not render the templates — they treat the Jinja source as
the contract surface. They lock in the WCAG fixes so that a future refactor
doesn't silently undo a landmark, an h1, an alt tweak, or the
keyboard-accessible table sort.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from mediaman.web.routes.keep import _lengths

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
    # The states with a title share one heading.
    assert "{{ share.heading(item.heading) }}" in keep
    assert "<h1 " in _tpl("_share.html").split("macro heading")[1].split("endmacro")[0]


def test_download_page_has_h1():
    """The title is an h1 in every state that has one, and the pages
    with only something to say have their own."""
    dl = _tpl("download.html")
    assert "{{ share.heading(item.title or '') }}" in dl
    assert "share.notice(" in dl and "share.limited(" in dl
    assert "<h1>{{ title }}</h1>" in _tpl("_share.html")


def test_keep_page_posters_use_empty_alt():
    """The visible title is rendered immediately below the
    poster, so the poster's alt text must be empty (decorative) — not
    ``alt={{ item.title }}`` which would double-announce."""
    assert "<img" not in _tpl("keep.html")
    poster_imgs = [line for line in _tpl("_share.html").splitlines() if "<img" in line]
    assert len(poster_imgs) >= 3
    for line in poster_imgs:
        assert 'alt=""' in line, f"poster <img> must have empty alt: {line}"


def test_components_warns_about_safe_filter():
    """Macros use ``|safe`` for trusted slots. The header
    comment must warn future contributors to escape user-supplied
    content before passing it through one of those slots."""
    comps = _tpl("_components.html")
    assert "SECURITY" in comps
    assert "user-supplied" in comps


def test_title_detail_labelled_by_visible_title():
    """The detail dialog's aria-labelledby points at a real, visible <h2>
    in the template; no placeholder sr-only heading stands in for it."""
    detail = _tpl("_watch_detail.html")
    assert 'aria-labelledby="ttl-title"' in detail
    assert re.search(r'<h2 class="ttl__title" id="ttl-title"[^>]*></h2>', detail)
    assert 'class="sr-only">Details</h2>' not in detail


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
    # The lengths come from the route, which leaves it out.
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    assert "forever" not in [length["duration"] for length in _lengths(now, now)]


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
