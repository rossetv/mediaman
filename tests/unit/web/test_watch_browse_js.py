"""The Watch page's Discover rows, Recommended row and Keep from Leaving soon, run under node
against a fake DOM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "js" / "watch_browse_harness.js"


def _run(scenario: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), scenario], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_skeleton_gives_way_to_the_discover_rows_with_their_badges() -> None:
    out = _run("loaded")
    assert out["rows"] == ["recommended", "trending", "popular_movies", "popular_tv"]
    assert (out["skeleton"], out["busy"], out["label"]) == (False, None, "Discover")
    assert out["badges"] == ["In library", "Queued", "Downloading 42%", None]


def test_cards_open_by_title_ref_keep_a_link_and_drop_a_bad_ref() -> None:
    out = _run("loaded")
    assert out["refs"] == ["movie-11", "tmdb-movie-2", "tmdb-tv-3", "tmdb-movie-4"]
    assert out["hrefs"][0] == "/watch/movie/11"
    assert out["hrefs"][1] == "/watch#title=tmdb-movie-2"


def test_a_title_from_the_server_is_text_never_markup() -> None:
    assert _run("loaded")["hostileTitle"] == [3]


def test_rows_added_after_load_get_the_row_arrows_once() -> None:
    assert _run("loaded")["arrows"] == [2, 2, 2, 2]


def test_a_title_change_updates_the_badge_on_its_cards() -> None:
    out = _run("titleChanged")
    assert out == {"queued": "Queued", "downloading": "Downloading 42%", "inLibrary": "In library"}


def test_a_failed_discover_row_says_which_and_try_again_loads_it_in_place() -> None:
    out = _run("discoverFailed")
    assert out["notes"][0].startswith("Couldn’t load Trending this week.")
    assert len(out["notes"]) == 3
    assert out["rec"] is True
    assert out["after"] == ["recommended", "trending", "popular_movies", "popular_tv"]
    assert (out["failedLeft"], out["cards"]) == (0, 4)


def test_without_tmdb_or_picks_the_slot_is_emptied_and_tmdb_is_not_asked() -> None:
    out = _run("nothing")
    assert out["children"] == 0
    assert out["calls"] == ["GET /api/watch/recommended"]


def test_recommendations_off_hide_the_row_from_viewers_and_point_admins_to_settings() -> None:
    out = _run("recsOff")
    assert out["viewerRow"] is False
    assert out["viewerRows"] == ["trending", "popular_movies", "popular_tv"]
    assert out["adminLink"] == "/settings#setg-general"
    assert out["adminTools"] == 0


def test_no_picks_says_so_and_only_admins_get_the_refresh_hint() -> None:
    out = _run("noPicks")
    assert out["viewer"] == "No recommendations yet."
    assert out["admin"].startswith("No recommendations yet.")
    assert "Refresh" in out["admin"]


def test_viewers_can_switch_batches_but_only_admins_can_refresh() -> None:
    out = _run("tools")
    assert out["viewer"] == ["picks"]
    assert out["admin"] == ["picks", "refresh"]


def test_choosing_an_earlier_batch_swaps_the_picks_in_the_same_row() -> None:
    out = _run("batch")
    assert out["open"]["keepMenuLook"] is True
    assert out["open"]["items"] == [["Latest · 21 Nov", "true"], ["14 Nov", "false"]]
    assert out["open"]["focus"] is True
    assert out["sameRow"] is True
    assert out["cards"] == ["rec-9"]
    assert out["checked"] == ["14 Nov"]
    assert "14 Nov" in out["pill"]


def test_refresh_during_the_cooldown_shows_the_time_left_and_no_error() -> None:
    out = _run("refreshCooldown")
    assert out["label"] == "Refresh in 14 h"
    assert out["disabled"] == "true"
    assert out["error"] is False
    assert out["note"] == "New picks can be fetched again in 14 h."


def test_a_refused_refresh_shows_the_reason_inline_and_can_be_tried_again() -> None:
    out = _run("refreshFailed")
    assert out["error"] == "Plex not configured"
    assert (out["label"], out["disabled"]) == ("Refresh picks", None)


def test_a_session_lost_while_refreshing_goes_to_login() -> None:
    assert _run("refreshLoggedOut")["href"] == "/login"


def test_a_finished_refresh_puts_the_new_picks_in_the_same_row_and_starts_the_cooldown() -> None:
    out = _run("refreshDone")
    assert out["busy"] == {"label": "Refreshing…", "spinning": True}
    assert out["sameRow"] is True
    assert out["cards"] == ["rec-20"]
    assert out["after"] == "Refresh in 24 h"


def test_keep_from_leaving_soon_offers_undo_and_moves_focus_to_the_next_keep() -> None:
    out = _run("keepFromRow")
    assert out["gone"] is True
    assert out["focus"] == "1101"
    assert out["toast"] == "Kept Leaving Film for 30 days."
    assert out["undo"] == "Undo"
