"""A person's page: what they are known for is a Watch row, its cards as large as the rows'."""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "static" / "css"
TOUCH = r"@media \(hover: none\), \(pointer: coarse\) \{"
PHONE = (
    r"@media \(hover: none\) and \(max-width: 599\.98px\), "
    r"\(pointer: coarse\) and \(max-width: 599\.98px\) \{"
)


def _declared(sheet: str, media: str, selector: str, prop: str) -> str:
    css = (CSS / sheet).read_text()
    block = re.search(media + r"\s*" + re.escape(selector) + r" \{([^}]*)\}", css)
    assert block, (sheet, selector)
    value = re.search(re.escape(prop) + r":\s*([^;]+);", block.group(1))
    assert value, (sheet, selector, prop)
    return value.group(1).strip()


def test_on_touch_the_known_for_cards_are_sized_as_the_watch_rows_cards() -> None:
    for prop in ("--watch-card-w", "--watch-peek"):
        assert _declared("_watch_explore.css", TOUCH, ".explore-known", prop) == _declared(
            "_watch.css", TOUCH, ".watch-rows", prop
        )
    assert _declared("_watch_explore.css", PHONE, ".explore-known", "--watch-peek") == _declared(
        "_watch.css", PHONE, ".watch-rows", "--watch-peek"
    )


def test_the_known_for_row_runs_to_the_screens_edges_where_its_fades_are() -> None:
    css = (CSS / "_watch_explore.css").read_text()
    rule = re.search(r"\n\.explore-known \{([^}]*)\}", css)
    assert rule and "container-type: inline-size" in rule.group(1)
    assert "calc(-1 * var(--pad))" in rule.group(1)


def test_where_it_fits_known_for_stands_beside_who_they_are_and_under_them_where_not() -> None:
    css = (CSS / "_watch_explore.css").read_text()
    wide = re.search(r"@media \(min-width: 1360px\) \{(.*?)\n\}", css, re.S)
    assert wide, "a wide layout"
    beside = re.search(
        r"\.explore--person \.explore-head > \.explore-known \{([^}]*)\}", wide.group(1)
    )
    assert beside and "grid-column: 3" in beside.group(1) and "grid-row: 1 / 3" in beside.group(1)
    under = re.search(r"\n\.explore--person \.explore-head > \.explore-known \{([^}]*)\}", css)
    assert under and "grid-column: 1 / -1" in under.group(1)


def test_a_persons_dates_placeholder_keeps_its_margins_inside_so_it_is_as_tall_as_the_dates() -> (
    None
):
    css = (CSS / "_watch_explore.css").read_text()
    assert re.search(r"\.explore-born:has\(> \.skel\) \{[^}]*display: flow-root", css)
    line = re.search(r"\.explore-born > \.skel--line \{([^}]*)\}", css)
    assert line
    height = int(re.search(r"height: (\d+)px", line[1])[1])
    margin = int(re.search(r"margin: (\d+)px 0", line[1])[1])
    born = re.search(r"\.explore-born \{[^}]*min-height: ([\d.]+)em[^}]*font-size: (\d+)px", css)
    assert born
    assert height + 2 * margin <= float(born[1]) * int(born[2])


def test_on_a_phone_a_state_badge_is_tight_enough_for_ready_to_watch_to_fit_a_poster() -> None:
    css = (CSS / "_watch_browse.css").read_text()
    rule = re.search(
        r"@media \(max-width: 599\.98px\) \{ \.watch-badge\.watch-badge--state \{([^}]*)\} \}", css
    )
    assert rule
    pad = int(re.search(r"padding-inline: (\d+)px", rule[1])[1])
    gap = int(re.search(r"gap: (\d+)px", rule[1])[1])
    assert 2 * pad + gap <= 12
