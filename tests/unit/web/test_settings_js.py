"""The settings page scripts keep rows, forms and the save bar in step with what people do.

The real scripts run under node against a small fake DOM, so these check what
the page ends up showing, not how the scripts are written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "settings_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_leaving_row_goes_when_its_height_closes_and_hands_focus_on() -> None:
    out = _run("leave")
    # the opacity fade ending first must not remove the row
    assert out["afterOpacity"] == {"present": True, "done": 0}
    assert out["afterHeight"] == {"present": False, "done": 1}
    assert out["focusOnNext"] is True
    # with no transition event at all the row still goes, and focus goes to the row before
    assert out["fallbackRemoved"] is True
    assert out["focusOnPrev"] is True


def test_discard_puts_back_the_last_saved_values() -> None:
    out = _run("savebar")
    assert out["dirty"] is True
    assert out["afterDiscard"] == {"value": "http://a", "checked": True, "dirty": False}
    # a save moves the point Discard returns to
    assert out["afterSave"] == {"value": "kept", "flag": False}
    assert out["afterSaveDiscard"] == {"value": "http://c", "flag": True}


def test_subscriber_is_removed_only_after_confirming() -> None:
    out = _run("remove")
    assert out["declined"] == {
        "deletes": 0,
        "rows": ["1", "2"],
        "label": "Remove",
        "disabled": False,
    }
    assert out["working"] == {"label": "Removing…", "aria": "Removing…: a@x"}
    assert out["deletes"] == ["/api/subscribers/1"]
    assert out["rows"] == ["2"]
    assert out["toasts"] == ["Removed a@x"]


def test_new_rows_land_where_the_server_lists_them() -> None:
    out = _run("insertOrder")
    assert out["subs"] == ["1", "2", "3", "4"]
    assert out["userRows"] == ["1", "2", "3", "4", "5"]
    # the accessible name starts with the visible text
    assert out["roleName"] == {"text": "Make admin", "aria": "Make admin: bob"}


def test_form_that_is_sending_stays_open_when_another_opens() -> None:
    out = _run("busyForm")
    assert out["whileBusy"] == {"pwHidden": False, "emailHidden": False}
    # its own toggle and Cancel leave it open with what was typed
    assert out["ownButtons"] == {"hidden": False, "value": "typed"}
    # once idle, opening another closes it and puts typed values back to their start
    assert out["afterIdle"] == {"emailHidden": True, "emailValue": "me@x", "submitDisabled": False}


def test_rate_limited_service_is_not_counted_as_not_connected() -> None:
    out = _run("rail")
    assert "warn" in out["plex"].split()
    assert out["badge"] == "1"


def test_pressed_test_that_is_rate_limited_warns_and_is_not_counted() -> None:
    out = _run("manualRateLimit")
    assert "warn" in out["pill"].split()
    assert "err" not in out["pill"].split()
    assert out["label"].strip().endswith("Rate limited")
    assert out["badge"] is None


def test_new_user_row_enters_only_after_the_add_form_has_closed() -> None:
    out = _run("addUserOrder")
    assert out["afterCreate"] == {"rows": 1, "open": True}
    assert out["whileClosing"] == {"rows": 1, "shut": True, "hidden": False}
    assert out["afterClose"] == {"rows": 2, "hidden": True, "submitDisabled": False}
    # focus goes back to the button without scrolling the page to it
    assert out["refocus"] == {"onButton": True, "preventScroll": True}


def test_form_message_clears_when_the_person_types_again() -> None:
    out = _run("clearOnInput")
    assert out["shown"] and out["afterInput"] == ""
    assert out["nlShown"] and out["afterChange"] == ""


def test_account_message_clears_on_edit_but_not_while_sending() -> None:
    out = _run("accountClearOnInput")
    assert out["shown"] and out["afterInput"] == ""
    assert out["whileSending"] == {"text": "Working", "disabled": True}


def test_newsletter_panel_ignores_its_toggle_and_cancel_while_sending() -> None:
    out = _run("newsletterBusy")
    assert out["whileSending"] == {"hidden": False, "toggle": "Close", "disabled": True}
    assert out["afterwards"] == {"toggle": "Select recipients"}


def test_refused_field_is_named_by_its_label_in_a_sentence() -> None:
    text = _run("fieldError")["text"]
    assert "Base URL must start with http:// or https://." in text
    assert "must be between 1 and 365 days." in text
    # a field the page does not show falls back to its key in words
    assert "Mystery key is not valid." in text
    assert "base_url" not in text and "got" not in text


def test_role_change_updates_the_row_in_place_and_keeps_focus() -> None:
    out = _run("roleInPlace")
    assert out == {
        "sameRow": True,
        "pill": "Admin",
        "button": "Make viewer",
        "focused": True,
        "disabled": False,
    }


def test_rate_limited_test_button_says_try_later_not_failed() -> None:
    out = _run("manualRateLimit")
    assert out["button"] == "Try later"
    assert out["pill"].split()[-1] == "warn"


def test_new_pill_waits_for_the_reauth_dialog_to_fade_out() -> None:
    out = _run("roleAfterReauth")
    assert out["during"] == {"label": "Viewer", "changed": False}
    assert out["after"] == {"label": "Admin", "changed": True}


def test_integration_card_body_folds_open_and_shut() -> None:
    out = _run("integrationFold")
    assert out["fold"] and out["wrapped"]
    assert out["open"] == {"hidden": False, "shut": False, "moving": True}
    assert out["closing"] == {"hidden": False, "shut": True}
    assert out["closed"] == {"hidden": True, "expanded": "false"}
