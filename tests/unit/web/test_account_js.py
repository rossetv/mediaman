"""account.js run under node against a fake DOM: the Profile page's email form and sign-ins."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

WEB = Path(__file__).parents[3] / "src" / "mediaman" / "web"
HARNESS = Path(__file__).parent / "js" / "account_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_live_sign_ins_are_listed_and_the_others_can_be_signed_out() -> None:
    out = _run("sessions")
    assert (out["first"]["rows"], out["first"]["disabled"]) == (2, False)
    assert "30 May," in out["first"]["text"] and "1 Jun," in out["first"]["text"]
    assert out["posts"] == 1
    assert (out["after"]["rows"], out["after"]["disabled"]) == (1, True)
    assert out["after"]["say"][0].endswith(" ok")


def test_the_sign_in_of_this_device_is_told_from_the_others() -> None:
    rows = _run("thisDevice")
    assert [("This device" in row) for row in rows] == [True, False, False]


def test_a_sign_in_names_its_device_as_text_or_has_its_dates_alone() -> None:
    here, unnamed, old = _run("thisDevice")
    assert here.startswith("Chrome on macOS") and "Signed in 30 May," in here
    assert unnamed.startswith("Signed in 30 May,") and unnamed.count("Signed in") == 1
    assert old.startswith("<b>Safari on iPhone")


def test_a_date_of_another_year_has_its_year() -> None:
    here, _unnamed, old = _run("thisDevice")
    assert "2026" not in here and "Nov 2025," in old


def test_one_device_is_signed_out_by_its_own_button() -> None:
    out = _run("signOutOne")
    assert (out["own"], out["label"]) == (False, "Sign out: Safari on iPhone")
    # one request for each press, however often it is pressed meanwhile
    assert out["sent"] == ["/api/users/sessions/a%2Fb"] * 3 + ["/api/users/sessions/id3"]
    # only a spinner at the width the button had, and the words for a screen reader
    assert out["busy"] == ["", "20px", "Signing out…"]
    assert out["labelAfter"] == out["label"]
    assert out["words"] == {
        "title": "Confirm it’s you",
        "descText": "Enter your Mediaman password to sign out a device.",
        "confirmLabel": "Continue",
    }


def test_a_device_that_would_not_sign_out_stays_and_says_so() -> None:
    out = _run("signOutOne")
    assert out["refused"] == {
        "rows": 3,
        "says": "Sign out",
        "toasts": ["error: No luck"],
        "focusKept": True,
    }
    assert out["cancelled"] == {"rows": 3, "toasts": []}


def test_a_device_signed_out_leaves_the_list_and_hands_the_focus_on() -> None:
    out = _run("signOutOne")
    assert out["gone"] == {
        "rows": 2,
        "toasts": ["Signed out Safari on iPhone."],
        "onNext": True,
        "why": False,
    }
    assert out["last"] == {
        "rows": 1,
        "focused": "account-sessions",
        "disabled": True,
        "why": True,
    }


def test_only_the_list_of_sign_ins_asked_last_is_shown() -> None:
    assert _run("lateList") == 1


def test_a_list_asked_before_a_device_was_signed_out_does_not_bring_it_back() -> None:
    assert _run("listBeforeSignOut") == 1


def test_the_password_form_opens_at_its_first_field_and_forgets_what_was_typed_when_shut() -> None:
    out = _run("passwordFold")
    assert out["shut"][:2] == [True, "false"]
    assert out["open"] == [False, "true", "account-old-password"]
    assert out["cancelled"] == [True, "false", "account-open-password", ""]
    assert out["escaped"] == [True, "false", "account-open-password"]


def test_the_rule_for_a_new_password_is_back_when_its_field_is_emptied() -> None:
    rule = "12 characters or more"
    assert _run("passwordRule") == [rule, rule + ", 3 so far", rule, rule]


def test_the_row_says_when_the_password_changed_and_once_open_leaves_the_rest_to_the_form() -> None:
    never, this_year, earlier = _run("passwordChanged")
    effect = "It signs out the others."
    assert never == [effect, effect, True]
    assert this_year == [f"14 March {effect}", "14 March", True]
    assert earlier[1] == "14 March 2025"


def test_the_form_says_how_many_devices_and_tvs_a_new_password_signs_out() -> None:
    none, one, both, tvs, no_list, no_tvs = _run("devicesLine")
    assert none == "You have no other device to sign out."
    assert one == "Signs out your other device."
    assert both == "Signs out your 2 other devices and your TV."
    assert tvs == "Signs out your 3 TVs."
    assert no_list == no_tvs == "Signs out your other devices and your TVs."


def test_the_words_that_stay_are_apart_from_what_fades_beside_them() -> None:
    css = (WEB / "static" / "css" / "_profile.css").read_text()
    assert "#account-password-sub.is-new > span:not(.is-fresh) { animation: fade-in" in css
    assert "#account-password-sub.is-new {" not in css
    for out in _run("passwordDone"):
        assert out["faded"] == [out["says"]]


def test_when_it_was_changed_keeps_its_lines_while_the_form_is_open() -> None:
    out = _run("passwordTurned")
    assert (out["opened"], out["keyboard"], out["again"], out["shut"]) == (
        "164.5px",
        "164.5px",
        "164.5px",
        "",
    )
    # the window's own once that has another width
    assert (out["turned"], out["back"]) == ("", "")


def test_what_a_new_password_signed_out_is_drawn_said_and_kept_in_the_row() -> None:
    some, none, many, unsaid = _run("passwordDone")
    for out in (some, none, many, unsaid):
        assert out["live"] == "Password changed. " + out["says"]
        assert out["sub"] == "Changed just now."
        assert out["after"] == "Changed just now. " + out["says"]
        assert out["left"] is False
    assert some["says"] == "Your 2 other devices were signed out, and your TV was unpaired."
    assert (some["icons"], some["more"]) == ("laptop phone phone tv", "")
    assert (none["says"], none["icons"]) == ("You had no other device to sign out.", "")
    # as many as there is room for, a TV among them
    assert (many["icons"], many["more"]) == ("laptop phone phone phone tv", "+6")
    # a server that does not say: what the lists held
    assert unsaid["says"] == "Your other device was signed out, and your 2 TVs were unpaired."


def test_email_is_saved_only_when_it_differs_and_a_wrong_one_is_refused_unsent() -> None:
    out = _run("emailFold")
    assert out["out"] == [False, True, False, True]
    (tone, words), invalid, focused = out["bad"]
    assert tone == "me-msg err" and words
    assert (invalid, focused) == ("true", "account-email")
    assert out["undone"] == ["was@example.com", False, ""]


def test_why_the_others_cannot_be_signed_out_is_said_while_there_are_none() -> None:
    assert _run("onlySession") == {"disabled": True, "why": True}
    out = _run("sessions")
    # not while there are others, nor beside the word that they were just signed out
    assert (out["first"]["why"], out["after"]["why"]) == (False, False)


def test_sign_ins_that_cannot_be_listed_can_still_be_ended_and_a_failure_is_said() -> None:
    out = _run("sessionsFail")
    assert len(out["first"]["rows"]) == 1 and "Could not load" in out["first"]["rows"][0]
    assert out["first"]["disabled"] is False
    assert out["after"] == {
        "focused": "account-revoke-others",
        "disabled": False,
        "say": ["me-msg err", "No luck"],
    }


def test_signing_out_the_others_keeps_the_focus_and_ends_it_on_the_list() -> None:
    out = _run("sessions")
    assert out["whileSending"] == "account-revoke-others"
    assert out["after"]["focused"] == "account-sessions"


def test_a_new_password_is_sent_once_keeps_the_focus_and_reloads_the_sign_ins() -> None:
    out = _run("password")
    assert [sorted(body) for body in out["sent"]] == [["new_password", "old_password"]]
    assert out["whileSending"] == "account-submit-password"
    # neither Cancel nor Escape shuts it while it is sent, or while it is drawn
    assert (out["working"][2], out["drawn"]) == (False, False)
    # the form is shut, emptied and as it was
    assert (out["shut"], out["resets"], out["focused"]) == (True, 1, "account-open-password")
    assert (out["left"], out["says"]) == (["", ""], "Save new password")
    assert out["toasts"] == []
    assert out["rows"] == 1


def test_a_password_is_out_of_the_page_as_soon_as_it_is_saved() -> None:
    out = _run("passwordForgotten")
    assert out["left"] == ["", ""]
    assert out["state"] == [True, False, "false"]
    assert out["again"] is False


def test_a_press_as_the_saved_form_folds_away_does_not_strand_it() -> None:
    assert _run("passwordPressedAsItFolds") == [1, False, "false", "false"]


def test_the_row_takes_its_new_height_from_the_one_it_had_both_ways() -> None:
    out = _run("passwordEased")
    # its words and its button are at once where they end
    assert out["open"] == [["34px", ""], True, "33px"]
    assert out["shut"] == [["-34px", ""], True]


def test_the_rest_of_the_page_works_where_the_form_s_script_did_not_load() -> None:
    assert _run("passwordWithout") == {"opener": True, "rows": 1, "email": True}


def test_one_who_moved_on_while_the_tick_was_drawn_keeps_their_place() -> None:
    assert _run("passwordLeft") == ["account-email", True]


def test_a_new_password_on_its_way_is_said_and_typing_meanwhile_leaves_the_words() -> None:
    said, shown, _shut = _run("password")["working"]
    assert said == shown == "Saving…"


def test_a_refused_new_password_is_explained_beside_the_field_to_put_right() -> None:
    out = _run("passwordRefusals")
    for refusal, name, words in (
        ("old", "old", "Enter your current password"),
        ("new", "new", "7 more characters"),
        ("known", "new", "you have now"),
        # a whole sentence, whatever the server says
        ("wrong", "old", "No."),
        ("weak", "new", "a digit."),
        ("same", "new", "No."),
    ):
        field = f"account-{name}-password"
        assert words in out[refusal]["words"], refusal
        assert out[refusal]["said"] == [field + "-why"], refusal
        assert out[refusal]["focused"] == field, refusal
        assert out[refusal]["last"] == [field, field + "-why"], refusal
        assert out[refusal]["typed"] == [], refusal


def test_a_refused_new_password_is_kept_and_the_rule_it_broke_is_marked() -> None:
    out = _run("passwordRefusals")
    assert [out[refusal]["red"] for refusal in ("weak", "same", "wrong")] == [
        ["common"],
        ["same"],
        [],
    ]
    for refusal in ("wrong", "weak", "same", "limit", "cancelled"):
        assert (out[refusal]["kept"], out[refusal]["busy"]) == (True, "false"), refusal


def test_a_limit_on_new_passwords_is_said_at_the_button_pressed() -> None:
    out = _run("passwordRefusals")["limit"]
    assert out["said"] == ["account-password-result"]
    assert out["focused"] == "account-submit-password"


def test_the_password_dialog_cancelled_says_nothing_and_leaves_the_focus() -> None:
    out = _run("passwordRefusals")["cancelled"]
    assert (out["said"], out["focused"]) == ([], "account-submit-password")


def test_each_new_word_on_a_new_password_is_brought_into_view_by_the_shortest_way() -> None:
    # on a short phone the form is taller than the screen
    out = _run("password")
    assert out["revealedSending"][-1] == ["account-submit-password", "nearest"]
    field = "account-new-password"
    assert out["revealedRefusal"] == [[field, "nearest"], [field + "-why", "nearest"]]
    assert out["revealedEmptied"] == []


def test_a_status_brought_into_view_brings_the_button_beside_it() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_profile.css").read_text()
    margin = re.search(r"\.me-msg \{[^}]*scroll-margin-bottom: (\d+)px", css)
    # a 44px button centred on one 18px line reaches 13px below it
    assert margin and int(margin.group(1)) >= 13


def test_an_empty_status_takes_no_room_and_can_still_be_announced() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_profile.css").read_text()
    assert re.search(r"\.me-msg:empty \{ position: absolute; \}", css)
    assert not re.search(r"\.me-msg:empty[^}]*(display|visibility)", css)


def test_a_fold_has_room_at_its_sides_for_the_focus_rings_it_clips() -> None:
    css = (HARNESS.parents[4] / "src/mediaman/web/static/css/_profile.css").read_text()
    rule = re.search(r"is-moving > \.setg-fold-inner \{([^}]*)\}", css)
    assert rule and "padding-inline: 6px; margin-inline: -6px" in rule[1]


def test_a_late_answer_is_brought_into_view_only_for_a_reader_still_at_its_status() -> None:
    out = _run("lateAnswer")
    for which in out:
        assert {where for where, seen in out[which].items() if seen[0]} == set(out[which]), which
    for which in ("sessions", "sessionsRefused"):
        assert [where for where, seen in out[which].items() if seen[1]] == ["there", "around"]


def test_a_late_success_shuts_its_form_without_moving_a_reader_elsewhere() -> None:
    out = _run("lateAnswer")
    for which, field, home in (
        ("email", "account-old-password", "account-email"),
        ("password", "account-email", "account-open-password"),
    ):
        assert out[which]["elsewhere"] == [True, False, field, True]
        assert out[which]["there"] == [True, False, home, True]


def test_a_late_refusal_moves_the_cursor_and_the_page_only_for_a_reader_still_at_its_status() -> (
    None
):
    out = _run("lateAnswer")
    for which in ("emailRefused", "passwordRefused"):
        for where, (_pressed, shown, cursor, said) in out[which].items():
            assert (shown, cursor) == (
                (True, True) if where in ("there", "around") else (False, False)
            )
            assert said, (which, where)


def test_a_refusal_takes_the_place_of_what_the_status_said() -> None:
    assert _run("passwordRefusals")["afterLimit"]["said"] == ["account-old-password-why"]


def test_a_refusal_that_names_no_field_is_said_at_the_button() -> None:
    out = _run("passwordRefusals")["unknown"]
    assert (out["said"], out["focused"]) == (["account-password-result"], "account-submit-password")


def test_the_list_of_sign_ins_takes_the_focus_without_pulling_the_page_to_it() -> None:
    assert _run("focusOnTheList") == {"preventScroll": True}


def test_email_is_sent_once_trimmed_and_each_outcome_is_said() -> None:
    out = _run("email")
    assert [body["email"] for _url, body in out["sent"]] == [
        "vic@example.com",
        "",
        "nope@example.com",
        "vic@example.com",
    ]
    button, field = "account-submit-email", "account-email"
    # folding away hands the focus to the field, as does a refusal; a cancelled password keeps it
    assert out["focus"] == [button, field, button, field, button, field, button, button]
    assert out["saved"] == ["me-msg", "", "Email saved.", True]
    assert out["typed"] == ""
    assert out["removed"][2] == "Email removed. Alerts are off."
    assert out["refused"] == ["me-msg err", "Not an email address", False]
    assert out["cancelled"] == ["me-msg", "", False]


def test_the_plex_card_is_told_after_a_password_may_have_been_given() -> None:
    assert _run("toldOfPassword") == [[0, ["mm:reauth"]]] * 2


def test_the_history_switch_saves_at_once_and_one_press_at_a_time() -> None:
    out = _run("history")
    assert out["first"] == {"hiddenAtFirst": True, "hidden": False, "state": ["true", True]}
    assert out["off"]["during"] == out["off"]["after"] == ["false", False]
    assert out["sent"][0] == ["/api/search/assist/history", False]
    # two presses each time
    assert [wanted for _url, wanted in out["sent"]] == [False, True, True, True]
    assert len(out["off"]["toasts"]) == 1 and not out["off"]["toasts"][0].startswith("error")
    assert (out["off"]["focused"], out["off"]["busy"]) == ("account-history", None)


def test_a_history_switch_that_could_not_be_saved_goes_back_and_says_so() -> None:
    out = _run("history")
    for failed in (out["refused"], out["offline"]):
        assert failed["during"] == ["true", True]
        assert failed["after"] == ["false", False]
        assert failed["toasts"] == ["error: No luck"]
        assert failed["busy"] is None


def test_a_history_switch_saved_with_its_answer_lost_stands_as_saved() -> None:
    lost = _run("history")["lost"]
    assert lost["after"] == ["true", True]
    assert len(lost["toasts"]) == 1 and not lost["toasts"][0].startswith("error")


def test_nothing_about_the_ai_is_shown_without_a_key() -> None:
    assert _run("historyHidden") == [True, True]


def test_recent_searches_are_cleared_only_once_confirmed() -> None:
    out = _run("clearSearches")
    assert (out["no"]["asked"], out["no"]["sent"], out["no"]["toasts"]) == (1, [], [])
    assert out["yes"]["sent"] == ["/api/search/assist/searches"]
    assert len(out["yes"]["toasts"]) == 1 and not out["yes"]["toasts"][0].startswith("error")
    assert out["failed"]["toasts"] == ["error: No luck"]
    assert [out[k]["says"] for k in ("yes", "failed")] == ["Clear", "Clear"]
    assert out["yes"]["busy"] is None
    # The question this tab remembers goes with them.
    assert out["yes"]["left"] == ["other"]
    assert len(out["no"]["left"]) == len(out["failed"]["left"]) == 2


def test_a_persons_tvs_are_listed_as_text_and_with_none_there_is_no_section() -> None:
    out = _run("tvs")
    assert (out["hidden"], out["none"]) == (False, True)
    living, den, long_ago = out["rows"]
    assert living == ["Living room", "On now. Paired 12 March."]
    assert den[0] == "<b>Den" and den[1].startswith("Last seen Sunday, ")
    assert "Last seen 15 May. " in long_ago[1] or "Last seen 16 May. " in long_ago[1]


def test_an_admin_sees_the_tvs_of_others_and_unpairs_one_only_after_a_yes() -> None:
    out = _run("tvsOfAll")
    # neither their own again nor an older TV, which is nobody's
    assert len(out["rows"]) == 1
    assert "Theirs" in out["rows"][0] and "Paired by bob 12 March." in out["rows"][0]
    assert out["asked"][0]["title"] == "Unpair Theirs?"
    assert out["left"] == 1


def test_a_tv_is_renamed_in_its_row_and_a_failure_keeps_what_was_typed() -> None:
    out = _run("tvRename")
    assert out["empty"] == [0, "true"]
    assert out["failed"] == [1, ["error: Could not rename it. Try again."], True]
    assert out["sent"] == ["/api/tvs/1", {"name": "Den"}]
    assert (out["name"], out["label"]) == ("Den", "Rename: Den")
    assert out["toasts"] == ["Renamed to Den."]


def test_an_unpaired_tv_leaves_the_list_and_the_last_takes_the_section_with_it() -> None:
    out = _run("tvUnpair")
    assert out["failed"] == [2, ["error: Could not unpair it. Try again."]]
    assert out["one"] == [1, False, ["Unpaired. That TV now says it was signed out."]]
    assert out["sent"] == ["/api/tvs/1", "/api/tvs/1", "/api/tvs/2"]
    assert out["last"] == [0, True] and out["asked"] == 0
    # the bar of what plays on a TV lets it go at once, and only once it is unpaired
    assert out["kept"] == 0 and out["dropped"] == [1, 2]
