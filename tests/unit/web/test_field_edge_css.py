"""A field's edge, the bars the keyboard must stay clear of, and All titles' grid on a phone, read as rules."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/css"
CONTROLS = ":is(a, button, input, select, textarea, [tabindex])"


def _rules(sheet: str, media: str | None = None) -> list[tuple[str, str]]:
    """(selector list, declarations) outside any `@media`, or inside those asking for *media*."""
    css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(encoding="utf-8"), flags=re.S)
    blocks = r"@media ([^{]+)\{((?:[^{}]*\{[^{}]*\})*)\s*\}"
    text = (
        "".join(body for query, body in re.findall(blocks, css) if media in query)
        if media
        else re.sub(blocks, "", css)
    )
    return [(head.strip(), body) for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text)]


def _of(selector: str, sheet: str, media: str | None = None) -> str:
    return "".join(
        body
        for head, body in _rules(sheet, media)
        if selector in [s.strip() for s in re.split(r",(?![^()]*\))", head)]
    )


def _token(name: str) -> str:
    return re.search(rf"{re.escape(name)}:\s*([^;]+);", (CSS / "_tokens.css").read_text())[
        1
    ].strip()


def _luminance(hex_colour: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_a_fields_edge_stands_out_three_to_one_from_every_surface_a_field_sits_on() -> None:
    edge = _token("--field-edge")
    surfaces = {
        "#000000" if s == "#000" else s for s in map(_token, ("--bg", "--s1", "--s2", "--s3"))
    }
    assert all(_contrast(edge, s) >= 3 for s in surfaces), edge


def test_every_text_field_draws_its_hairline_as_the_edge_unless_it_is_disabled() -> None:
    rule = [
        body
        for head, body in _rules("_forms.css")
        if head.startswith(":where(input:not(")
        and head.endswith("select, textarea):not(:disabled)")
    ]
    assert rule and "--hair: var(--field-edge)" in rule[0]
    assert "1px solid var(--hair)" in _of(".inp", "_forms.css")


def test_historys_search_and_its_menus_fields_have_an_edge() -> None:
    for field in (".hx-search input", ".hx-menu__f input", ".hx-dates input"):
        assert "border: 1px solid var(--hair);" in _of(field, "_history.css"), field


def test_the_code_boxes_of_pairing_and_play_on_have_the_fields_edge() -> None:
    assert "border: 1px solid var(--field-edge)" in _of(".pair__code i", "_pair.css")
    assert "border: 1px solid var(--field-edge)" in _of(".play-on__code i", "_play_on.css")


def test_an_off_switch_shows_its_track_and_an_on_one_its_colour_alone() -> None:
    edge = _of(".tog:not(.on, :focus-visible)", "_forms.css")
    assert "outline: 1px solid var(--field-edge)" in edge and "outline-offset: -1px" in edge
    # Drawn without a border, so the switch keeps its size and the knob its place.
    assert "border:" not in _of(".tog", "_forms.css")


def test_on_history_the_keyboard_lands_clear_of_the_bar_and_tools_but_a_bars_own_control_stays_put() -> (
    None
):
    assert "scroll-padding-top: var(--bar-clear)" in _of(
        'html[data-page="history"]', "_history.css"
    )
    held = 'html[data-page="history"].is-bar-focus'
    assert "scroll-padding-top: 0" in _of(held, "_history.css")


def test_under_a_short_window_historys_tools_and_all_titles_head_go_with_the_page() -> None:
    short = "(max-height: 500px)"
    assert "position: relative" in _of(".hx-tools", "_history.css", short)
    assert "--bar-clear: calc(var(--nav-h) + 12px)" in _of(
        'html[data-page="history"]', "_history.css", short
    )
    assert "position: relative" in _of(".watch-all__head", "_watch_grid.css", short)


def test_all_titles_goes_two_across_only_where_a_poster_would_be_tiny() -> None:
    # One rule from 360 wide up, so a phone's posters match across 360-440; a very long word
    # breaks inside its card there (overflow-wrap) rather than the columns changing by width.
    two = [
        m
        for m in re.finditer(
            r"@media \(max-width: ([\d.]+)px\) \{\s*\.watch-grid-host > \.watch-grid \{ ([^}]*)\}",
            (CSS / "_watch_grid.css").read_text(),
        )
        if "repeat(2," in m[2]
    ]
    assert len(two) == 1
    first_three = float(two[0][1]) + 0.02
    assert first_three <= 360
    assert (first_three - 2 * 20 - 2 * 10) / 3 >= 100


def test_search_results_go_two_across_where_all_titles_does() -> None:
    narrow = _of(".results-page .watch-grid", "_watch_search.css", "(max-width: 389.98px)")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in narrow
