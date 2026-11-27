"""The pane over Watch's stylesheet (_you.css) and where Watch puts the pane (watch.html)."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[3] / "src/mediaman/web"
CSS = re.sub(r"/\*.*?\*/", "", (WEB / "static/css/_you.css").read_text(), flags=re.S)
# The rules outside any at-rule: those that hold at every size and in full motion.
TOP = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", CSS)
WATCH = (WEB / "templates/watch.html").read_text()


def _block(at: str) -> str:
    """The body of the at-rule that starts with *at*."""
    start = CSS.index(at + " {") + len(at) + 2
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(CSS[i], 0)
        i += 1
    return CSS[start : i - 1]


def _rules(css: str) -> dict[str, str]:
    return {sel.strip(): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)}


def test_with_reduced_motion_the_pane_and_its_views_only_fade_on_the_fast_duration() -> None:
    calm = _rules(_block("@media (prefers-reduced-motion: reduce)"))
    moving = [sel for sel in calm if "transition" in calm[sel]]
    assert {".you-view", ".you-view.is-on"} <= set(moving)
    assert any(".you__panel" in sel and ".you__veil" in sel for sel in moving)
    for sel in moving:
        for part in re.findall(r"transition:([^;]*);", calm[sel])[0].split(","):
            prop, duration = part.split()[:2]
            assert prop in {"opacity", "visibility"}, (sel, part)
            assert duration in {"var(--dur-fast)", "0s"}, (sel, part)
    # the page's own reduced motion cuts every transition to nothing; the pane's fade must win over it
    for sel in moving:
        assert "!important" in calm[sel], sel
    # what slides in full motion stands still
    assert "transform: none" in calm[".you-view"]
    assert all("transform: none" in calm[sel] for sel in moving if ".you__panel" in sel)


def test_every_motion_of_the_pane_runs_on_the_app_s_tokens() -> None:
    durations = re.findall(r"transition:([^;]*);", CSS)
    for value in durations:
        for part in value.split(","):
            assert re.search(r"var\(--dur(-fast|-slow)?\)|\b0s\b|^\s*none\s*$", part), part


def test_on_a_phone_the_pane_is_a_sheet_from_the_foot_with_a_handle_the_finger_owns() -> None:
    phone = _rules(_block("@media (max-width: 699.98px)"))
    sheet = phone[".you__panel, .you.is-wide .you__panel"]
    assert "inset: auto 0 0" in sheet and "translateY(100%)" in sheet
    assert "touch-action: none" in phone[".you__grab"]
    assert "touch-action: none" in phone[".you__bar"]
    assert "transition: none" in phone[".you__panel.is-dragged"]


def test_the_pane_stands_over_the_page_and_under_a_question_or_a_toast() -> None:
    assert "z-index: var(--z-modal)" in _rules(CSS)[".you"]


def test_watch_loads_the_pane_s_script_before_the_one_that_starts_the_address() -> None:
    assert WATCH.index("watch/address.js") < WATCH.index("watch/you.js") < WATCH.index("/watch.js")
    assert "_you.css" in WATCH
    assert '{% include "_you.html" %}' in WATCH.split("{% block overlays %}")[1]


def test_each_view_is_laid_out_at_the_width_it_ends_at_so_nothing_reflows_while_the_panel_grows() -> (
    None
):
    rules = _rules(TOP)
    assert "container-type: inline-size" in rules[".you"]
    for sel, w in ((".you-view", "--you-narrow"), (".you-view[data-wide]", "--you-wide")):
        assert re.search(r"--view-w: var\(" + w + r"\)", rules[sel]), sel
    view = rules[".you-view"]
    assert "100cqw" in view and "var(--view-w)" in view
    # pinned to the panel's right edge, where it stays as the panel grows to the left
    assert "right: 0" in view and "left: auto" in view
    # a phone's sheet is the screen's width at every step
    assert "left: 0" in _rules(_block("@media (max-width: 699.98px)"))[".you-view"]


def test_back_fades_by_a_class_the_page_s_hidden_rule_cannot_cut() -> None:
    rules = _rules(TOP)
    off = rules[".you__back.is-off"]
    assert "opacity: 0" in off and "visibility: hidden" in off
    assert "opacity var(--dur-fast)" in rules[".you__back"]
    template = (WEB / "templates/_you.html").read_text()
    back = re.search(r"<button[^>]*data-you-back[^>]*>", template).group(0)
    assert "is-off" in back and "hidden" not in back


def test_dragging_the_sheet_by_its_bar_or_handle_selects_no_text() -> None:
    rules = _rules(TOP)
    for sel in (".you__bar", ".you__grab"):
        assert "user-select: none" in rules[sel], sel
        assert "-webkit-user-select: none" in rules[sel], sel


def test_a_question_over_the_pane_puts_its_answers_side_by_side_with_discard_in_red() -> None:
    rules = _rules(TOP)
    acts = rules[".you.is-open ~ .ui-confirm-overlay .ui-confirm-actions"]
    assert "flex-direction: row" in acts and "flex-wrap: wrap" in acts
    assert "width: auto" in rules[".you.is-open ~ .ui-confirm-overlay .ui-confirm-actions .btn"]
    red = rules[".you.is-open ~ .ui-confirm-overlay .btn--danger"]
    assert "var(--danger-solid)" in red


def _at(value: str, d: int) -> float:
    """A calc() of --dir worked out for a view whose --dir is *d*, its units dropped."""
    expr = re.sub(r"var\(--dir(?:, 1)?\)", str(d), value).replace("calc", "").replace("%", "")
    assert re.fullmatch(r"[\d\s.+\-*/()]+", expr), value
    return float(eval(expr))


def test_a_step_pushes_the_new_view_over_the_old_which_slips_a_little_way_under_and_fades() -> None:
    view = _rules(TOP)[".you-view"]
    # opaque, so the two views never show through each other
    assert "background: var(--s1)" in view
    shift = re.search(r"transform: translateX\((.*)\);", view).group(1)
    z = re.search(r"z-index: (.*);", view).group(1)
    fade = re.search(r"opacity: (.*);", view).group(1)
    # the deeper view, on the right (--dir 1), comes or goes the full width, on top and opaque
    assert _at(shift, 1) == 100
    assert _at(fade, 1) == 1
    # the shallower, on the left (--dir -1), slips a short way under it and fades out
    assert -40 <= _at(shift, -1) <= -20
    assert _at(fade, -1) == 0
    assert _at(z, 1) > _at(z, -1)


def test_the_pane_comes_on_the_push_curve_leaves_on_the_exit_curve_and_fades_once_under_way() -> (
    None
):
    rules = _rules(TOP)
    opening = rules[".you.is-open .you__panel"]
    closing = rules[".you__panel"]
    assert "transform var(--dur-slow) var(--ease-push)" in opening
    assert "transform var(--dur) var(--ease-exit)" in closing
    # the panel fades only once it is under way, so it does not hang over Watch half seen
    assert re.search(
        r"opacity calc\(var\(--dur[^)]*\) \* [.\d]+\) var\(--ease-exit\) calc\(", closing
    )
    phone = _rules(_block("@media (max-width: 699.98px)"))
    assert "var(--ease-exit)" in phone[".you__panel, .you.is-wide .you__panel"]
    assert "var(--ease-push)" in phone[".you.is-open .you__panel"]
    # a step moves both views on the push curve, the one the bar's words move on (watch/you.js)
    for sel in (".you-view", ".you-view.is-on"):
        assert "transform var(--dur-slow) var(--ease-push)" in rules[sel], sel


def test_the_push_and_exit_curves_are_tokens_of_their_own() -> None:
    tokens = (WEB / "static/css/_tokens.css").read_text()
    assert re.search(r"--ease-push:\s*cubic-bezier\(\.32,\s*\.72,\s*0,\s*1\)", tokens)
    assert re.search(r"--ease-exit:\s*cubic-bezier\(\.4,\s*0,\s*1,\s*1\)", tokens)


def _press_after_hover(path: str) -> list[str]:
    """The :active rules of *path* that come before the hover rule of the same thing, which would then
    win over them under a desktop's mouse."""
    css = re.sub(r"/\*.*?\*/", "", (WEB / path).read_text(), flags=re.S)
    early = []
    for m in re.finditer(r"([^{}]*:active[^{}]*)\{", css):
        for sel in re.split(r",(?![^()]*\))", m.group(1)):
            hover = css.find(sel.strip().replace(":active", ":hover"))
            if hover > m.start():
                early.append(sel.strip())
    return early


