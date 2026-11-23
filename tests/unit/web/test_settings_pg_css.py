"""The settings page's phone layout holds still: nothing beside a changing label or message moves.

These read the stylesheet rules that carry that, since no layout engine runs here.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).parents[3] / "src" / "mediaman" / "web" / "static" / "css"
PANELS = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg_panels.css").read_text(), flags=re.S)


def _declarations(selector: str) -> str:
    """Every declaration block whose selector list ends in `selector`, joined."""
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", PANELS)
    return " ".join(
        body for head, body in blocks if any(part.strip() == selector for part in head.split(","))
    )


def test_form_message_line_is_never_hidden_so_nothing_below_jumps() -> None:
    assert "display: none" not in _declarations(
        ".setg-pg .inline-form-actions .inline-form-msg:empty"
    )
    assert re.search(r"min-height:\s*1\.4em", _declarations(".setg-pg .inline-form-msg"))
    assert "line-height: 1.4" in _declarations(".setg-pg .inline-form-msg")


def test_fold_wrapper_may_shrink_below_its_content() -> None:
    assert "min-width: 0" in _declarations(".setg-pg .setg-fold-inner")


def test_user_actions_stay_on_one_line_and_buttons_keep_a_width() -> None:
    assert "flex-wrap: nowrap" in _declarations(".setg-pg .usr-row .usr-actions")
    assert "min-width" in _declarations('.setg-pg .usr-row [data-user-action="delete"]')
    assert "min-width" in _declarations('.setg-pg .usr-row [data-user-action="role"]')


def test_buttons_whose_label_changes_have_a_minimum_width() -> None:
    assert "min-width" in _declarations(".setg-pg .btn-test[data-test-service]")
    assert "min-width" in _declarations(".setg-pg #btn-send-newsletter")


def test_save_error_stays_visible_on_phones() -> None:
    phone = PANELS[PANELS.index("max-width: 699.98px") :]
    assert ".setg-pg .savebar:not(.is-error) .sb-sub" in phone
    assert ".setg-pg .savebar.is-error .sb-sub" in phone
    assert "min-height: 44px" in phone


def test_small_buttons_are_44px_on_phones() -> None:
    phone = PANELS[PANELS.rindex("max-width: 639.98px") :]
    assert ".setg-pg .btn--sm" in phone
    assert "min-height: 44px" in phone


def test_integration_chevron_uses_the_motion_tokens() -> None:
    chev = _declarations(".setg-pg .intg-card-hd .intg-chev")
    assert "var(--dur)" in chev and "var(--ease-out)" in chev


def test_scrolled_into_view_controls_clear_the_sticky_bars() -> None:
    css = re.sub(r"/\*.*?\*/", "", (CSS / "_settings_pg.css").read_text(), flags=re.S)
    rules = re.findall(r"html:has\(\.setg-pg\)\s*\{([^{}]*)\}", css)
    assert len(rules) == 2
    assert all("scroll-padding-top" in body for body in rules)
    # the phone rule reaches past the top bar and the chip strip (48 + 65 px)
    assert "48px + 65px" in rules[1]
