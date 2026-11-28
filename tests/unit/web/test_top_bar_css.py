"""The top bar's own details: the logo stays put, answers a pointer only, and the field leaves the keyboard its way."""

from __future__ import annotations

from tests.unit.web._css import rules, style


def test_the_logo_never_moves_and_only_its_fade_back_waits() -> None:
    logo = style(".wbar .brand > *", "_watch_search.css")
    assert "transform" not in logo and logo["transition"].endswith(" .12s")
    # The storage and the profile still wait their turn as a whole.
    shared = style(".wbar :is(.space-meter, .nav-end)", "_watch_search.css")
    assert "var(--dur-slow)" in shared["transition"]


def test_a_focused_empty_field_leaves_the_logo_and_the_storage_where_tab_reaches_them() -> None:
    hiding = [
        sel
        for _, sels, decls in rules("_watch_search.css")
        if decls.get("display") == "none"
        for sel in sels
        if "> .brand" in sel or "> .space-meter" in sel
    ]
    assert hiding
    for sel in hiding:
        assert ".is-focus:not(.has-value, .has-token)" in sel


def test_the_logo_back_and_profile_buttons_dim_under_a_pointer_only() -> None:
    # A tap on a phone leaves :hover on, so a rule outside the media query would stay dimmed.
    nav = rules("_nav.css")
    for selector in (".brand", ".nav-back", ".nav-noscript", ".profile-btn"):
        hovers = [
            (media, s) for media, sels, _ in nav for s in sels if s.endswith(selector + ":hover")
        ]
        assert hovers, selector
        assert all(media == ("(hover: hover)",) for media, _ in hovers), selector
