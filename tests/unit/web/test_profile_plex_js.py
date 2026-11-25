"""The Plex account card's script, run under node over the panels of its template."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

HARNESS = Path(__file__).parent / "js" / "profile_plex_harness.js"


def _run(scenario: str) -> Any:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_linking_goes_from_open_plex_to_is_this_you_to_linked() -> None:
    out = _run("link")
    assert out["first"] == {"shown": "unlinked", "focus": "", "status": ""}
    assert out["waiting"]["shown"] == "waiting"
    assert out["waiting"]["focus"] == "plex-open"
    assert out["waiting"]["href"] == "https://app.plex.tv/auth#?code=x"
    assert out["waiting"]["app"] == "mediaman (h)"
    assert out["still"] == "waiting"
    assert out["confirm"]["shown"] == "confirm"
    assert out["confirm"]["focus"] == "plex-confirm"
    assert out["confirm"]["status"] != out["waiting"]["status"]
    assert out["confirm"]["name"] == "<b>amy</b>"
    assert out["confirm"]["waits"] == []
    assert out["sent"] == ['POST /api/account/plex {"attempt":"a1","move":false}']
    assert out["linked"]["shown"] == "linked"
    assert "Reading" in out["linked"]["read"]
    assert "last read" in out["read"]["text"]
    assert out["read"]["waits"] == []


def test_only_plexs_own_approval_page_is_opened() -> None:
    out = _run("badUrl")
    assert (out["shown"], out["href"]) == ("failed", None)


def test_an_answer_after_cancelling_changes_nothing() -> None:
    out = _run("cancelWhileChecking")
    assert (out["shown"], out["waits"], out["cancelled"]) == ("unlinked", [], True)


def test_a_hidden_tab_does_not_ask_and_asks_at_once_on_return() -> None:
    out = _run("hidden")
    assert (out["whileHidden"], out["after"]) == (0, 1)
    assert out["waits"] == [2000]


def test_a_failed_sign_in_says_why_and_whether_plex_signed_out() -> None:
    refused, again, ran_out = _run("failures")
    assert (refused["shown"], refused["text"]) == ("failed", "No access here.")
    assert refused["status"] == "No access here."
    assert refused["note"] is True
    assert (again["note"], again["said"][-1:]) == (False, ["No access here."])
    assert (ran_out["shown"], ran_out["note"]) == ("failed", False)
    assert ran_out["text"] == "It ran out; see Authorized Devices."
    assert ran_out["focus"] == "plex-retry"


def test_a_failing_check_waits_longer_and_does_not_end_the_sign_in() -> None:
    out = _run("slowPlex")
    assert (out["shown"], out["waits"]) == ("waiting", [10000])
    assert "slow" in out["wait"]


def test_unlinking_asks_first() -> None:
    out = _run("unlink")
    assert out["kept"]["shown"] == "linked"
    assert out["after"] == "unlinked"
    assert out["deleted"] is True


def test_a_move_from_another_person_is_said_before_it_is_made() -> None:
    out = _run("moves")
    assert out["untold"] is False
    assert (out["told"]["shown"], out["told"]["notice"]) == ("confirm", True)
    assert "moves it to you" in out["told"]["said"]
    assert out["told"]["focus"] == "plex-moves"
    assert out["shown"] == "linked"
    assert [call.endswith('"move":true}') for call in out["sent"]] == [False, True]


def test_trying_a_failed_load_again_loads_and_does_not_start_a_link() -> None:
    out = _run("failedLoad")
    assert out["first"] == {"shown": "failed", "text": "Unreachable."}
    assert (out["shown"], out["started"]) == ("unlinked", False)


def test_one_failed_refresh_leaves_the_linked_card() -> None:
    out = _run("quietFailure")
    assert out["during"] == {"shown": "linked", "waits": [10000]}
    assert out["shown"] == "linked"
    assert "last read" in out["read"]


def test_a_link_made_in_another_tab_shows_here() -> None:
    assert _run("otherTab") == {"shown": "linked", "waits": []}
    assert _run("otherTabConfirmed") == ["linked", "linked", "failed", "Too late."]


def test_cancelling_shows_when_mediaman_may_still_be_signed_in() -> None:
    assert _run("cancelNote") == {"shown": "unlinked", "note": True}


def test_a_busy_button_keeps_the_focus_and_runs_once() -> None:
    out = _run("busy")
    assert out["during"] == {"disabled": False, "aria": "true"}
    assert (out["starts"], out["after"]) == (1, None)


def test_the_same_failure_twice_is_announced_twice() -> None:
    assert _run("repeat") == ["Plex is down.", "Plex is down."]


def test_a_move_is_read_before_the_button_can_be_pressed() -> None:
    out = _run("movesFromTheStart")
    assert (out["shown"], out["focus"]) == ("confirm", "plex-moves")


def test_a_key_held_down_presses_nothing_more() -> None:
    assert _run("heldKey") == [True, True, False, False]


def test_a_person_whose_link_moved_sees_it_until_they_dismiss_it() -> None:
    out = _run("moved")
    assert out["first"]["note"] is True
    assert out["first"]["shown"] == "unlinked"
    assert "1 Mar 2026" in out["first"]["when"]
    assert out["failed"] == {"note": True, "error": "Unreachable."}
    assert "2 Mar 2026" in out["newer"]
    assert (out["after"]["note"], out["after"]["focus"]) == (False, "plex-start")


def test_dismissing_where_plex_is_not_set_up_keeps_the_focus_in_the_card() -> None:
    out = _run("movedUnset")
    assert (out["shown"], out["note"], out["focus"]) == ("unset", False, "plex")


def test_a_card_left_open_shows_the_move_once_it_hears_of_it() -> None:
    assert _run("lostWhileOpen") == {"shown": "unlinked", "note": True}


def test_linking_asks_who_it_is_and_never_speaks_of_saving() -> None:
    (words,) = _run("asks")
    assert (words["title"], words["confirmLabel"]) == ("Confirm it’s you", "Continue")
    assert "Plex" in words["descText"]
    assert not any("save" in text.lower() for text in words.values())


PLEX = "https://app.plex.tv/auth#?code=x"
OPEN = {"opener": None, "closed": False}


def test_the_press_itself_opens_the_tab_and_the_answer_sends_it_to_plex() -> None:
    out = _run("firstPress")
    assert out["pressed"]["tabs"] == [{**OPEN, "went": []}]
    assert "Opening Plex" in out["pressed"]["says"]
    assert (out["pressed"]["cancel"], out["cancel"]) == (True, False)
    assert out["pressed"]["label"] != out["label"] == "Link Plex account"
    assert out["after"] == [{**OPEN, "went": [PLEX]}]
    assert "noreferrer" in out["rel"]
    assert out["lead"] == "Approve mediaman in the Plex tab. This page updates by itself."
    assert (out["shown"], out["open"], out["loud"]) == ("waiting", "Open Plex again", False)


def test_a_start_that_fails_closes_its_tab_unsent_and_says_why() -> None:
    outs = _run("tabOnFailure")
    assert [(o["closed"], o["went"]) for o in outs] == [(True, [])] * 5
    assert [o["shown"] for o in outs] == ["failed"] * 4 + ["unset"]
    assert [o["text"] for o in outs[:2]] == ["Plex is down.", "Unreachable."]


def test_a_blocked_tab_leaves_a_main_button_that_opens_plex() -> None:
    out = _run("blockedTab")
    assert (out["shown"], out["href"]) == ("waiting", PLEX)
    assert (out["first"]["open"], out["first"]["loud"]) == ("Open Plex", True)
    assert "browser" in out["first"]["lead"]
    assert (out["pressed"]["open"], out["pressed"]["loud"]) == ("Open Plex again", False)
    assert "in the Plex tab" in out["pressed"]["lead"]


def test_a_tab_closed_before_plex_opened_is_told_and_offered_again() -> None:
    out = _run("closedEarly")
    assert (out["shown"], out["went"], out["loud"]) == ("waiting", [], True)
    assert "closed" in out["lead"]


def test_cancelling_while_plex_opens_closes_the_tab_and_ends_the_late_sign_in() -> None:
    out = _run("cancelOpening")
    assert out["at1"]["closed"] is True
    assert "Cancelled" in out["at1"]["note"]
    assert out["at1"]["said"] == out["at1"]["note"]
    assert out["at1"]["label"] == "Link Plex account"
    assert (out["shown"], out["went"], out["cancelled"]) == ("unlinked", [], True)
    assert out["focus"] == "plex-start"


def test_cancelling_while_waiting_closes_the_tab_and_says_so_until_the_next_press() -> None:
    out = _run("cancelWaiting")
    assert (out["closed"], out["shown"], out["noteAfter"]) == (True, "unlinked", "")
    assert out["status"] == out["note"] != ""


def test_the_password_is_asked_without_a_stray_tab_and_its_press_opens_one() -> None:
    out = _run("password")
    closed = {"opener": None, "closed": True, "went": []}
    assert out["asked"] == [closed]
    assert out["wrong"] == {"ok": False, "error": "Wrong."}
    assert out["afterWrong"] == [closed, closed]
    assert out["right"] == {"ok": True}
    assert out["after"] == [closed, closed, {**OPEN, "went": [PLEX]}]
    assert out["shown"] == "waiting"


def test_a_password_accepted_after_its_dialog_was_closed_starts_nothing() -> None:
    out = _run("passwordCancelled")
    assert all(tab["closed"] for tab in out["tabs"])
    assert (out["starts"], out["shown"]) == (1, "unlinked")
    assert "Cancelled" in out["note"]


def test_one_press_after_it_ran_out_starts_again_in_a_new_tab() -> None:
    out = _run("expired")
    assert out["text"] == "It ran out."
    assert (out["shown"], out["tabs"][1:]) == ("waiting", [{**OPEN, "went": [PLEX]}])


def test_coming_back_asks_at_once_past_a_stuck_check_and_tells_what_to_do() -> None:
    out = _run("returning")
    assert out["first"]["checks"] == 2
    assert out["first"]["waits"] == [2000]
    assert "closed the Plex tab" in out["first"]["wait"]
    assert (out["focusAlone"], out["waits"]) == (1, [2000])


def test_a_new_link_is_celebrated_by_name_until_the_page_is_shown_again() -> None:
    out = _run("success")
    assert (out["done"], out["name"], out["since"], out["kept"]) == (
        True,
        "<b>amy</b>",
        False,
        True,
    )
    assert "<b>amy</b>" in out["said"]
    assert (out["later"], out["sinceLater"]) == (False, True)


def test_a_password_that_is_due_is_asked_first_and_its_press_opens_the_tab() -> None:
    out = _run("passwordDue")
    assert out["asked"] == {"tabs": 0, "starts": 0, "label": "Link Plex account"}
    assert out["wrong"] == {"closed": True, "label": "Link Plex account"}
    assert out["after"] == {**OPEN, "went": [PLEX]}
    assert out["good"] == {"asked": 1, "tab": {**OPEN, "went": [PLEX]}}
    assert out["nearlyOut"] == {"asked": 2, "tabs": 3}
    assert out["cancelled"] == {"label": "Link Plex account", "starts": 2}


def test_the_card_eases_to_each_states_height_unless_motion_is_unwelcome() -> None:
    out = _run("heights")
    assert out["out"][-1:] == [["300px", "160px", 250]]
    assert out["calm"] == []


def test_a_tab_never_outlives_a_page_that_goes_to_sign_in() -> None:
    out = _run("sessionEnded")
    assert (out["before"], out["gone"]) == (False, True)
    # back from the page's cache, the old dialog opens nothing more
    assert (out["tabs"], out["late"]) == (1, False)
    assert (out["label"], out["shown"]) == ("Link Plex account", "unlinked")


def test_a_password_dialog_kept_by_the_pages_cache_still_links() -> None:
    out = _run("frozenDialog")
    assert out == {"asked": 1, "tabs": [{**OPEN, "went": [PLEX]}], "shown": "waiting"}


def test_a_start_that_was_never_answered_does_not_hold_up_the_next() -> None:
    assert _run("stuckStart") == {"first": True, "shown": "waiting", "tabs": 2}


def test_a_password_given_elsewhere_is_not_asked_for_again() -> None:
    out = _run("freshHint")
    assert out == {"out": [0, "waiting"] * 3, "due": 1}


def test_a_late_hint_does_not_undo_a_password_given_since() -> None:
    assert _run("staleHint") == 1


def test_a_second_start_waits_until_the_cancelled_one_is_ended() -> None:
    out = _run("race")
    assert out["held"] == 1
    assert out["ending"] == {"starts": 1, "cancel": True}
    assert (out["starts"], out["shown"]) == (2, "waiting")
    assert [tab["went"] for tab in out["tabs"]] == [[], [PLEX + "2"]]


def test_a_start_cancelled_while_it_waits_is_never_sent() -> None:
    assert _run("raceCancelledTwice") == {"starts": 1, "shown": "unlinked", "open": 0}


def test_the_page_scrolls_only_for_a_press_and_only_to_what_is_out_of_view() -> None:
    out = _run("scroll")
    assert out["press"] == {"early": 0, "after": ["plex-open"]}
    assert out["wording"] == {"eased": ["160px", "180px", 250], "scrolled": []}
    assert out["background"] == {"shown": "confirm", "focus": "plex-confirm", "scrolled": []}
    assert out["inView"] == []
    assert out["calm"] == ["plex-open"]


def test_a_new_change_of_height_ends_the_one_before_it() -> None:
    assert _run("ease") == {"cancelled": True, "scrolledByOld": [], "last": False}


def test_a_press_known_to_be_over_the_limit_opens_no_tab() -> None:
    assert _run("limited") == {"known": {"tabs": 0, "text": "Too many."}, "over": 1}
    ready = _run("limitedReady")
    assert ready["tabs"] == 0
    assert "browser" not in ready["lead"]


def test_a_password_after_one_over_the_limit_opens_no_tab() -> None:
    out = _run("limitedPassword")
    assert [tab["closed"] for tab in out["tabs"]] == [True]
    assert out["shown"] == "waiting"


def test_a_start_that_is_not_answered_holds_the_next_for_three_seconds_only() -> None:
    out = _run("startUnanswered")
    assert out["held"] == {"starts": 1, "waits": [3000]}
    assert out["capped"] == {"shown": "waiting", "starts": 2, "went": 1}
    assert (out["shown"], out["href"]) == ("waiting", PLEX + "2")
    assert out["cancel"] == ['POST /api/account/plex/cancel {"attempt":"a1"}']


def test_a_start_waits_for_a_cancel_but_three_seconds_only() -> None:
    assert _run("cancelUnanswered") == {"held": 1, "starts": 2, "shown": "waiting"}


def test_coming_back_asks_for_one_hint_and_a_password_given_for_another() -> None:
    assert _run("oneGlance") == {"once": 1, "later": 3}


def test_a_start_that_another_one_replaced_shows_that_one() -> None:
    assert _run("displaced") == {"shown": "waiting", "closed": True}
