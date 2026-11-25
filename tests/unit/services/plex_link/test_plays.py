"""Reading a linked person's plays from the Plex server."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.crypto import encrypt_value
from mediaman.db.connection import _add_missing_columns
from mediaman.services.plex_link import history, repository, sync
from mediaman.services.plex_link.bounded import Pool, TooBusy, TookTooLong
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
            conn,
            current,
            [play],
            libraries=["1"],
            read_at=NOW,
            oldest=NOW - timedelta(days=9),
            keep=9,
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
    args = {"libraries": ["1"], "read_at": NOW, "oldest": NOW - timedelta(days=9), "keep": 9}
    assert repository.save_plays(conn, old, [play], **args) is False
    freezer.tick(1)
    repository.save_link(conn, "amy", PlexIdentity(account, account, "other"), SERVER_ID, ip="")
    assert repository.save_plays(conn, old, [play], **args) is False
    assert titles(conn, "amy") == []


@pytest.mark.parametrize("number", [str(2**63), "-1", "100000", "9" * 5000, "two"])
def test_a_number_that_cannot_be_real_is_left_out_and_stops_nobody(plex, conn, secret_key, number):
    link(conn, "amy", 4242)
    link(conn, "bob", 99)
    plex.history_rows = [
        row(1, type="episode", grandparentTitle="Show", parentIndex=number, index=number),
        row(2, account=99),
    ]
    sync.read_plays(conn, secret_key)
    saved = conn.execute("SELECT * FROM plex_plays WHERE username = 'amy'").fetchone()
    assert (saved["season_number"], saved["episode_number"]) == (None, None)
    assert titles(conn, "bob") == ["Film 2"]


ENTITY = '<!DOCTYPE m [<!ENTITY a "b">]><MediaContainer machineIdentifier="&a;"/>'


@pytest.mark.parametrize("where", [("GET", f"{SERVER}/identity"), HISTORY], ids=["server", "plays"])
@pytest.mark.parametrize("body", [ENTITY, '<?xml version="1.0" encoding="nope"?><a/>'])
def test_xml_the_parser_refuses_is_plex_being_unreadable(plex, conn, secret_key, where, body):
    link(conn, "amy", 4242)
    plex.answers[where] = (200, body)
    sync.read_plays(conn, secret_key)
    assert repository.get_link(conn, "amy").last_read_at is None


def test_any_error_in_one_persons_read_is_undone_and_the_next_is_read(
    plex, conn, secret_key, monkeypatch
):
    link(conn, "amy", 4242)
    link(conn, "bob", 99)
    plex.history_rows = [row(1), row(2, account=99)]
    real = repository.save_plays

    def save(conn, link, plays, **options):
        if link.username == "amy":
            conn.execute("UPDATE plex_links SET plex_name = 'half done'")
            raise RuntimeError("bug")
        return real(conn, link, plays, **options)

    monkeypatch.setattr(repository, "save_plays", save)
    sync.read_plays(conn, secret_key)
    assert titles(conn, "bob") == ["Film 2"]
    assert repository.get_link(conn, "amy").plex_name == "amy"


def test_a_read_that_ran_out_of_pages_is_read_from_the_same_time_again(
    plex, conn, secret_key, monkeypatch
):
    """As when the server sends everyone's plays whoever is asked about."""
    monkeypatch.setattr(history, "MAX_PAGES", 2)
    monkeypatch.setattr(history, "PAGE_SIZE", 2)
    link(conn, "amy", 4242)
    plex.history_rows = [row(n, account=99, ago=timedelta(minutes=n)) for n in range(1, 4)]
    plex.history_rows += [row(4, ago=timedelta(hours=1)), row(5, ago=timedelta(days=30))]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 4"]
    assert repository.get_link(conn, "amy").last_read_at is None

    monkeypatch.setattr(history, "MAX_PAGES", 5)
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 4", "Film 5"]
    assert repository.get_link(conn, "amy").last_read_at == NOW.isoformat()


