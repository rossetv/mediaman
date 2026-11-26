"""The settings page's phone layout holds still: nothing beside a changing label or message moves.

These read the stylesheet rules that carry that, since no layout engine runs here.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "static" / "css"
PANELS = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg_panels.css").read_text(), flags=re.S)


def _declarations(selector: str, css: str = PANELS) -> str:
    """Every declaration block whose selector list ends in `selector`, joined."""
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    return " ".join(
        body for head, body in blocks if any(part.strip() == selector for part in head.split(","))
    )


def test_form_message_line_is_never_hidden_so_nothing_below_jumps() -> None:
    assert "display: none" not in _declarations(
        ".setg-pg .inline-form-actions .inline-form-msg:empty"
    )
    assert re.search(r"min-height:\s*1\.4em", _declarations(".setg-pg .inline-form-msg"))
    assert "line-height: 1.4" in _declarations(".setg-pg .inline-form-msg")


def test_a_status_brought_into_view_brings_the_button_beside_it() -> None:
    margin = re.search(
        r"scroll-margin-bottom:\s*(\d+)px", _declarations(".setg-pg .inline-form-msg")
    )
    # a 44px button centred on one 18px line reaches 13px below it
    assert margin and int(margin.group(1)) >= 13


def test_what_is_scrolled_into_view_stops_above_the_save_bar() -> None:
    css = (CSS / "_settings_pg.css").read_text()

    bar = re.search(r"html:has\(\.setg-pg \.savebar\.on\) \{([^{}]*)\}", css)
    assert bar and "--savebar-below: 0px" in bar.group(1)
    assert "scroll-padding-bottom: calc(var(--savebar-h, 73px) + var(--savebar-below)" in bar.group(
        1
    )
    # no other rule sets it to a size of its own
    assert css.count("scroll-padding-bottom") == 1
    # on a phone the bar stands on the one at the foot
    below = "var(--foot)"
    phone = css[css.index(f".setg-pg .savebar {{ bottom: {below};") :]
    assert f"html:has(.setg-pg .savebar.on) {{ --savebar-below: {below}; }}" in phone


def test_a_long_user_name_or_email_wraps_inside_its_row() -> None:
    for part in (".usr-name", ".usr-sub"):
        for at in (".usr-row", ".usr-self-head"):
            assert "overflow-wrap: anywhere" in _declarations(f".setg-pg {at} {part}")


def test_fold_wrapper_may_shrink_below_its_content() -> None:
    assert "min-width: 0" in _declarations(".setg-pg .setg-fold-inner")


def test_user_actions_stay_on_one_line_and_buttons_keep_a_width() -> None:
    assert "flex-wrap: nowrap" in _declarations(".setg-pg .usr-row .usr-actions")
    assert "min-width" in _declarations('.setg-pg .usr-row [data-user-action="delete"]')
    assert "min-width" in _declarations('.setg-pg .usr-row [data-user-action="role"]')


def test_buttons_whose_label_changes_have_a_minimum_width() -> None:
    assert "min-width" in _declarations(".setg-pg .btn-test[data-test-service]")
    assert "min-width" in _declarations(".setg-pg #btn-send-newsletter")


def _narrow_phone_rule(selector: str) -> str:
    """The last declarations under 360px for `selector`, which must come after the phone ones."""
    start = PANELS.rindex("@media (max-width: 359.98px)")
    assert start > PANELS.rindex("@media (max-width: 520px)")
    match = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", PANELS[start:])
    assert match
    return match.group(1)


def test_the_longest_subscriber_state_leaves_remove_on_its_line_on_a_narrow_phone() -> None:
    assert "margin-left: 0" in _narrow_phone_rule(".setg-pg .sub-row .sub-stat")


def test_a_service_header_starts_its_answer_row_in_the_same_place_at_every_phone_width() -> None:
    narrow = re.search(r"@media \(max-width: 359\.98px\) \{(.*?)\n\}\n", PANELS, re.S)
    assert narrow and ".hd-actions" not in narrow.group(1)


def test_a_service_header_drops_its_answer_under_a_description_of_at_least_180px() -> None:
    main = _declarations(".setg-pg .intg-card-hd .hd-main")
    assert "flex-wrap: wrap" in main
    # the row wraps by what fits in the card itself, so no screen width is named for it
    assert re.search(r"flex:\s*\d+ 1 180px", _declarations(".setg-pg .intg-card-hd .h-info"))
    assert "margin-left: auto" in _declarations(".setg-pg .intg-card-hd .hd-btns")
    header = PANELS[PANELS.index(".setg-pg .intg-card-hd .hd-main") :].split(
        "\n.setg-pg .intg-card-bd", 1
    )[0]
    assert "@media" not in header and "@container" not in header


def test_a_long_user_name_wraps_in_the_password_dialog() -> None:
    css = (CSS / "_settings_pg.css").read_text()
    title = re.search(r"\.reauth-title \{([^{}]*)\}", css)
    assert title and "overflow-wrap: anywhere" in title.group(1)


def _phone_block_with(selector: str) -> str:
    """The body of the phone `@media` block that holds a rule for `selector`."""
    for m in re.finditer(r"@media \(max-width: 699\.98px\)(?:, \(pointer: coarse\))?\s*\{", PANELS):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(PANELS[i], 0)
            i += 1
        body = PANELS[m.end() : i - 1]
        if selector in body:
            return body
    raise AssertionError(f"no phone block holds {selector}")


def test_save_error_stays_visible_on_phones() -> None:
    phone = _phone_block_with(".setg-pg .savebar.is-error .sb-sub")
    assert ".setg-pg .savebar:not(.is-error) .sb-sub" in phone
    assert ".setg-pg .savebar .btn { min-height: 44px; }" in phone


def test_save_bar_caption_is_never_taken_out_of_reach_of_a_screen_reader_on_phones() -> None:
    phone = _phone_block_with(".setg-pg .savebar.is-error .sb-sub")
    assert "display: none" not in phone
    partial = re.findall(r"([^{}]*\.savebar\.is-partial \.sb-sub[^{}]*)\{([^{}]*)\}", phone)
    # the notice that newer edits are unsaved is shown, after and over the rule that folds captions away
    assert any("position: static" in body and "clip-path: none" in body for _, body in partial)
    assert phone.index("clip-path: inset(50%)") < phone.index("clip-path: none")
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    assert not re.search(r"\.savebar \.sb-sub \{\s*display: none", css)


def test_dots_bars_and_the_current_choice_show_in_forced_colours() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    (block,) = re.findall(r"@media \(forced-colors: active\) \{((?:[^{}]*\{[^{}]*\})*)", css)
    rules: dict[str, str] = {}
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
        for part in head.split(","):
            rules[part.strip()] = rules.get(part.strip(), "") + body
    for dot in (".conn-dot", ".sb-dot", ".chip-dot"):
        assert "forced-color-adjust: none" in rules[f".setg-pg {dot}"]
        assert "solid CanvasText" in rules[f".setg-pg {dot}"]
    for bar in (".ov-storage .bar span", ".lp-bar2 span"):
        assert "forced-color-adjust: none" in rules[f".setg-pg {bar}"]
    assert "background: Highlight" in rules[".setg-pg .setg-rail-item.on"]


def test_a_selected_library_shows_in_forced_colours() -> None:
    # after the pill's own colours, which would otherwise win
    own = PANELS.index(".setg-pg .lib-pill.on")
    forced = re.search(
        r"@media \(forced-colors: active\) \{\s*\.setg-pg \.lib-pill\.on \{([^{}]*)\}", PANELS
    )
    assert forced and forced.start() > own and "background: Highlight" in forced.group(1)


def test_a_card_heading_is_not_squeezed_by_its_button() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    rules = dict(re.findall(r"([^{}]+)\{([^{}]*)\}", css))
    rules = {head.strip(): body for head, body in rules.items()}
    assert "flex-wrap: wrap" in rules[".setg-pg .setg-card-hd"]
    assert "flex: 1 1 130px" in rules[".setg-pg .setg-card-hd .head-info"]


def test_touch_screens_of_any_width_get_the_phone_targets() -> None:
    for sheet, targets in (
        ("_settings_pg_panels.css", (".btn--sm", ".link-danger")),
        (
            "_settings_pg.css",
            (".setg-rail-item", ".inp", ".lib-pill", ".fpill", ".reauth-actions .btn"),
        ),
        ("_settings.css", (".btn-test",)),
    ):
        css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(), flags=re.S)
        blocks = re.findall(r"@media [^{]*\(pointer: coarse\)[^{]*\{((?:[^{}]*\{[^{}]*\})*)", css)
        tall = [
            part.strip()
            for block in blocks
            for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block)
            if "min-height: 44px" in body
            for part in head.split(",")
        ]
        for target in targets:
            assert any(part.endswith(target) for part in tall), (sheet, target)


def test_small_buttons_are_44px_on_phones() -> None:
    phone = _phone_block_with(".setg-pg .btn--sm")
    assert re.search(r"\.setg-pg \.btn--sm,[^{}]*\{[^{}]*min-height:\s*44px", phone)


def test_integration_chevron_uses_the_motion_tokens() -> None:
    chev = _declarations(".setg-pg .intg-card-hd .intg-chev")
    assert "var(--dur)" in chev and "var(--ease-out)" in chev


def test_overview_dots_take_the_colour_of_their_service() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    coloured = {
        part.strip(): body
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        for part in head.split(",")
    }
    for tone, token in (("ok", "success"), ("warn", "warning"), ("err", "danger")):
        assert f"background: var(--{token})" in coloured[f".setg-pg .conn-dot.{tone}"]


def test_the_browsers_own_field_parts_are_drawn_for_a_dark_page() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    page = re.search(r"\.setg-pg\s*\{([^{}]*)\}", css)
    assert page and "color-scheme: dark" in page.group(1)


def test_scrolled_into_view_controls_clear_the_sticky_bars() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    rules = re.findall(r"html:has\(\.setg-pg\)\s*\{([^{}]*)\}", css)
    assert len(rules) == 2
    assert all("scroll-padding-top" in body for body in rules)
    # the phone rule reaches past the top bar and the chip strip (65 px)
    assert "var(--nav-h) + 65px" in rules[1]


def test_blocks_add_no_scroll_margin_on_top_of_the_page_scroll_padding() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if ".setg-block" in head and "-hd" not in head:
            assert "scroll-margin" not in body


def test_every_select_draws_its_own_chevron_clear_of_the_edge() -> None:
    forms = re.sub(r"/\*.*?\*/", "", (CSS / "_forms.css").read_text(), flags=re.S)
    rule = re.search(r"select\.inp,\s*\.form-select \{([^{}]*)\}", forms)
    assert rule and "appearance: none" in rule.group(1)
    assert "background-position: right 12px center" in rule.group(1)
    forced = re.search(r"@media \(forced-colors: active\) \{[^{}]*\{([^{}]*)\}", forms)
    assert forced and "appearance: auto" in forced.group(1)


def test_no_field_rule_wipes_the_select_chevron() -> None:
    for sheet in ("_forms.css", "_settings_pg.css", "_watch_list.css"):
        css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(), flags=re.S)
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            fields = [p.strip() for p in head.split(",")]
            if any(re.search(r"(\.inp|\.form-select)(:focus)?$", f) for f in fields):
                shorthand = re.search(r"(?<![\w-])background\s*:", body)
                assert not shorthand or "background-image" in body, (sheet, fields)


def test_a_switch_shows_in_forced_colours_and_no_page_redraws_its_thumb() -> None:
    forms = re.sub(r"/\*.*?\*/", "", (CSS / "_forms.css").read_text(), flags=re.S)
    blocks = re.findall(r"@media \(forced-colors: active\) \{((?:[^{}]*\{[^{}]*\})*)", forms)
    rules = {
        head.strip(): body
        for block in blocks
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block)
    }
    assert "border: 1px solid CanvasText" in rules[".tog"]
    assert "background: CanvasText" in rules[".tog::after"]
    assert "Highlight" in rules[".tog.on"] and "Highlight" in rules[".tog.on::after"]
    # A later, more specific thumb would paint over the one above.
    for sheet in CSS.glob("*.css"):
        if sheet.name != "_forms.css":
            css = re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.S)
            assert not re.search(r"\.tog(\.on)?::after", css), sheet.name


def test_an_open_fold_at_rest_is_laid_out_anew_when_its_content_grows() -> None:
    at_rest = _declarations(".setg-pg .setg-fold:not(.is-moving):not(.is-shut)")
    assert "display: flow-root" in at_rest  # Safari keeps a grid row's old height
    assert "display: grid" in _declarations(".setg-pg .setg-fold")


def _sheet(name: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (CSS / name).read_text(), flags=re.S)


def test_a_fold_with_a_margin_of_its_own_gives_it_up_as_it_shuts() -> None:
    # Kept while shut, it goes in one step when the fold is hidden at the end.
    assert "margin-bottom: -6px" in _declarations("#account-email-fold", _sheet("_profile.css"))
    assert "margin-bottom: 0" in _declarations(
        "#account-email-fold.is-shut", _sheet("_profile.css")
    )


def test_a_card_that_holds_only_a_fold_folds_its_padding_and_edge_with_it() -> None:
    css = _sheet("_settings_pg.css")
    body = ".setg-pg .setg-card-bd:has(> .setg-fold:only-child)"
    shut = ".setg-pg .setg-card-bd:has(> .setg-fold.is-shut:only-child)"
    head = ".setg-pg .setg-card-hd:has(+ .setg-card-bd > .setg-fold.is-shut:only-child)"
    assert "transition: padding var(--dur-slow) var(--ease-out)" in _declarations(body, css)
    assert "padding-block: 0" in _declarations(shut, css)
    assert "border-bottom-width: 0" in _declarations(head, css)
    assert "border-bottom-width var(--dur-slow)" in _declarations(head.replace(".is-shut", ""), css)


def test_a_panel_folds_at_the_speed_of_the_other_folds() -> None:
    fold = _declarations(".setg-pg .setg-fold")
    # MM.dom.fold opens a <details> beside it over --dur-slow, as the app's other folds open.
    assert "'--dur-slow'" in (CSS.parent / "js" / "core" / "dom.js").read_text()
    for part in ("grid-template-rows", "margin", "padding"):
        assert f"{part} var(--dur-slow)" in fold
    reduced = (
        r"prefers-reduced-motion: reduce\)\s*\{\s*\.setg-pg \.setg-fold \{ transition: none; \}"
    )
    assert re.search(reduced, PANELS)
