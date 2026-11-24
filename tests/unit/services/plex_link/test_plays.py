"""Reading a linked person's plays from the Plex server."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.crypto import encrypt_value
from mediaman.services.plex_link import repository, sync
from mediaman.services.plex_link.plex_tv import PlexIdentity
from tests.helpers.fake_plex import SERVER, SERVER_ID, FakePlex

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
HISTORY = ("GET", f"{SERVER}/status/sessions/history/all")


def row(number: int, *, account: int = 4242, ago: timedelta = timedelta(days=1), **over: str):
    return {
        "historyKey": f"/status/sessions/history/{number}",
        "ratingKey": str(number),
        "type": "movie",
        "title": f"Film {number}",
        "librarySectionID": "1",
        "accountID": str(account),
        "viewedAt": str(int((NOW - ago).timestamp())),
    } | over


def add_user(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, 'h', '2020')",
        (name,),
    )
    conn.commit()


def link(conn, name: str, account: int, server_id: str = SERVER_ID) -> None:
    add_user(conn, name)
    repository.save_link(conn, name, PlexIdentity(account, account, name), server_id, ip="")


def titles(conn, name: str) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM plex_plays WHERE username = ? ORDER BY viewed_at DESC", (name,)
    )
    return [r["title"] for r in rows]


@pytest.fixture
def plex(monkeypatch, conn, secret_key, freezer):
    freezer.move_to(NOW)
    token = encrypt_value("admin-token", secret_key, conn=conn, aad=b"plex_token")
    for key, value, encrypted in (
        ("plex_url", SERVER, 0),
        ("plex_token", token, 1),
        ("plex_libraries", '["1", "2"]', 0),
    ):
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at) VALUES (?, ?, ?, '2026')",
            (key, value, encrypted),
        )
    conn.commit()
    return FakePlex().install(monkeypatch)


def test_nobody_linked_means_no_call_to_plex(plex, conn, secret_key):
    sync.read_plays(conn, secret_key)
    assert plex.calls == []


def test_a_person_gets_their_own_plays_even_if_the_server_sends_everyones(plex, conn, secret_key):
    link(conn, "amy", 4242)
    link(conn, "bob", 99)
    nobody = row(4)
    del nobody["accountID"]
    plex.history_rows = [row(1), row(2, account=99), row(3, account=1), nobody]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]
    assert titles(conn, "bob") == ["Film 2"]


def test_the_server_is_only_ever_read(plex, conn, secret_key):
    link(conn, "amy", 4242)
    sync.read_plays(conn, secret_key)
    assert {method for method, *_ in plex.calls} == {"GET"}


def test_an_episode_keeps_its_show_season_and_number(plex, conn, secret_key):
    link(conn, "amy", 4242)
    plex.history_rows = [
        row(
            5,
            type="episode",
            title="Pilot",
            grandparentTitle="Some Show",
            grandparentKey="/library/metadata/50",
            parentIndex="2",
            index="7",
        )
    ]
    sync.read_plays(conn, secret_key)
    saved = conn.execute("SELECT * FROM plex_plays").fetchone()
    assert (saved["show_title"], saved["show_rating_key"]) == ("Some Show", "50")
    assert (saved["season_number"], saved["episode_number"]) == (2, 7)
    assert saved["viewed_at"] == "2026-02-28T12:00:00+00:00"


@pytest.mark.parametrize(
    "bad",
    [
        {"librarySectionID": "9"},
        {"librarySectionID": ""},
        {"type": "track"},
        {"type": "episode"},
        {"title": ""},
        {"historyKey": ""},
        {"viewedAt": "soon"},
        {"viewedAt": "99999999999999999999"},
        {"viewedAt": str(int((NOW + timedelta(days=30)).timestamp()))},
    ],
)
def test_a_play_that_cannot_be_trusted_is_left_out(plex, conn, secret_key, bad):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1), row(2, **bad)]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]


def test_every_page_is_read_and_the_newest_two_thousand_of_two_years_kept(
    plex, conn, secret_key, monkeypatch
):
    monkeypatch.setattr(sync, "KEEP_PLAYS", 450)
    link(conn, "amy", 4242)
    plex.history_rows = [row(n, ago=timedelta(hours=n)) for n in range(1, 500)]
    sync.read_plays(conn, secret_key)
    kept = titles(conn, "amy")
    assert (len(kept), kept[0], kept[-1]) == (450, "Film 1", "Film 450")


def test_a_server_that_repeats_its_first_page_is_not_read_for_ever(plex, conn, secret_key):
    link(conn, "amy", 4242)
    one = '<MediaContainer><Video historyKey="h" accountID="4242"/></MediaContainer>'
    plex.answers[HISTORY] = (200, one)
    sync.read_plays(conn, secret_key)
    assert plex.paths().count(HISTORY) == 2


def test_a_later_read_asks_from_a_day_before_the_last_and_adds_no_play_twice(
    plex, conn, secret_key, freezer
):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1)]
    sync.read_plays(conn, secret_key)
    freezer.move_to(NOW + timedelta(days=3))
    plex.history_rows = [row(2, ago=timedelta(days=-2)), row(1)]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 2", "Film 1"]
    since = int(plex.calls[-1][3]["viewedAt>"][0])
    assert since == int((NOW - timedelta(days=1)).timestamp())


def test_plays_older_than_two_years_go(plex, conn, secret_key, freezer):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1, ago=timedelta(days=700)), row(2, ago=timedelta(days=800))]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]
    plex.history_rows = []
    freezer.move_to(NOW + timedelta(days=60))
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == []


def test_one_persons_failure_keeps_their_plays_and_reads_the_next_person(plex, conn, secret_key):
    link(conn, "amy", 4242)
    link(conn, "bob", 99)
    plex.history_rows = [row(1), row(2, account=99)]
    sync.read_plays(conn, secret_key)
    plex.history_rows = [row(3), row(4, account=99)]
    real = plex._default

    def amy_fails(method, where, query):
        if query.get("accountID") == ["4242"]:
            return 500, ""
        return real(method, where, query)

    plex._default = amy_fails
    before = repository.get_link(conn, "amy").last_read_at
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]
    assert repository.get_link(conn, "amy").last_read_at == before
    assert set(titles(conn, "bob")) == {"Film 2", "Film 4"}


def test_another_plex_server_ends_the_links_made_on_the_old_one(plex, conn, secret_key):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1)]
    sync.read_plays(conn, secret_key)
    plex.server_id = "srv-2"
    sync.read_plays(conn, secret_key)
    assert repository.list_links(conn) == []
    assert titles(conn, "amy") == []


@pytest.mark.parametrize("answer", [(500, ""), (200, "<MediaContainer/>"), (200, "junk")])
def test_a_server_that_will_not_say_who_it_is_changes_nothing(plex, conn, secret_key, answer):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1)]
    sync.read_plays(conn, secret_key)
    plex.answers["GET", f"{SERVER}/identity"] = answer
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]


def test_reading_plays_leaves_progress_and_cleanup_alone(plex, conn, secret_key):
    link(conn, "amy", 4242)
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, plex_library_id, plex_rating_key,"
        " added_at, file_path, file_size_bytes) VALUES ('1','Film 1','movie',1,'1','2020','/f',1)"
    )
    conn.commit()
    plex.history_rows = [row(1)]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]
    assert conn.execute("SELECT last_watched_at FROM media_items").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM watch_progress").fetchone()[0] == 0


def test_unlinking_takes_the_plays_and_the_picks_made_from_them_and_nobody_elses(conn):
    for name, account in (("amy", 4242), ("bob", 99)):
        link(conn, name, account)
        current = repository.get_link(conn, name)
        play = repository.PlexPlay("h1", "movie", "Film", NOW.isoformat())
        repository.save_plays(
            conn, current, [play], read_at=NOW, oldest=NOW - timedelta(days=9), keep=9
        )
        for uses_plex in (0, 1):
            conn.execute(
                "INSERT INTO user_picks (username, title, media_type, batch_id, created_at,"
                " uses_plex, tmdb_id) VALUES (?, 'T', 'movie', 'b', '2026', ?, ?)",
                (name, uses_plex, uses_plex),
            )
    conn.commit()
    assert repository.unlink(conn, "amy", actor="amy", reason="self")
    assert titles(conn, "amy") == []
    assert titles(conn, "bob") == ["Film"]
    left = conn.execute("SELECT username, uses_plex FROM user_picks ORDER BY username, uses_plex")
    assert [tuple(r) for r in left] == [("amy", 0), ("bob", 0), ("bob", 1)]


@pytest.mark.parametrize("account", [4242, 7], ids=["same account", "another account"])
def test_a_read_that_ends_after_an_unlink_and_relink_stores_nothing(conn, freezer, account):
    freezer.move_to(NOW)
    link(conn, "amy", 4242)
    old = repository.get_link(conn, "amy")
    repository.unlink(conn, "amy", actor="amy", reason="self")
    play = repository.PlexPlay("h1", "movie", "Film", NOW.isoformat())
    args = {"read_at": NOW, "oldest": NOW - timedelta(days=9), "keep": 9}
    assert repository.save_plays(conn, old, [play], **args) is False
    freezer.tick(1)
    repository.save_link(conn, "amy", PlexIdentity(account, account, "other"), SERVER_ID, ip="")
    assert repository.save_plays(conn, old, [play], **args) is False
    assert titles(conn, "amy") == []
