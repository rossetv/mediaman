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
    # the notice has a class the phone layout shows it by, and its live region stays in the page
    assert out["partial"] == [True, False]
    assert out["emptied"] is False
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


def test_the_newsletter_buttons_keep_the_focus_after_a_click_that_gave_them_none() -> None:
    assert _run("newsletterWithoutFocus") == dict.fromkeys(("open", "close", "add", "next"), True)


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


def test_a_role_change_or_delete_is_sent_once_from_a_button_that_keeps_the_focus() -> None:
    once = {"dropped": False, "busy": "true", "runs": 1, "again": None, "label": True}
    assert _run("rowActions") == {"role": once, "delete": once}


def test_a_press_just_after_a_role_change_does_not_reverse_it() -> None:
    out = _run("roleQuiet")
    assert out["quiet"] == {
        "sent": ["admin"],
        "label": "Make viewer",
        "focused": True,
        "wait": [1000],
    }
    assert out["later"] == ["admin", "viewer"]
    # a held Enter presses the button once
    assert (out["held"], out["first"], out["tab"]) == (True, False, False)


def test_the_password_dialog_for_a_delete_names_the_delete() -> None:
    words = _run("deleteWords")
    assert "delete bob" in words["title"]
    assert words["confirmVariant"] == "danger"
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
    # with the selection it was changed from, so the server can tell what was changed
    assert out["bases"] == [["1"], ["1"]]
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


def test_thresholds_stored_as_no_object_do_not_stop_the_page_saving() -> None:
    out = _run("strayThresholds")
    saved = {"base_url": "b", "disk_thresholds": {"1": {"path": "/a", "threshold": 80}}}
    assert out == {"odd": saved, "5": saved, "true": saved}


def test_what_a_failed_save_may_have_stored_stays_unsaved_until_one_is_answered() -> None:
    out = _run("unanswered")
    unsaved, refused = [True, True], [False, False]
    assert out["on"] == {"undefined": unsaved, "0": unsaved, "503": unsaved, "422": refused}
    assert "may have been stored" in out["says"][0]
    # with the class by which phones show it
    assert out["says"][1] is True
    assert out["refused"] is True
    assert out["next"] == {"base_url": "a"}
    assert (out["answered"], out["forgotten"]) == (False, False)


def test_an_undo_never_overrides_what_another_page_has_set_since() -> None:
    out = _run("undo")
    library, limit, higher, days = (
        out[c] for c in ("otherLibrary", "otherLimit", "otherHigher", "otherDays")
    )
    assert (library["sent"], library["pills"]) == ([], ["4"])
    assert (limit["sent"], limit["limit"]) == ([], ["/a", "90"])
    # what else was edited is still sent
    assert (higher["sent"], higher["limit"]) == ([{"base_url": "b"}], ["/a", "95"])
    assert (days["sent"], days["days"][2]) == ([], "30")
    # a value that lets more be deleted than the one put back is the other page's all the same
    lower, address = out["otherLowerDays"], out["otherAddress"]
    assert (lower["sent"], lower["days"][2]) == ([], "10")
    assert (address["sent"], address["url"]) == ([], "c")
    for case, name in (
        (library, "L3"),
        (limit, "L1"),
        (higher, "L1"),
        (days, "Grace days"),
        (lower, "Grace days"),
        (address, "Base url"),
    ):
        assert name in case["told"]
        assert case["on"] is False


def test_an_undo_of_a_save_that_never_arrived_sends_nothing() -> None:
    out = _run("undo")["notStored"]
    assert (out["sent"], out["told"], out["on"]) == ([], "", False)
    assert (out["pills"], out["limit"], out["days"][2], out["url"]) == (
        ["1"],
        ["/a", "80"],
        "14",
        "a",
    )


def test_an_undo_that_lets_no_more_be_deleted_is_sent_over_what_the_lost_save_stored() -> None:
    out = _run("undo")["undone"]
    (sent,) = out["sent"]
    assert sorted(sent.pop("plex_libraries")) == ["1", "4"]
    assert sorted(sent.pop("plex_libraries_base")) == ["1", "3", "4"]
    assert sent == {
        "grace_days": 14,
        "base_url": "a",
        "disk_thresholds": {"1": {"path": "/a", "threshold": 80}},
    }
    # 4 was selected by another page
    assert (out["pills"], out["told"], out["on"]) == (["1", "4"], "", False)


