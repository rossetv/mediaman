"""History rows: a short line, the full event behind it, and nothing sensitive in either."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.core.audit import security_event
from mediaman.scanner.repository.audit import event_page
from mediaman.web.routes._history_view import _VALUE_MAX, describe, redact_text
from mediaman.web.routes.history import router as history_router
from tests.helpers.factories import insert_audit_log

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parents[3] / "src" / "mediaman" / "web" / "templates")
)


def _describe(**over):
    args = {
        "event_id": 7,
        "action": "sec:login.success",
        "action_label": "Signed in",
        "detail": "actor=admin ip=1.2.3.4",
        "actor": "admin",
        "created_at": "2026-11-24T10:15:03.123456+00:00",
        "is_security": True,
        "title": "Signed in",
        "media_item_id": "_security",
        "plex_rating_key": None,
        "space_impact": "",
    }
    return describe(**{**args, **over})


def _by_label(view) -> dict[str, str]:
    return {f["label"]: f["value"] for f in view.fields}


def _all_values(view) -> str:
    return " ".join(str(f["value"]) for f in view.fields)


class TestDetailList:
    def test_security_event_lists_who_ip_time_and_id(self):
        fields = _by_label(_describe())
        assert fields["Event"] == "Signed in (sec:login.success)"
        assert fields["Time"] == "2026-11-24 10:15:03 UTC"
        assert (fields["Who"], fields["IP address"], fields["Event ID"]) == (
            "admin",
            "1.2.3.4",
            "7",
        )

    def test_a_typed_name_that_looks_like_an_address_is_not_the_address(self):
        view = _describe(detail="actor=x ip=9.9.9.9 ip=1.2.3.4", actor="x ip=9.9.9.9")
        assert view.ip == "1.2.3.4"
        assert view.actor == "x ip=9.9.9.9"

    def test_each_json_key_is_its_own_row(self):
        view = _describe(
            action="sec:user.created",
            action_label="User created",
            detail='actor=admin ip=1.2.3.4 {"new_username":"bob","role":"viewer"}',
        )
        fields = _by_label(view)
        assert (fields["New username"], fields["Role"]) == ("bob", "viewer")
        assert view.summary == "bob (viewer)"

    def test_empty_values_say_not_recorded(self):
        view = _describe(detail="actor=- ip=-", actor="")
        fields = _by_label(view)
        assert fields["Who"] == fields["IP address"] == fields["Details"] == "Not recorded"
        assert view.ip is None and view.actor is None

    def test_old_rows_without_an_actor_column_still_read(self):
        view = _describe(detail="actor=bob ip=2.2.2.2 hello there", actor=None)
        fields = _by_label(view)
        assert (fields["Who"], fields["IP address"], fields["Details"]) == (
            "bob",
            "2.2.2.2",
            "hello there",
        )

    def test_a_body_that_is_not_json_or_not_in_the_usual_shape_is_kept_whole(self):
        broken = _by_label(_describe(detail='actor=admin ip=1.2.3.4 {"half":'))
        assert broken["Details"] == '{"half":'
        odd = _by_label(_describe(action="sec:future.thing", detail="something else entirely"))
        assert odd["Details"] == "something else entirely"
        assert odd["Event"] == "Signed in (sec:future.thing)"

    def test_media_event_lists_title_ids_and_space(self):
        view = _describe(
            action="deleted",
            action_label="deleted",
            detail="Deleted: Heat [rk:55]",
            actor="admin",
            is_security=False,
            title="Heat",
            media_item_id="m1",
            plex_rating_key="55",
            space_impact="1.5 GB",
        )
        fields = _by_label(view)
        assert fields["Title"] == "Heat"
        assert (fields["Media ID"], fields["Plex key"]) == ("m1", "55")
        assert fields["Space reclaimed"] == "1.5 GB"
        assert fields["Details"] == "Deleted: Heat [rk:55]"
        assert view.summary == "1.5 GB"

    @pytest.mark.parametrize(
        ("actor", "summary"), [("amy", ""), ("boss", "amy")], ids=["by-self", "by-another"]
    )
    def test_an_unlinked_plex_account_names_its_owner_when_another_did_it(self, actor, summary):
        view = _describe(
            action="sec:plex.unlinked",
            detail=f'actor={actor} ip=1.2.3.4 {{"user":"amy","reason":"admin"}}',
            actor=actor,
        )
        assert view.summary == summary

    @pytest.mark.parametrize(
        ("body", "summary"),
        [
            ('{"email":"a@x.example","previous":null}', ""),
            ('{"email":"a@x.example","previous":null,"target_username":"amy"}', "amy"),
        ],
        ids=["own", "someone-elses"],
    )
    def test_an_email_changed_for_someone_else_names_them_but_no_address(self, body, summary):
        view = _describe(action="sec:user.email_updated", detail=f"actor=admin ip=1.2.3.4 {body}")
        assert view.summary == summary

    @pytest.mark.parametrize(
        "stored", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00", "soon"]
    )
    def test_a_time_out_of_range_is_shown_as_stored(self, stored):
        assert _by_label(_describe(created_at=stored))["Time"] == stored

    def test_a_long_value_is_whole_and_a_runaway_one_says_how_much_is_cut(self):
        long = "word " * 3000
        assert _by_label(_describe(detail=f"actor=a ip=1.1.1.1 {long}"))["Details"] == long
        runaway = _by_label(_describe(detail="actor=a ip=1.1.1.1 " + "x" * (_VALUE_MAX + 1234)))
        assert runaway["Details"] == "x" * _VALUE_MAX + "… (1,234 more characters)"


class TestNothingSensitive:
    def test_ua_hash_and_credential_values_never_appear(self):
        view = _describe(
            detail=(
                'actor=admin ip=1.2.3.4 {"ua_hash":"abc123deadbeef","api_token":"s3cretvalue",'
                '"nested":{"password":"hunter2"},"reason":"ok"}'
            )
        )
        shown = _all_values(view)
        for leaked in ("abc123deadbeef", "s3cretvalue", "hunter2", "ua_hash"):
            assert leaked not in shown
        assert _by_label(view)["Reason"] == "ok"
        assert _by_label(view)["Api token"] == "[hidden]"

    def test_a_token_in_free_text_is_hidden(self):
        view = _describe(
            action="delete_failed",
            action_label="delete failed",
            is_security=False,
            detail="Delete error: Heat — Error: GET http://plex:32400/x?X-Plex-Token=abcd1234 failed",
        )
        assert "abcd1234" not in _all_values(view)
        assert "failed" in _by_label(view)["Details"]

    def test_a_credential_is_hidden_before_the_text_is_cut_short(self):
        text = "x" * 480 + " password=SUPERSECRETVALUE"
        value = _by_label(_describe(detail="actor=a ip=1.1.1.1 " + text))["Details"]
        assert "SUPER" not in value and "[hidden]" in value

    def test_bearer_headers_and_quoted_json_secrets_are_hidden(self):
        out = redact_text('Authorization: Bearer abc.def {"secret": "zzz"} cookie=a=b')
        assert not re.search(r"abc\.def|zzz|a=b", out)

    @pytest.mark.parametrize(
        ("text", "leaks"),
        [
            ("GET http://bob:hunter2@plex:32400/x failed", ["bob", "hunter2"]),
            ("at https://s3cr3t@host/", ["s3cr3t"]),
            ("http://bob:hun@ter2@plex/x", ["hun", "ter2"]),
            ("Authorization: Basic Ym9iOmh1", ["Ym9iOmh1"]),
            ("Authorization: Weird Ym9iOmh1", ["Ym9iOmh1"]),
            (
                'Authorization: Digest username="bob b", response="f00d"',
                ["bob", "f00d"],
            ),
            ("sent Bearer abc.def-9 and", ["abc.def-9"]),
            ("sent bearer%20abc9", ["abc9"]),
            (r'{"password":"zq\"vv tail","x":1}', ["zq", "vv", "tail"]),
            (r"{'secret':'pa\'ss tail'}", ["pa", "tail"]),
            (r'{"body":"{\"token\":\"ab cd\"}"}', ["ab", "cd"]),
            ("next=%2Fx%3FX-Plex-Token%3Dabcd1234", ["abcd1234"]),
            ("next=%252Fx%253FX-Plex-Token%253Dabcd1234", ["abcd1234"]),
            ("Cookie: a=bbb; c=ddd", ["bbb", "ddd"]),
            ('password="never closed', ["never", "closed"]),
            ("{'token' => 'zzz'}", ["zzz"]),
            ("sent Bearer abcdefghijklmnop and", ["abcdefghijklmnop"]),
            ("bearer=hunter", ["hunter"]),
            ("http://bob:hun/ter@host/x", ["bob", "hun", "ter"]),
            ("http://bob:/hun/ter@host", ["bob", "hun", "ter"]),
            ("http://bob:hun/t@er/x@host", ["bob", "hun"]),
            ("http://a:b/c see http://bob:hun/ter@host", ["bob", "hun", "ter"]),
            ('tokens="abc", "def" , "ghi"', ["abc", "def", "ghi"]),
            ("tokens='abc','def'", ["abc", "def"]),
            (r"\"tokens\":\"abc\", \"def\"", ["abc", "def"]),
            ('tokens: ["abc", "de]f", "ghi"] end', ["abc", "de]f", "ghi"]),
            ("{'secrets': ('abc', ['def', 'ghi'])}", ["abc", "def", "ghi"]),
            ('{"tokens": {"plex": "abc", "x": "def"}, "n": 1}', ["abc", "def"]),
            ("tokens=[abc, def", ["abc", "def"]),
            ('Cookie: "a=abc", "b=def"', ["abc", "def"]),
        ],
    )
    def test_every_shape_of_credential_is_hidden(self, text, leaks):
        out = redact_text(text)
        assert "[hidden]" in out
        assert [leak for leak in leaks if leak in out] == []

    def test_text_beside_a_credential_still_reads(self):
        out = redact_text(
            "GET http://bob:pw@plex:32400/x?X-Plex-Token=abc failed for bob@x.example"
        )
        assert (
            out == "GET http://[hidden]@plex:32400/x?X-Plex-Token=[hidden] failed for bob@x.example"
        )
        plain = "Not deleted: Heat — https://plex:32400/x: 2 tokens, hash of the file"
        assert redact_text(plain) == plain

    @pytest.mark.parametrize(
        "text",
        [
            "Not deleted: The Bearer Bonds",
            "The Bearer: Redemption (1999)",
            "term=The+Bearer+Bonds+2",
            "Bearer%20Bonds%20x",
            "GET http://plex:32400/photo/@2x and http://[::1]:32400/a@b",
            '{"key":["abc", "def"]}',
        ],
    )
    def test_text_that_holds_no_credential_is_left_alone(self, text):
        assert redact_text(text) == text

    def test_what_follows_a_hidden_value_or_list_still_reads(self):
        assert redact_text('{"token":"abc","title":"Heat"}') == '{"token":[hidden],"title":"Heat"}'
        assert (
            redact_text("{'tokens': ['abc', 'def'], 'title': 'Heat'}")
            == "{'tokens': [hidden], 'title': 'Heat'}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "token",
            "a.",
            "token.",
            '\\"',
            "token\\",
            "://",
            "://a:",
            "://a:@",
            "token ",
            'token="',
            'token="a",',
            "token=[",
            "token=[]",
            "token=[\\",
            "bearer ",
            "bearer a",
            "bearer%20",
        ],
    )
    def test_hiding_takes_no_longer_than_the_text_is_long(self, text):
        started = time.process_time()
        redact_text(text * (200_000 // len(text)))
        assert time.process_time() - started < 2

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("Details", "{0}"),
            ("Stored record", "{0}"),
            ("Note", '{{"note":"{0}"}}'),
            ("Stored record", '{{"note":"{0}"}}'),
        ],
    )
    def test_a_credential_at_the_cut_is_hidden_first(self, label, body):
        # The @ that tells an address's password apart lies past the cut.
        head = "actor=a ip=1.1.1.1 "
        before = len(head) + len(body.format("")) if label == "Stored record" else 0
        filler = "x" * (_VALUE_MAX - before - len("http://bob:hunter2"))
        secret = "http://bob:hunter2pass@plex/x " + "y" * 100
        fields = _by_label(_describe(detail=head + body.format(filler + secret)))
        assert "hunter2" not in fields[label]
        assert "[hidden]" in fields[label]

    def test_main_line_summary_never_carries_an_address_email_or_typed_name(self):
        for event, detail in (
            ("subscriber.added", '{"email":"a@b.example"}'),
            ("subscriber.removed", '{"id":3,"email":"a@b.example"}'),
            ("login.failed", '{"reason":"bad_credentials"}'),
            ("library.sync.failed", '{"error":"boom http://10.0.0.5:32400"}'),
        ):
            view = _describe(
                action=f"sec:{event}",
                detail=f"actor=typed-name ip=8.8.8.8 {detail}",
                actor="typed-name",
            )
            assert view.summary == ""

    def test_a_name_that_is_an_email_is_off_the_main_line(self):
        view = _describe(
            action="sec:user.created",
            detail='actor=admin ip=1.2.3.4 {"new_username":"bob@x.example","role":"viewer"}',
        )
        assert view.summary == "[hidden] (viewer)"
        assert _by_label(view)["New username"] == "bob@x.example"

    def test_failed_sign_in_names_the_typed_name_only_in_the_details(self):
        view = _describe(
            action="sec:login.failed",
            detail='actor=typed-name ip=8.8.8.8 {"reason":"bad_credentials"}',
            actor="typed-name",
        )
        assert _by_label(view)["Name typed"] == "typed-name"
        assert "Who" not in _by_label(view)
        assert "typed-name" not in view.summary


class TestEventPage:
    def _rows(self, conn, n, action="scanned"):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            insert_audit_log(
                conn, media_item_id=f"m{i}", action=action, created_at=base + timedelta(minutes=i)
            )
            for i in range(n)
        ]

    def test_finds_the_page_in_newest_first_order(self, conn):
        ids = self._rows(conn, 30)
        assert event_page(conn, action=None, event_id=ids[-1], per_page=25) == 1
        assert event_page(conn, action=None, event_id=ids[4], per_page=25) == 2

    def test_rows_with_the_same_time_are_ordered_by_id(self, conn):
        same = datetime(2026, 1, 1, tzinfo=UTC)
        ids = [
            insert_audit_log(conn, media_item_id=f"m{i}", action="scanned", created_at=same)
            for i in range(3)
        ]
        assert [event_page(conn, action=None, event_id=i, per_page=2) for i in ids] == [2, 1, 1]

    def test_none_when_missing_or_hidden_by_the_filter(self, conn):
        ids = self._rows(conn, 2)
        assert event_page(conn, action=None, event_id=999, per_page=25) is None
        assert event_page(conn, action="deleted", event_id=ids[0], per_page=25) is None


class TestPage:
    def _client(self, app_factory, authed_client, conn):
        app = app_factory(history_router, conn=conn, state_extras={"templates": _TEMPLATES})
        return authed_client(app, conn)

    def _event_at(self, conn, stored):
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, created_at) VALUES ('m1', 'scanned', ?)",
            (stored,),
        )
        conn.commit()

    def test_every_row_is_a_details_element_and_the_script_is_loaded(
        self, app_factory, authed_client, conn
    ):
        insert_audit_log(conn, media_item_id="m1", action="scanned")
        html = self._client(app_factory, authed_client, conn).get("/history").text
        assert html.count("<details") == html.count("<summary") == 1
        assert re.search(r'<script src="/static/js/history\.js[^"]*" defer>', html)

    @pytest.mark.parametrize("stored", ["2026-11-24T10:15:03+00:00", "2026-11-24T10:15:03"])
    def test_a_time_carries_its_utc_moment_and_names_its_zone(
        self, app_factory, authed_client, conn, stored
    ):
        self._event_at(conn, stored)
        html = self._client(app_factory, authed_client, conn).get("/history").text
        times = re.findall(r"<time ([^>]*)>([^<]*)</time>", html)
        assert [text for _, text in times] == ["2026-11-24 10:15 UTC", "2026-11-24 10:15:03 UTC"]
        assert all('datetime="2026-11-24T10:15:03Z"' in attrs for attrs, _ in times)
        assert 'title="2026-11-24 10:15:03 UTC"' in times[0][0]

    def test_a_stored_time_that_cannot_be_read_is_shown_without_a_moment(
        self, app_factory, authed_client, conn
    ):
        self._event_at(conn, "soon")
        html = self._client(app_factory, authed_client, conn).get("/history").text
        assert "<time" not in html
        assert '<span class="hist-date">soon</span>' in html

    def test_deep_link_opens_the_right_row_on_its_page_under_the_filter(
        self, app_factory, authed_client, conn
    ):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        ids = [
            insert_audit_log(
                conn,
                media_item_id=f"m{i}",
                action="deleted",
                created_at=base + timedelta(minutes=i),
            )
            for i in range(30)
        ]
        client = self._client(app_factory, authed_client, conn)
        html = client.get(f"/history?action=deleted&event={ids[0]}").text
        assert re.findall(r"<details[^>]* open>", html) == [
            f'<details class="hist" id="event-{ids[0]}" data-event="{ids[0]}" open>'
        ]
        assert re.findall(r'data-event="(\d+)"', html)[-1] == str(ids[0])
        assert 'rel="next"' not in html
        hidden = client.get(f"/history?action=kept&event={ids[0]}").text
        assert not re.findall(r"<details[^>]* open>", hidden)

    def test_a_bad_event_value_is_ignored(self, app_factory, authed_client, conn):
        insert_audit_log(conn, media_item_id="m1", action="scanned")
        client = self._client(app_factory, authed_client, conn)
        for bad in ("abc", "-1", "9" * 40, ""):
            resp = client.get(f"/history?event={bad}")
            assert resp.status_code == 200
            assert 'data-event="1" open' not in resp.text

    def test_api_items_gain_the_new_keys_and_keep_title_and_detail(
        self, app_factory, authed_client, conn
    ):
        security_event(
            conn,
            event="user.created",
            actor="admin",
            ip="1.2.3.4",
            detail={"new_username": "bob", "role": "viewer"},
        )
        item = self._client(app_factory, authed_client, conn).get("/api/history").json()["items"][0]
        assert item["title"] == "User created"
        assert item["detail"] == "admin from 1.2.3.4"
        assert (item["summary"], item["actor"], item["ip"]) == ("bob (viewer)", "admin", "1.2.3.4")
        assert {"label": "Role", "value": "viewer", "empty": False} in item["fields"]

    def test_a_typed_name_cannot_pose_as_the_address_in_the_api_detail(
        self, app_factory, authed_client, conn
    ):
        security_event(conn, event="login.failed", actor="x ip=9.9.9.9", ip="1.2.3.4")
        item = self._client(app_factory, authed_client, conn).get("/api/history").json()["items"][0]
        assert item["ip"] == "1.2.3.4"


def test_a_quoted_file_path_is_not_taken_for_the_title(app_factory, authed_client, conn):
    insert_audit_log(
        conn,
        media_item_id="gone",
        action="delete_failed_permanent",
        detail="Permanent delete error: OSError: [Errno 13] denied: '/media/movies/Heat/heat.mkv'",
    )
    app = app_factory(history_router, conn=conn, state_extras={"templates": _TEMPLATES})
    item = authed_client(app, conn).get("/api/history").json()["items"][0]
    assert item["title"] == "Unknown"


class TestMainLine:
    def _row(self, app_factory, authed_client, conn, **row):
        insert_audit_log(conn, **row)
        app = app_factory(history_router, conn=conn, state_extras={"templates": _TEMPLATES})
        client = authed_client(app, conn)
        html = client.get("/history").text
        [line] = re.findall(r"<summary.*?</summary>", html, re.S)
        return line, client.get("/api/history").json()["items"][0]

    def test_a_folder_row_has_no_path(self, app_factory, authed_client, conn):
        line, item = self._row(
            app_factory,
            authed_client,
            conn,
            media_item_id="_delete_root:/mnt/x",
            action="delete_root_unavailable",
            detail="Delete root or folder '/mnt/x' is missing or empty",
        )
        assert "/mnt/x" not in line and "Delete folder" in line
        assert item["title"] == "/mnt/x"

    def test_a_deletion_problem_takes_no_quoted_text_for_its_title(
        self, app_factory, authed_client, conn
    ):
        line, item = self._row(
            app_factory,
            authed_client,
            conn,
            media_item_id="gone",
            action="delete_failed_permanent",
            detail="Permanent delete error: OSError: denied: 'movies/Heat/heat.mkv'",
        )
        assert "heat.mkv" not in line and "Unknown" in line
        assert item["title"] == "movies/Heat/heat.mkv"

    def test_an_email_taken_from_the_stored_text_is_not_on_it(
        self, app_factory, authed_client, conn
    ):
        line, _item = self._row(
            app_factory,
            authed_client,
            conn,
            media_item_id="gone",
            action="downloaded",
            detail="asked for by 'amy@x.example'",
        )
        assert "amy@x.example" not in line

    def test_the_api_hides_credentials_in_a_media_row(self, app_factory, authed_client, conn):
        _line, item = self._row(
            app_factory,
            authed_client,
            conn,
            media_item_id="gone",
            action="scanned",
            detail="GET 'http://plex/x?X-Plex-Token=abcd1234' failed",
        )
        assert "abcd1234" not in item["detail"] + item["title"]
        assert item["detail"].startswith("GET 'http://plex/x?X-Plex-Token=[hidden]")


class TestUnclosedBracket:
    def test_an_unclosed_bracket_hides_everything_after_it_even_on_later_lines(self):
        out = redact_text("token=[abc def\nnext line\nmore")
        assert out == "token=[hidden]"

    def test_a_quoted_secret_holding_a_newline_is_hidden_to_its_end(self):
        out = redact_text('tokens=["ab\ncd')
        assert "cd" not in out and "ab" not in out

    def test_a_list_closed_on_a_later_line_is_hidden_whole(self):
        out = redact_text('tokens: [\n"abc",\n"def"\n] after')
        assert out == "tokens: [hidden] after"