def test_the_plays_of_an_unfinished_read_go_with_their_library(plex, conn, secret_key, monkeypatch):
    monkeypatch.setattr(history, "MAX_PAGES", 1)
    monkeypatch.setattr(history, "PAGE_SIZE", 1)
    link(conn, "amy", 4242)
    plex.history_rows = [row(1), row(2)]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]

    conn.execute("UPDATE settings SET value = '[]' WHERE key = 'plex_libraries'")
    conn.commit()
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == []


def test_reading_never_raises(plex, conn, secret_key, monkeypatch):
    link(conn, "amy", 4242)
    monkeypatch.setattr(sync, "open_history", lambda *_: 1 / 0)
    sync.read_plays(conn, secret_key)


class Clock:
    now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sync, "time", clock)
    monkeypatch.setattr(history, "time", clock)
    return clock


def test_a_person_whose_pages_take_too_long_gets_nothing_and_the_next_is_read(
    plex, conn, secret_key, clock
):
    link(conn, "amy", 4242)
    link(conn, "bob", 99)
    plex.history_rows = [row(n, ago=timedelta(minutes=n)) for n in range(1, 900)]
    plex.history_rows.insert(0, row(901, account=99))

    def slow_for_amy(method, where, query):
        if query.get("accountID") == ["4242"]:
            clock.now += 40

    plex.on_call = slow_for_amy
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == []
    assert repository.get_link(conn, "amy").last_read_at is None
    assert titles(conn, "bob") == ["Film 901"]


def test_a_run_out_of_time_takes_up_next_time_where_it_stopped(
    plex, conn, secret_key, clock, freezer, monkeypatch
):
    for name, number in (("amy", 1), ("bob", 2), ("cat", 3)):
        link(conn, name, number)
    seen: list[str] = []

    def slow(method, where, query):
        if where.endswith("/history/all"):
            seen.append(query["accountID"][0])
            clock.now += sync._RUN_SECONDS / 2 + 1

    plex.on_call = slow
    plex.history_rows = []
    monkeypatch.setattr(sync, "_PERSON_SECONDS", 1000.0)
    sync.read_plays(conn, secret_key)
    assert seen == ["1", "2"]
    freezer.tick(60)
    sync.read_plays(conn, secret_key)
    assert seen[2] == "3"


def test_an_answer_that_never_ends_is_left_behind(plex, conn, secret_key, monkeypatch):
    monkeypatch.setattr(sync, "_PERSON_SECONDS", 0.5)
    link(conn, "amy", 4242)
    link(conn, "bob", 99)
    plex.history_rows = [row(1), row(2, account=99)]
    over = threading.Event()

    def hang_for_amy(method, where, query):
        if query.get("accountID") == ["4242"]:
            over.wait(2)

    plex.on_call = hang_for_amy
    try:
        sync.read_plays(conn, secret_key)
        assert titles(conn, "amy") == []
        assert titles(conn, "bob") == ["Film 2"]
    finally:
        over.set()
        # Or its next call would find the fake Plex taken away.
        for thread in threading.enumerate():
            if thread.name == "plex-link-call":
                thread.join(5)


def test_a_pool_counts_the_calls_still_running_and_those_left_behind():
    pool = Pool(2)
    over, running = threading.Event(), threading.Event()
    try:
        with pytest.raises(TookTooLong):
            pool.within(0.01, lambda: over.wait(10))
        waiting = threading.Thread(
            target=lambda: pool.within(10, lambda: (running.set(), over.wait(10)))
        )
        waiting.start()
        assert running.wait(5)
        with pytest.raises(TooBusy):
            pool.within(1, lambda: 1)
    finally:
        over.set()
    waiting.join(5)
    assert Pool(1).within(5, lambda: 1) == 1


def test_a_call_that_ends_makes_room_again():
    pool = Pool(1)
    for _ in range(3):
        with pytest.raises(ZeroDivisionError):
            pool.within(5, lambda: 1 / 0)
        assert pool.within(5, lambda: "done") == "done"