def test_an_undo_that_lets_more_be_deleted_is_left_to_the_person() -> None:
    out = _run("undo")
    safer = out["safer"]
    assert (safer["sent"], safer["on"]) == ([], False)
    assert (safer["pills"], safer["limit"], safer["days"]) == (["1"], ["/a", "85"], ["30"] * 3)
    for name in ("L3", "L1", "Grace days", "Min age days", "Inactivity days"):
        assert name in safer["told"]
    # no limit, or one with no path, lets everything be deleted
    for case in ("toBlank", "toNoPath", "unreadable"):
        assert (out[case]["sent"], out[case]["limit"]) == ([], ["/a", "80"]), case
        assert "L1" in out[case]["told"]
    assert out["chosenAgain"] == {
        "plex_libraries": ["4", "3"],
        "plex_libraries_base": ["4"],
    }
    assert out["oldValueChosen"] == {"grace_days": 14}


def test_a_stored_value_its_field_cannot_hold_leaves_nothing_unsaved() -> None:
    out = _run("undo")["held"]
    assert (out["sent"], out["url"], out["on"]) == ([], "", False)


def test_an_edit_a_lost_save_sent_that_is_not_put_back_is_sent_again() -> None:
    out = _run("undo")["kept"]
    (sent,) = out["sent"]
    assert sorted(sent.pop("plex_libraries")) == ["1", "3", "4"]
    assert sorted(sent.pop("plex_libraries_base")) == ["1", "4"]
    assert sent == {
        "grace_days": 20,
        "base_url": "b",
        "disk_thresholds": {"1": {"path": "/a", "threshold": 85}},
    }
    assert (out["told"], out["on"]) == ("", False)


def test_an_undo_whose_own_save_is_lost_still_gives_way_to_another_page() -> None:
    out = _run("undo")["undoLost"]
    assert (out["sent"], out["days"][2], out["on"]) == ([], "30", False)
    assert "Grace days" in out["told"]


def test_nothing_is_saved_while_the_server_cannot_be_asked_about_a_lost_save() -> None:
    out = _run("undo")["unknown"]
    assert (out["sent"], out["days"][2], out["on"], out["error"]) == ([], "14", True, True)
    assert "nothing was saved" in out["says"]


def test_what_is_typed_while_the_server_is_asked_about_a_lost_save_stays_unsaved() -> None:
    out = _run("undo")["meanwhile"]
    assert out["sent"] == [{"base_url": "b"}]
    assert (out["limit"], out["url"], out["on"]) == (["/mid ", "0"], "typing", True)
    # beside what the server has
    assert (out["pills"], out["days"][2]) == (["1", "2"], "30")


def test_discard_after_a_failed_save_goes_back_to_what_the_server_has() -> None:
    out = _run("discardUnanswered")
    assert out["notStored"] == {
        "on": False,
        "field": "a",
        "pills": ["1"],
        "limit": ["", "0"],
        "toasts": 0,
        "puts": 1,
    }
    assert out["stored"] == {
        "on": False,
        "field": "b",
        "pills": ["1", "2"],
        "limit": ["/a", "80"],
        "toasts": 1,
        "puts": 1,
    }
    for name in ("Base url", "scanning of L2", "disk threshold of L1"):
        assert name in out["told"]
    assert out["forgotten"] is False
    assert out["after"] == [True, {"base_url": "a"}]
    # nothing is thrown away while the server cannot be asked
    assert out["unknown"] == {
        "on": True,
        "field": "b",
        "pills": ["1", "2"],
        "limit": ["/a", "80"],
        "toasts": 0,
        "puts": 1,
    }
    assert out["error"] is True


def test_a_secret_a_failed_save_may_have_stored_is_named_when_it_cannot_be_put_back() -> None:
    out = _run("unansweredSecret")
    ((told,), puts) = out["putBack"]
    assert told.count("Plex token") == 1 and puts == 0
    assert out["savedAgain"] == [{"plex_token": "new"}, ""]
    assert out["discarded"][0] == "****" and "Plex token" in out["discarded"][1][0]
    assert out["gone"] == ["", 1]


def test_the_bar_does_not_call_saved_changes_unsaved() -> None:
    out = _run("savedWords")
    assert out["before"] == "Unsaved changes"
    assert (out["saved"]["msg"], out["saved"]["button"]) == ("No unsaved changes", "Saved ✓")
    # with nothing to discard, Discard does not ask
    assert out["discardDisabled"] is True
    assert (out["edited"]["msg"], out["edited"]["button"]) == ("Unsaved changes", "Save Settings")
    assert out["gone"] == [False, "Unsaved changes", False]


