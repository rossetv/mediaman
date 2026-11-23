"""Roles on users: validation, the last-admin guard and the CLI default."""

from __future__ import annotations

import sys

import pytest

from mediaman.db import init_db
from mediaman.web.auth import cli as cli_mod
from mediaman.web.auth.password_hash import (
    LastAdminError,
    create_user,
    delete_user,
    get_user_role,
    list_users,
    set_user_role,
    user_must_change_password,
)
from tests.helpers.factories import insert_admin_user


def _id(conn, username):
    return conn.execute("SELECT id FROM admin_users WHERE username=?", (username,)).fetchone()["id"]


def test_create_user_defaults_to_admin_and_accepts_viewer(conn):
    create_user(conn, "amy", "password1234", enforce_policy=False)
    create_user(conn, "vic", "password1234", enforce_policy=False, role="viewer")

    assert get_user_role(conn, "amy") == "admin"
    assert get_user_role(conn, "vic") == "viewer"
    assert {u["username"]: u["role"] for u in list_users(conn)} == {"amy": "admin", "vic": "viewer"}
    assert get_user_role(conn, "ghost") is None


def test_create_user_rejects_an_unknown_role_and_writes_nothing(conn):
    with pytest.raises(ValueError):
        create_user(conn, "amy", "password1234", enforce_policy=False, role="owner")

    assert list_users(conn) == []


def test_create_user_can_flag_the_account_for_a_password_change(conn):
    create_user(conn, "amy", "password1234", enforce_policy=False, must_change_password=True)
    create_user(conn, "bo", "password1234", enforce_policy=False)

    assert user_must_change_password(conn, "amy") is True
    assert user_must_change_password(conn, "bo") is False


def test_the_last_admin_cannot_be_demoted(conn):
    insert_admin_user(conn, username="amy")
    insert_admin_user(conn, username="vic", role="viewer")

    with pytest.raises(LastAdminError):
        set_user_role(conn, "amy", "viewer")

    assert get_user_role(conn, "amy") == "admin"


def test_an_admin_can_be_demoted_while_another_admin_remains(conn):
    insert_admin_user(conn, username="amy")
    insert_admin_user(conn, username="bo")

    assert set_user_role(conn, "amy", "viewer") is True

    assert get_user_role(conn, "amy") == "viewer"
    with pytest.raises(LastAdminError):
        set_user_role(conn, "bo", "viewer")


def test_a_viewer_can_be_promoted_and_an_unknown_user_is_reported(conn):
    insert_admin_user(conn, username="amy")
    insert_admin_user(conn, username="vic", role="viewer")

    assert set_user_role(conn, "vic", "admin") is True
    assert set_user_role(conn, "ghost", "admin") is False
    with pytest.raises(ValueError):
        set_user_role(conn, "vic", "root")


def test_the_last_admin_cannot_be_deleted_even_when_viewers_remain(conn):
    insert_admin_user(conn, username="amy")
    insert_admin_user(conn, username="vic", role="viewer")
    insert_admin_user(conn, username="cal", role="viewer")

    assert delete_user(conn, _id(conn, "amy"), "cal") is False

    assert get_user_role(conn, "amy") == "admin"


def test_a_viewer_can_be_deleted_and_so_can_an_admin_when_another_remains(conn):
    insert_admin_user(conn, username="amy")
    insert_admin_user(conn, username="bo")
    insert_admin_user(conn, username="vic", role="viewer")

    assert delete_user(conn, _id(conn, "vic"), "amy") is True
    assert delete_user(conn, _id(conn, "bo"), "amy") is True
    assert [u["username"] for u in list_users(conn)] == ["amy"]


class TestCreateUserCli:
    def _run(self, monkeypatch, tmp_path, *extra):
        monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "0123456789abcdef" * 4)
        monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mediaman-create-user",
                "--username",
                "newbie",
                "--password",
                "Correct-Horse-9!x",
                *extra,
            ],
        )
        cli_mod.create_user_cli()
        conn = init_db(str(tmp_path / "mediaman.db"))
        try:
            return get_user_role(conn, "newbie")
        finally:
            conn.close()

    def test_role_defaults_to_admin(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path) == "admin"

    def test_role_viewer_creates_a_viewer(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path, "--role", "viewer") == "viewer"

    def test_an_unknown_role_is_refused(self, monkeypatch, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            self._run(monkeypatch, tmp_path, "--role", "owner")

        assert excinfo.value.code == 2
