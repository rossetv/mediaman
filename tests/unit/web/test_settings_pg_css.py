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
    assert "min-height" in _declarations(".setg-pg .inline-form-msg")


def test_a_status_brought_into_view_brings_the_button_beside_it() -> None:
    margin = re.search(
        r"scroll-margin-bottom:\s*(\d+)px", _declarations(".setg-pg .inline-form-msg")
    )
    # a 44px button centred on one 18px line reaches 13px below it
    assert margin and int(margin.group(1)) >= 13


def test_the_save_bar_takes_its_own_room_under_the_sections_and_covers_none_of_them() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    pane = _declarations('.you-view[data-view="settings"]', css)
    assert "display: flex" in pane and "flex-direction: column" in pane
    assert "flex: 1" in _declarations(".setg-pg.stx", css)
    bar = _declarations(".stx-save", css)
    assert "flex: none" in bar and "position" not in bar
    assert "overflow-y: auto" in _declarations(".stx__body", css)
    assert "scroll-padding-bottom" not in css and "var(--foot)" not in css


def test_a_long_user_name_or_email_wraps_inside_its_row() -> None:
    for part in (".usr-name", ".usr-sub"):
        for at in (".usr-row", ".usr-self-head"):
            assert "overflow-wrap: anywhere" in _declarations(f".setg-pg {at} {part}")


def test_buttons_whose_label_changes_have_a_minimum_width() -> None:
    assert "min-width" in _declarations(".setg-pg .btn-test[data-test-service]")
    assert "min-width" in _declarations(".setg-pg #btn-send-newsletter")


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


def test_save_bar_buttons_are_a_finger_tall_on_phones() -> None:
    phone = _phone_block_with(".stx-save .btn")
    assert ".stx-save .btn { min-height: 44px; }" in phone


def test_save_bar_caption_is_never_taken_out_of_reach_of_a_screen_reader_on_phones() -> None:
    caption = (".stx-save__text", ".sb-msg", "#save-status")
    for sheet in CSS.glob("*.css"):
        css = re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.S)
        for m in re.finditer(r"@media [^{]*max-width[^{]*\{", css):
            depth, i = 1, m.end()
            while depth:
                depth += {"{": 1, "}": -1}.get(css[i], 0)
                i += 1
            for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css[m.end() : i - 1]):
                # the bar itself hides only while nothing is unsaved
                whole_bar = ".stx-save" in head and ":not(.on)" not in head
                if whole_bar or any(c in head for c in caption):
                    assert not re.search(r"display:\s*none|visibility:\s*hidden", body), (
                        sheet.name,
                        head.strip(),
                    )


def test_a_save_error_or_what_a_save_left_out_wraps_whole_and_is_never_hidden() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    for state in ("is-error", "is-partial"):
        assert "white-space: normal" in _declarations(f".stx-save.{state} #save-status", css)
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "#save-status" in head and ":empty" not in head:
            assert "display: none" not in body, head


def test_dots_bars_and_the_current_choice_show_in_forced_colours() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    (block,) = re.findall(r"@media \(forced-colors: active\) \{((?:[^{}]*\{[^{}]*\})*)", css)
    rules: dict[str, str] = {}
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
        for part in head.split(","):
            rules[part.strip()] = rules.get(part.strip(), "") + body
    for dot in (".setg-pg .conn-dot", ".stx-save__dot", ".setg-pg .chip-dot"):
        assert "forced-color-adjust: none" in rules[dot]
        assert "solid CanvasText" in rules[dot]
    for bar in (".ov-storage .bar span", ".dt-bar span"):
        assert "forced-color-adjust: none" in rules[f".setg-pg {bar}"]
    assert "background: Highlight" in rules[".stx-link[aria-current]"]


def test_a_selected_library_shows_in_forced_colours() -> None:
    # after the pill's own colours, which would otherwise win
    own = PANELS.index(".setg-pg .lib-pill.on")
    forced = re.search(
        r"@media \(forced-colors: active\) \{\s*\.setg-pg \.lib-pill\.on \{([^{}]*)\}", PANELS
    )
    assert forced and forced.start() > own and "background: Highlight" in forced.group(1)


