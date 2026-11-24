"""History rows: a short line, the full event behind it, and nothing sensitive in either."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.templating import Jinja2Templates

from mediaman.core.audit import security_event
from mediaman.scanner.repository.audit import event_page
from mediaman.web.routes._history_view import describe, redact_text
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

    def test_every_row_is_a_details_element_and_the_script_is_loaded(
        self, app_factory, authed_client, conn
    ):
        insert_audit_log(conn, media_item_id="m1", action="scanned")
        html = self._client(app_factory, authed_client, conn).get("/history").text
        assert html.count("<details") == html.count("<summary") == 1
        assert re.search(r'<script src="/static/js/history\.js[^"]*" defer>', html)

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
        assert "Page 2 of 2" in html
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