def test_a_save_that_left_an_undo_out_says_so_in_the_bar() -> None:
    out = _run("keptWords")
    kept = out["kept"]
    assert kept["button"] == "Save Settings"
    # the bar names it, and no toast says it again
    assert kept["status"] == "Saved. Grace days was left as it was stored."
    # with the class by which phones show it
    assert (kept["msg"], kept["partial"]) == ("No unsaved changes", True)
    assert (kept["on"], kept["told"]) == (True, 0)
    assert out["gone"] == [False, False]
    assert "since" in out["edited"] and "Grace days was left" in out["edited"]
    # where the save then failed, the bar says that and the toast names it
    assert "try again" in out["failed"]["status"].lower()
    assert len(out["failed"]["told"]) == 1 and "Grace days" in out["failed"]["told"][0]
    assert out["still"] is True
    assert out["beside"] == 0


def test_what_was_left_as_stored_is_told_once_when_an_edit_ends_the_bar_that_says_it() -> None:
    out = _run("keptWords")
    for after in (out["early"], out["undone"]):
        assert "as it was stored" not in after["status"]
        assert len(after["told"]) == 1 and "Grace days" in after["told"][0]
    assert (out["early"]["on"], out["undone"]["on"]) == (True, False)
    assert out["earlyOnce"] == 1


def test_a_save_gives_the_focus_back_when_it_is_answered_and_not_when_the_bar_goes() -> None:
    out = _run("keptWords")
    assert out["focusAtAnswer"] is True
    assert out["hidden"] == {"on": False, "focus": True, "told": 0}


def test_an_emptied_secret_is_removed_only_after_confirming() -> None:
    out = _run("clearSecret")
    removed = out["removed"]
    assert len(removed["asked"]) == 1 and "Plex Token" in removed["asked"][0]
    assert removed["atAsk"] == {"button": "Save Settings", "sent": 0}
    assert removed["sent"] == [{"plex_token": "__CLEAR__"}]
    assert (removed["field"], removed["on"]) == ("", False)
    assert out["after"] == {"base_url": "b"}
    # said no: the field shows the stored secret again
    assert out["kept"] == {
        "asked": 1,
        "sent": 0,
        "field": "****",
        "on": False,
        "disabled": False,
        "focus": True,
    }
    assert out["keptRest"] == {
        "sent": [],
        "field": "****",
        "url": "b",
        "on": True,
        "disabled": False,
    }
    assert out["then"] == {"base_url": "b"}


def test_removing_several_secrets_names_them_and_what_stops_in_one_sentence_each() -> None:
    assert _run("clearSecret")["several"] == (
        "mediaman will delete the saved Plex Token and OMDB API Key. "
        "What uses them stops working until you enter them again."
    )


def test_the_bar_does_not_answer_while_a_confirmed_removal_is_sent() -> None:
    assert _run("clearSecret")["removing"] == {
        "sent": 1,
        "save": True,
        "discard": True,
        "label": "Saving…",
        "labelAfterEdit": "Saving…",
    }


def test_keeping_a_secret_the_server_still_has_ends_the_doubt_of_its_lost_removal() -> None:
    out = _run("clearSecret")
    assert out["keptAfterLost"] == {"field": "****", "on": False, "sent": 0}
    # but what a lost replacement stored is as unknown as before
    assert out["keptAfterLostReplacement"] == {"field": "****", "on": True, "sent": 0}


def test_an_edit_inside_the_mask_never_replaces_the_saved_secret() -> None:
    out = _run("secretMask")
    for typed, after in out["sent"].items():
        assert (after["sent"], after["field"], after["on"]) == ([], "****", False), typed
        assert "Plex Token stays" in after["says"] or "for Plex Token" in after["says"], typed
        # what was typed beside the mask breaks the rule; asterisks alone are nothing new
        assert ("Nothing new" in after["says"]) is (not typed.strip("*")), typed
        assert ("cannot start or end with ****" in after["says"]) is bool(typed.strip("*")), typed
    rest = out["rest"]
    assert (rest["sent"], rest["field"], rest["on"]) == ([{"base_url": "b"}], "****", False)
    assert "Plex Token" in rest["says"]
    assert out["real"] == [{"plex_token": "a*b*"}]