def test_touch_screens_of_any_width_get_the_phone_targets() -> None:
    for sheet, targets in (
        ("_settings_pg_panels.css", (".btn--sm", ".link-danger")),
        (
            "_settings_pg.css",
            (".inp", ".lib-pill", ".fpill", ".reauth-actions .btn"),
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


def test_the_browsers_own_field_parts_are_drawn_for_a_dark_page() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    page = re.search(r"\.setg-pg\s*\{([^{}]*)\}", css)
    assert page and "color-scheme: dark" in page.group(1)


def test_toasts_stand_above_the_save_bar_while_it_shows_in_the_open_pane() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_ui_feedback.css").read_text(), flags=re.S)
    # savebar.js marks the page while the bar is on show in the open pane, with the bar's height
    lifted = re.findall(r"html\[data-save-bar\] \.ui-toast-host \{([^{}]*)\}", css)
    # on a wide screen the stack's foot moves up; on a phone, where it stands at the foot, too
    assert any("--toast-bottom: calc(var(--save-bar) +" in body for body in lifted)
    assert any(re.search(r"(?<!-)\bbottom: calc\(var\(--save-bar\) \+", body) for body in lifted)


def test_reduced_motion_fades_the_list_and_section_at_the_short_duration_over_the_page_rule() -> (
    None
):
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)
    assert block
    rule = _declarations(".stx.is-in > .stx__body", block.group(1))
    assert "transform: none !important" in rule
    assert "transition-property: opacity, visibility !important" in rule
    assert "transition-duration: var(--dur-fast), 0s !important" in rule


def test_every_select_draws_its_own_chevron_clear_of_the_edge() -> None:
    forms = re.sub(r"/\*.*?\*/", "", (CSS / "_forms.css").read_text(), flags=re.S)
    rule = re.search(r"select\.inp \{([^{}]*)\}", forms)
    assert rule and "appearance: none" in rule.group(1)
    assert "background-position: right" in rule.group(1)
    forced = re.search(r"@media \(forced-colors: active\) \{[^{}]*\{([^{}]*)\}", forms)
    assert forced and "appearance: auto" in forced.group(1)


def test_no_field_rule_wipes_the_select_chevron() -> None:
    for sheet in ("_forms.css", "_settings_pg.css", "_watch_list.css"):
        css = re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(), flags=re.S)
        for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            fields = [p.strip() for p in head.split(",")]
            if any(re.search(r"(\.inp)(:focus)?$", f) for f in fields):
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


def _strip(sheet: str) -> str:
    return re.sub(r"/\*.*?\*/", "", (CSS / sheet).read_text(), flags=re.S)


def test_the_monospace_settings_field_is_16px_where_touch_would_zoom() -> None:
    css = _strip("_settings_pg.css")
    base = re.search(r"^\.setg-pg \.inp--mono \{[^{}]*font-size: 13px;[^{}]*\}", css, flags=re.M)
    assert base
    # Top level and after the 13px rule, or the same-weight 13px rule wins.
    rule = re.compile(
        r"^@media \(max-width: 639\.98px\), \(pointer: coarse\) \{\s*"
        r"\.setg-pg \.inp--mono \{[^{}]*font-size: 16px[^{}]*\}\s*\}",
        flags=re.M,
    )
    assert rule.search(css, base.end())


def test_the_history_search_field_is_16px_at_every_width() -> None:
    css = _strip("_history.css")
    base = re.search(r"\n\.hx-search input \{([^{}]*)\}", css)
    assert base and "font-size: 16px;" in base.group(1)
    # no rule, at any width or pointer, makes it smaller
    for head, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if ".hx-search input" in head and "font-size" in body:
            assert "font-size: 16px" in body, head


def test_the_share_link_field_and_the_years_select_are_16px_where_touch_would_zoom() -> None:
    for sheet, field in (
        ("_watch_detail.css", r"\.ttl-share__input"),
        ("_watch_explore.css", r"\.explore-years__select"),
    ):
        css = _strip(sheet)
        pattern = (
            r"@media \(max-width: 639\.98px\), \(pointer: coarse\) \{\s*"
            + field
            + r" \{[^{}]*font-size: 16px"
        )
        assert re.search(pattern, css), sheet


def test_on_reduced_motion_a_section_arriving_beside_the_list_fades_in_150ms() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    calm = "".join(re.findall(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S))
    arriving = _declarations(".stx-sec.is-arriving", calm)
    name = re.search(r"animation-name: ([\w-]+)", arriving)
    assert name and "animation-duration: var(--dur-fast) !important" in arriving, arriving
    frames = re.search(r"@keyframes " + name.group(1) + r" \{([^}]*)\}", css)
    assert frames and re.fullmatch(r"\s*from \{ opacity: 0;?\s*", frames.group(1) + " "), frames


def test_no_class_of_a_library_card_is_styled_by_the_players_unscoped_rules() -> None:
    # The player's rules are global; one sharing a class with the card cut its library name off unseen.
    js = (CSS.parent / "js" / "settings" / "disk_thresholds.js").read_text()
    card = set(re.findall(r"className = '([\w-]+)'", js))
    player = re.sub(r"/\*.*?\*/", "", (CSS / "_player.css").read_text(), flags=re.S)
    styled = set(re.findall(r"\.([a-zA-Z][\w-]*)", "".join(re.findall(r"([^{}]+)\{", player))))
    assert card, "no card classes found"
    assert not card & styled


def test_a_long_library_name_wraps_in_its_card_rather_than_being_cut() -> None:
    name = _declarations(".setg-pg .dt-head .dt-name")
    assert "overflow-wrap: anywhere" in name
    assert "nowrap" not in name