def test_a_press_lights_on_a_desktop_too_as_it_comes_after_the_hover_rule() -> None:
    assert _press_after_hover("static/css/_profile.css") == []
    assert _press_after_hover("static/css/_you.css") == []
    you = _rules(TOP)
    assert any(":active" in sel and ".you__back" in sel for sel in you)
    assert any(":active" in sel and ".you__close" in sel for sel in you)


def test_while_the_panel_changes_width_a_frame_is_drawn_in_its_place_and_its_own_box_is_clear() -> (
    None
):
    rules = _rules(TOP)
    held = rules[".you.is-sizing .you__panel"]
    assert "--you-w: var(--you-wide)" in held
    assert "background: transparent" in held and "box-shadow: none" in held
    assert "border-color: transparent" in held
    assert "display: block" in rules[".you.is-sizing .you__frame.is-on"]
    # the fill shrinks or grows from the right, the left edge only moves
    assert "transform-origin: right" in rules[".you__frame-fill"]
    assert "border-radius: 20px 0 0 20px" in rules[".you__frame-edge"]
    assert "border-radius: 0 20px 20px 0" in rules[".you__frame-end"]


def test_the_pane_moves_only_by_transform_and_opacity_never_by_its_width() -> None:
    for value in re.findall(r"transition:([^;]*);", CSS):
        for part in value.split(","):
            prop = part.split()[0]
            assert prop in {
                "opacity",
                "transform",
                "visibility",
                "background-color",
                "color",
                "none",
            }, part


def test_while_profile_loads_the_wait_is_drawn_as_the_hub() -> None:
    rules = _rules(TOP)
    head = rules['.you[data-wait="profile"] .you__wait > .skel:first-child']
    assert "height: 76px" in head and "radial-gradient" in head