def test_a_mask_typed_where_no_secret_is_stored_is_refused_by_the_rule() -> None:
    for typed, after in _run("secretMask")["none"].items():
        assert (after["sent"], after["field"]) == ([], ""), typed
        assert "cannot start or end with ****" in after["says"], typed
        assert "Nothing new" not in after["says"], typed


def test_the_mask_of_a_secret_is_selected_as_a_whole() -> None:
    out = _run("secretMask")
    assert out["focused"] == out["moved"] == [0, 4]
    # it selects nothing that is selected already, and leaves what was typed alone
    assert out["again"] == 0
    assert out["typed"] == [3, 3]


def test_a_secret_of_only_spaces_counts_as_emptied() -> None:
    spaces = _run("clearSecret")["spaces"]
    assert spaces == {"asked": 1, "sent": [{"plex_token": "__CLEAR__"}], "on": False}


def test_a_secret_saved_on_this_page_can_be_emptied_and_removed() -> None:
    typed = _run("clearSecret")["typed"]
    assert typed == {"asked": 1, "sent": [{"plex_token": "new"}, {"plex_token": "__CLEAR__"}]}


def test_a_removed_secret_is_not_said_to_be_put_back() -> None:
    lost = _run("clearSecret")["lost"]
    assert (lost["sent"], lost["field"]) == ([], "")
    assert "Plex Token" in lost["told"]


def test_a_save_checks_again_only_the_services_it_changed() -> None:
    out = _run("recheck")
    failed = ["err", "Connection failed"]
    assert out["written"] == [
        ["sonarr?auto=1", "nzbget?auto=1"],
        ["ok", "Connected"],
        failed,
        failed,
    ]
    assert out["elsewhere"] == out["refused"] == []
    assert out["emptied"] == [[], ["off", "Not configured"]]


def test_only_checks_nobody_pressed_are_marked_automatic() -> None:
    out = _run("recheck")
    assert out["atLoad"] == ["plex?auto=1", "sonarr?auto=1", "nzbget?auto=1"]
    assert out["pressed"] == ["plex"]


def test_a_service_whose_lost_save_is_settled_is_checked_again() -> None:
    out = _run("recheck")
    assert out["lost"] == [[], ["plex?auto=1"]]
    assert out["discarded"] == ["plex?auto=1"]


def test_a_service_is_checked_once_a_lost_save_is_known_and_the_rest_is_dropped() -> None:
    out = _run("recheck")
    assert out["refusedThenDiscarded"] == ["plex?auto=1"]
    assert out["refusedThenUndone"] == ["plex?auto=1"]


def test_a_check_that_a_newer_one_has_replaced_is_not_made() -> None:
    assert _run("recheck")["twice"] == ["plex?auto=1"]


def test_a_late_check_of_the_old_settings_does_not_replace_the_new_ones() -> None:
    late = _run("recheck")["late"]
    assert late == [2, ["ok", "Connected"], "Testing…"]


def test_the_page_loads_each_script_once() -> None:
    pages = [
        (_TEMPLATE_DIR / name).read_text(encoding="utf-8")
        for name in ("base.html", "settings.html")
    ]
    scripts = re.findall(r'<script src="([^"?]+)', "".join(pages))
    assert "/static/js/settings/savebar.js" in scripts and "/static/js/core/dom.js" in scripts
    assert len(scripts) == len(set(scripts))


def test_a_form_comes_into_view_once_it_has_unfolded() -> None:
    out = _run("revealed")
    assert out["email"]["unfolding"] == []
    assert out["email"]["open"] == [["user-email-2", "nearest", "smooth"]]
    assert out["add"]["open"] == [["add-user-form", "nearest", "smooth"]]
    # and again with the recipients, which come when they come
    assert out["newsletter"]["opened"] == [["newsletter-send-panel", "nearest", "smooth"]] * 2
    # with reduced motion there is nothing to wait for
    assert out["calm"]["unfolding"] == [["user-email-2", "nearest", "auto"]]