def test_people_whose_reads_are_slow_do_not_keep_the_next_from_being_read(
    plex, conn, secret_key, clock, freezer
):
    for number in range(1, 7):
        link(conn, f"user{number}", number)
    plex.history_rows = [row(n, account=6, ago=timedelta(minutes=n)) for n in range(1, 4)]

    def slow_for_five(method, where, query):
        if where.endswith("/history/all") and query["accountID"] != ["6"]:
            clock.now += sync._PERSON_SECONDS + 1

    plex.on_call = slow_for_five
    plex.answers[HISTORY] = (200, '<MediaContainer><Video historyKey="h"/></MediaContainer>')
    sync.read_plays(conn, secret_key)
    assert titles(conn, "user6") == []
    plex.answers.clear()
    freezer.tick(60)
    sync.read_plays(conn, secret_key)
    assert len(titles(conn, "user6")) == 3


def test_a_turn_that_could_not_start_is_not_counted(plex, conn, secret_key, monkeypatch):
    link(conn, "amy", 4242)
    full = Pool(1)
    monkeypatch.setattr(sync, "PLEX_SERVER", full)
    monkeypatch.setattr(sync, "read_server_id", lambda _plex: SERVER_ID)
    over = threading.Event()
    try:
        with pytest.raises(TookTooLong):
            full.within(0.01, lambda: over.wait(10))
        sync.read_plays(conn, secret_key)
    finally:
        over.set()
    assert repository.get_link(conn, "amy").last_tried_at is None


def set_libraries(conn, value: str) -> None:
    conn.execute("UPDATE settings SET value = ? WHERE key = 'plex_libraries'", (value,))
    conn.commit()


def test_a_library_added_later_gets_its_older_plays(plex, conn, secret_key, freezer):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1), row(2, ago=timedelta(days=90), librarySectionID="7")]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]
    freezer.tick(timedelta(days=5))
    set_libraries(conn, '["1", "7"]')
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1", "Film 2"]


def test_a_library_taken_away_loses_its_plays_even_while_plex_is_down(plex, conn, secret_key):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1), row(2, librarySectionID="2", ago=timedelta(days=9))]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1", "Film 2"]
    set_libraries(conn, '["1"]')
    plex.answers["GET", f"{SERVER}/identity"] = (503, "")
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]

    set_libraries(conn, '["1", "2"]')
    plex.answers.clear()
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1", "Film 2"]


def test_with_no_library_ticked_nothing_is_kept_or_counted_as_read(plex, conn, secret_key):
    link(conn, "amy", 4242)
    set_libraries(conn, "[]")
    sync.read_plays(conn, secret_key)
    assert repository.get_link(conn, "amy").last_read_at is None
    assert plex.calls == []
    set_libraries(conn, '["1"]')
    plex.history_rows = [row(1, ago=timedelta(days=300))]
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1"]
    read_at = repository.get_link(conn, "amy").last_read_at
    set_libraries(conn, "[]")
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == []
    assert repository.get_link(conn, "amy").last_read_at == read_at


def test_plays_held_before_their_library_was_recorded_are_read_again(plex, conn, secret_key):
    link(conn, "amy", 4242)
    plex.history_rows = [row(1, ago=timedelta(days=90))]
    sync.read_plays(conn, secret_key)
    conn.execute("ALTER TABLE plex_links DROP COLUMN libraries")
    conn.execute("ALTER TABLE plex_links DROP COLUMN last_tried_at")
    conn.execute("ALTER TABLE plex_plays DROP COLUMN library_id")
    conn.commit()
    _add_missing_columns(conn)
    plex.history_rows.append(row(2, ago=timedelta(days=99)))
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1", "Film 2"]
    set_libraries(conn, '["1"]')
    plex.answers["GET", f"{SERVER}/identity"] = (503, "")
    sync.read_plays(conn, secret_key)
    assert titles(conn, "amy") == ["Film 1", "Film 2"]
