"""The settings page scripts keep rows, forms and the save bar in step with what people do.

The real scripts run under node against a small fake DOM, so these check what
the page ends up showing, not how the scripts are written.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mediaman.app_factory import _TEMPLATE_DIR

HARNESS = Path(__file__).parent / "js" / "settings_harness.js"


def _run(scenario: str, *args: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario, *args], capture_output=True, text=True, check=False
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


def test_an_edit_made_during_a_save_stays_unsaved() -> None:
    out = _run("editDuringSave")
    unsaved = {"on": True, "button": "Save Settings", "disabled": False}
    assert out["during"] == unsaved
    assert "since" in out["status"]
    assert out["keptPlace"] is True
    # Discard waits for the reply, then goes back to what was sent
    assert (out["discardWhileSending"], out["discardAfter"]) == (True, False)
    assert out["discarded"] == "first"
    assert (out["said"], out["after"]) == ("Saved ✓", unsaved)
    assert out["quiet"] == {"on": False, "button": "Save Settings", "disabled": False}


def test_any_edit_after_a_failed_save_puts_the_bar_back_to_unsaved() -> None:
    out = _run("editAfterFailure")
    failed = out["failed"]
    assert (failed["button"], failed["error"]) == ("Try again", True)
    assert failed["status"] == "Too many saves — slow down for a moment."
    for edited in ("typed", "switched"):
        assert out[edited] == {
            "button": "Save Settings",
            "primary": True,
            "error": False,
            "status": "Edits apply to every section at once.",
        }


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


def test_a_subscriber_who_opted_out_is_called_unsubscribed() -> None:
    assert _run("subscriberStates") == ["Active", "Unsubscribed"]


def test_new_rows_land_where_the_server_lists_them() -> None:
    out = _run("insertOrder")
    assert out["subs"] == ["1", "2", "3", "4"]
    assert out["userRows"] == ["1", "2", "3", "4", "5"]
    # the accessible name starts with the visible text
    assert out["roleName"] == {"text": "Make admin", "aria": "Make admin: bob"}


def test_a_person_has_the_same_avatar_in_the_team_list_as_everywhere_else() -> None:
    people = ["viewer", "casey.lane@example.com", "Jo_de-Vries", "İnci", "ßeta", "...", "7"]
    macro = Environment(loader=FileSystemLoader(_TEMPLATE_DIR)).get_template("_avatar.html").module
    drawn = [re.search(r'class="(.+?)".*>(.*)<', str(macro.avatar(n))) for n in people]
    assert _run("avatars", json.dumps(people)) == [list(m.groups()) for m in drawn]


def test_add_user_form_that_is_sending_stays_open_with_what_was_typed() -> None:
    out = _run("busyAddForm")
    assert out == {"hidden": False, "value": "typed"}


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
    # ...and the button is scrolled into view only once the fold has ended
    assert out["scrollWhileClosing"] == 0
    assert out["scrollAfterClose"] == [{"block": "nearest", "behavior": "smooth"}]


def test_form_message_clears_when_the_person_types_again() -> None:
    out = _run("clearOnInput")
    assert out["shown"] and out["afterInput"] == ""
    assert out["nlShown"] and out["afterChange"] == ""


def test_account_message_clears_on_edit_but_not_while_sending() -> None:
    out = _run("accountClearOnInput")
    assert out["shown"] and out["afterInput"] == ""
    assert out["whileSending"] == {"text": "Updating…", "disabled": True}


def test_newsletter_panel_ignores_its_toggle_and_cancel_while_sending() -> None:
    out = _run("newsletterBusy")
    assert out["whileSending"] == {"hidden": False, "toggle": "Close", "disabled": True}
    assert out["afterwards"] == {"toggle": "Select recipients"}


def test_newsletter_is_sent_only_after_confirming_how_many_people_get_it() -> None:
    out = _run("newsletterConfirm")
    assert len(out["asked"]) == 1 and "3 people" in out["asked"][0]
    assert out["sentBefore"] == 0
    assert out["declined"] == {"sent": 0, "disabled": False}
    assert "1 person?" in out["one"]
    assert out["sent"] == [["a@x"]]
    assert out["asks"] == 3


def test_panels_say_whether_they_are_open_and_give_the_focus_back() -> None:
    out = _run("panelToggles")
    ids = ("newsletter-send-panel", "add-user-form")
    shut = [["false", i] for i in ids]
    assert out["shut"] == out["shutAgain"] == shut
    assert out["open"] == [["true", i] for i in ids]
    assert (out["taken"], out["sentFocus"], out["cancelFocus"]) == (True, True, True)
    # someone who went elsewhere during the send keeps their place
    assert out["movedOn"] is True


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


def test_an_admin_changes_someones_email_in_their_row() -> None:
    out = _run("userEmail")
    assert (out["ownHasButton"], out["shutAtFirst"]) == (False, True)
    assert out["opened"] == {"open": True, "value": "old@x.example", "focused": True}
    assert out["refused"] == {
        "open": True,
        "value": " ann@x.example ",
        "msg": "Another user has this address.",
        "focused": True,
        "sub": "old@x.example · joined 2 Jan 2026",
    }
    assert out["clearedOnInput"] == ""
    assert out["cancelled"] == {"open": True, "msg": ""}
    assert out["sending"] == {"sent": 1, "open": True, "busy": "true"}
    assert out["saved"] == {
        "open": False,
        "sub": "new@x.example · joined 2 Jan 2026",
        "focused": True,
        "sent": ["/api/users/2/email", "ann@x.example"],
        "busy": None,
    }
    assert out["again"] == out["forgotten"] == "new@x.example"
    assert out["removed"] == {
        "sub": "No email · joined 2 Jan 2026",
        "sent": ["/api/users/2/email", ""],
        "errors": [],
    }


def test_focus_comes_back_after_a_change_whose_button_took_none() -> None:
    assert _run("userEmailSavedWithoutFocus") == {
        "email": True,
        "role": True,
        "delete": True,
        "addUser": True,
    }


def test_the_password_dialog_for_a_delete_names_the_delete() -> None:
    words = _run("deleteWords")
    assert "delete bob" in words["title"]
    assert "delete" in words["confirmLabel"] and "save" not in json.dumps(words)


def test_a_form_shut_by_a_click_hands_the_focus_to_what_opened_it() -> None:
    assert _run("shutWithoutFocus") == {
        "emailCancel": True,
        "emailAgain": True,
        "addCancel": True,
        "addAgain": True,
    }


def test_library_count_never_says_more_are_selected_than_plex_has() -> None:
    assert _run("libraryCount") == {
        "unreachable": "3 selected",
        "none": "3 selected",
        "oneGone": "2 of 3 selected",
    }


def test_a_test_says_it_uses_the_saved_settings_while_its_card_is_edited() -> None:
    out = _run("testWhileUnsaved")
    plain = ["Test", "Test"]
    assert out["clean"] == out["putBack"] == out["saved"] == out["discarded"] == plain
    assert out["quiet"] == 0
    # only the card that was edited
    assert out["edited"] == out["afterTest"] == ["Test saved", "Test"]
    assert out["textKept"] is True
    assert len(out["told"]) == 1 and "Plex as last saved" in out["told"][0]


def test_a_save_sends_only_the_settings_that_were_changed() -> None:
    out = _run("changedOnly")
    # text the browser kept from before the page was left is an unsaved edit
    assert out["leftTyped"] is True
    # and a switch is saved as it is drawn
    assert out["hidden"] == "false"
    assert out["restored"] == {"scan_time": "10:00"}
    assert out["one"] == {"grace_days": 20}
    assert out["two"] == {"base_url": "http://b", "openai_model": "other"}


def test_an_edit_that_is_put_back_leaves_nothing_unsaved() -> None:
    out = _run("putBack")
    assert out["start"] is False
    for edited in ("text", "path", "pill"):
        assert out[edited] == [True, False], edited
    assert out["failed"] == [True, False, False]
    assert (out["whileSaving"], out["saved"], out["after"]) == (True, "Saved ✓", False)


def test_a_retry_after_the_password_sends_what_was_first_sent() -> None:
    out = _run("retryAfterPassword")
    assert out["sent"] == [["1", "2"], ["1", "2"]]
    # what was chosen while the dialog was open is still unsaved
    assert out["unsaved"] is True
    assert out["discarded"] == ["1", "2"]


def test_a_library_loses_its_threshold_only_when_the_person_clears_it() -> None:
    out = _run("diskRows")
    a, b = ["/a", "80"], ["/b", "60"]
    assert out["firstSave"] == {"1": {"path": "/a", "threshold": 80}}
    assert out["afterAnotherPill"] == a
    assert out["typedBeforeSaving"] == out["selectedAgain"] == b
    assert out["restored"] == a
    assert out["discarded"] == [["", "0"], b]
    assert out["afterFailure"] == ["/new", "50"]
    assert out["late"] == a
    # nothing is sent of a library whose fields were not changed
    assert out["deselectedSave"] == {"2": {"path": "/b", "threshold": 60}}
    assert out["clearedSave"] == {"1": {"path": "", "threshold": 0}}
    assert out["afterFailureSave"] == {"2": {"path": "/new", "threshold": 50}}
    for untouched in (
        "afterAnotherPillSave",
        "discardedSave",
        "beforeListSave",
        "unreadableSave",
        "unreachableSave",
    ):
        assert out[untouched] is None


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