def test_a_forms_status_comes_into_view_when_it_changes() -> None:
    out = _run("revealed")
    # a refusal brings the field it puts the cursor in, then its reason
    assert [what for what, *_ in out["email"]["refused"]] == [
        "inline-form-msg",
        "inp",
        "inline-form-msg err",
    ]
    field = ["new-username", "nearest", "auto"]
    created = ["create-user-result", "nearest", "auto"]
    reason = ["new-username-why", "nearest", "auto"]
    assert out["add"]["tooShort"] == [field, reason]
    assert out["add"]["refused"] == [created, field, reason]
    # Safari would scroll to the field later, and the reason out of view
    assert out["add"]["focus"] == {"preventScroll": True}
    sent = ["newsletter-send-status", "nearest", "auto"]
    assert out["newsletter"]["sending"] == [sent]
    assert out["newsletter"]["none"] == [sent]


def test_a_late_answer_comes_into_view_only_for_a_reader_still_there() -> None:
    out = _run("revealed")
    # a button clicked in Safari takes no focus: what is around it has it
    assert out["there"] == out["around"] == {"created": 1, "sent": 1, "listed": 1}
    for away in ("elsewhere", "above", "underTheBottomBar"):
        assert out[away] == {"created": 0, "sent": 0, "listed": 0}, away


def test_a_save_shows_what_another_page_stored_and_keeps_what_is_being_typed() -> None:
    out = _run("mergedReply")
    assert out["quiet"] == {
        "pills": ["1", "3"],
        "rows": [["/a", "60"], None, ["/c", "50"], None],
        "on": False,
    }
    assert out["busy"] == {
        "pills": ["1", "2", "4"],
        "rows": [["/typed ", "60"], ["/b", "70"], None, ["", "0"]],
        "on": True,
    }
    assert out["sameCard"] is True
    # the next save says what this page changed from what is stored now
    assert out["next"] == {
        "plex_libraries": ["1", "2", "4"],
        "plex_libraries_base": ["1", "2"],
        "disk_thresholds": {"1": {"path": "/typed", "threshold": 60}},
    }
    assert out["discarded"] == [["1", "2", "4"], ["/b", "70"]]


def test_unreadable_thresholds_can_be_mended_from_the_page() -> None:
    out = _run("mendThresholds")
    assert out["refused"] == {"on": True, "says": "why it was refused"}
    assert out["sent"] == {
        "disk_thresholds": {"1": {"path": "/a", "threshold": 80}},
        "plex_libraries": ["1"],
        "plex_libraries_base": ["1", "9"],
    }
    assert out["on"] is False


def test_typing_the_shown_number_over_an_unreadable_limit_brings_up_the_bar_and_can_be_saved() -> (
    None
):
    out = _run("unreadableLimit")
    assert (out["start"]["on"], out["over"]["on"]) == (False, True)
    assert "t be read" in out["start"]["says"] and "t be read" not in out["over"]["says"]
    assert out["sent"] == {"disk_thresholds": {"1": {"path": "/a", "threshold": 9}}}
    assert out["saved"]["on"] is False and "t be read" not in out["saved"]["says"]


def test_the_bar_goes_when_the_typing_over_an_unreadable_limit_leaves_none_to_save() -> None:
    out = _run("unreadableLimit")
    for left in ("undone", "blank", "zero"):
        assert out[left]["on"] is False, left
        assert "t be read" in out[left]["says"]


def test_a_path_waiting_for_its_limit_outlives_a_save_of_something_else() -> None:
    out = _run("unreadableLimit")
    assert out["otherSent"] == {"base_url": "b"}
    for look in (out["draft"], out["kept"]):
        assert (look["on"], look["shows"]) == (False, ["/new", "9"])
        assert "path isn’t saved until you enter a limit" in look["says"]
    assert out["entered"]["on"] is True
    assert out["enteredSent"] == {"disk_thresholds": {"1": {"path": "/new", "threshold": 9}}}
    assert (out["stored"]["on"], out["stored"]["shows"]) == (False, ["/new", "9"])
    assert out["edited"] == {"disk_thresholds": {"1": {"path": "/new", "threshold": 50}}}
    assert out["cleared"] == {"disk_thresholds": {"1": {"path": "", "threshold": 0}}}
    assert out["end"]["on"] is False


def test_a_limit_typed_during_a_save_outlives_a_library_chosen_during_it() -> None:
    out = _run("pillDuringSave")
    assert out["shown"] == ["/a", "80"]
    assert out["unsaved"] is True
    assert out["next"] == {
        "disk_thresholds": {"1": {"path": "/a", "threshold": 80}},
        "plex_libraries": ["1", "2"],
        "plex_libraries_base": ["1"],
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
