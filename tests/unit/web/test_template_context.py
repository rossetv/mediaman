"""What templates are given about the signed-in person."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import Request

from mediaman.app_factory import create_app
from mediaman.db import set_connection
from mediaman.web import template_context
from mediaman.web.repository.avatars import avatar_url
from mediaman.web.template_context import signed_in_avatar
from tests.helpers.factories import insert_admin_user


def _request(username: str | None) -> Request:
    state = {} if username is None else {"username": username}
    return Request({"type": "http", "headers": [], "state": state})


def test_the_signed_in_person_s_picture_is_theirs(conn):
    insert_admin_user(conn, username="robin")
    insert_admin_user(conn, username="sam")
    set_connection(conn)

    given = {name: signed_in_avatar(_request(name))["avatar_url"] for name in ("robin", "sam")}

    assert given == {name: avatar_url(conn, name) for name in ("robin", "sam")}
    assert all(given.values())


def test_a_page_for_nobody_reads_no_database(monkeypatch):
    monkeypatch.setattr(template_context, "get_db", lambda: pytest.fail("read the database"))

    assert signed_in_avatar(_request(None)) == {"avatar_url": None}


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("locked"), RuntimeError("no db")])
def test_a_page_is_still_drawn_when_the_picture_cannot_be_read(monkeypatch, failure):
    def fail():
        raise failure

    monkeypatch.setattr(template_context, "get_db", fail)

    assert signed_in_avatar(_request("robin")) == {"avatar_url": None}


def test_the_app_gives_it_to_every_template():
    templates = create_app().state.templates

    assert signed_in_avatar in templates.context_processors
