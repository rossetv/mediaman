from __future__ import annotations

import io
import sys

import pytest

from mediaman.db import init_db
from mediaman.web.auth import cli as cli_mod
from mediaman.web.auth.password_hash import authenticate, create_user


def _enable_config(monkeypatch, tmp_path):
    """Set the env vars load_config() needs to succeed."""
    # 64 hex chars, 16 unique — passes the entropy check.
    monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "0123456789abcdef" * 4)
    monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))


class TestCreateUserCli:
    """Verify the create-user CLI matches the README's "no flags" example."""

    def test_username_optional_prompts_interactively(self, monkeypatch, tmp_path, capsys):
        """``mediaman-create-user`` with no flags must prompt."""
        _enable_config(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["mediaman-create-user"])

        # Replace stdin so input() returns the username.
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "alice")
        # Replace getpass for the password.
        monkeypatch.setattr(
            cli_mod.getpass, "getpass", lambda *_a, **_kw: "Correct-Horse-Battery-9!"
        )

        # Stub the actual user creation so we don't write a real DB row;
        # the test is about argparse + prompt wiring, not the auth path.
        recorded = {}

        def fake_create_user(conn, username, password):
            recorded["username"] = username
            recorded["password"] = password

        monkeypatch.setattr(cli_mod, "create_user", fake_create_user)

        cli_mod.create_user_cli()

        assert recorded["username"] == "alice"
        assert recorded["password"] == "Correct-Horse-Battery-9!"
        out = capsys.readouterr().out
        assert "alice" in out

    def test_empty_interactive_username_after_three_attempts_aborts(self, monkeypatch, tmp_path):
        """Three blank username inputs in a row exit non-zero."""
        _enable_config(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["mediaman-create-user"])
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")
        monkeypatch.setattr(cli_mod.getpass, "getpass", lambda *_a, **_kw: "x")

        with pytest.raises(SystemExit) as excinfo:
            cli_mod.create_user_cli()
        assert excinfo.value.code == 1

    def test_password_stdin_reads_one_line(self, monkeypatch, tmp_path):
        """``--password-stdin`` consumes a single line from stdin."""
        _enable_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            ["mediaman-create-user", "--username", "bob", "--password-stdin"],
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO("Correct-Horse-Battery-9!\n"))

        recorded = {}

        def fake_create_user(conn, username, password):
            recorded["username"] = username
            recorded["password"] = password

        monkeypatch.setattr(cli_mod, "create_user", fake_create_user)

        cli_mod.create_user_cli()

        assert recorded["username"] == "bob"
        assert recorded["password"] == "Correct-Horse-Battery-9!"

    def test_password_and_password_stdin_are_mutually_exclusive(self, monkeypatch, tmp_path):
        """Setting both flags is a hard error — neither option should win."""
        _enable_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mediaman-create-user",
                "--username",
                "carol",
                "--password",
                "x",
                "--password-stdin",
            ],
        )

        with pytest.raises(SystemExit) as excinfo:
            cli_mod.create_user_cli()
        assert excinfo.value.code == 2

    def test_duplicate_username_exits_cleanly_without_traceback(
        self, monkeypatch, tmp_path, capsys
    ):
        """Creating an existing user must exit(1) with a clean message.

        ``create_user`` raises ``UserExistsError`` (an ``Exception``
        subclass, not ``ValueError``) on a duplicate, so the CLI must
        catch it explicitly — otherwise the operator gets a raw
        traceback instead of "Error: User 'x' already exists".
        """
        from mediaman.web.auth.password_hash import UserExistsError

        _enable_config(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mediaman-create-user",
                "--username",
                "alice",
                "--password",
                "Correct-Horse-Battery-9!",
            ],
        )

        def fake_create_user(conn, username, password):
            raise UserExistsError(f"User '{username}' already exists")

        monkeypatch.setattr(cli_mod, "create_user", fake_create_user)

        with pytest.raises(SystemExit) as excinfo:
            cli_mod.create_user_cli()
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "already exists" in err

    def test_config_error_exits_cleanly(self, monkeypatch, tmp_path, capsys):
        """A missing MEDIAMAN_SECRET_KEY prints an actionable error."""
        # Deliberately do NOT call _enable_config — load_config() will raise.
        monkeypatch.delenv("MEDIAMAN_SECRET_KEY", raising=False)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mediaman-create-user",
                "--username",
                "dave",
                "--password",
                "Correct-Horse-Battery-9!",
            ],
        )

        with pytest.raises(SystemExit) as excinfo:
            cli_mod.create_user_cli()
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "configuration is invalid" in err


class TestUnlockUserCli:
    """``mediaman-unlock-user <name>`` clears a login lockout without a prompt."""

    def _run(self, monkeypatch, tmp_path, *argv):
        _enable_config(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["mediaman-unlock-user", *argv])
        cli_mod.unlock_user_cli()

    @staticmethod
    def _db(tmp_path):
        return init_db(str(tmp_path / "mediaman.db"))

    def test_locked_user_is_unlocked_and_can_log_in_again(self, monkeypatch, tmp_path, capsys):
        conn = self._db(tmp_path)
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(15):
            assert authenticate(conn, "alice", "wrong-password") is False
        assert authenticate(conn, "alice", "correct-password-123") is False
        conn.close()

        self._run(monkeypatch, tmp_path, "alice")

        assert "alice" in capsys.readouterr().out
        conn = self._db(tmp_path)
        assert authenticate(conn, "alice", "correct-password-123") is True
        audit = conn.execute(
            "SELECT 1 FROM audit_log WHERE action = 'sec:user.unlocked'"
        ).fetchone()
        assert audit is not None
        conn.close()

    def test_unknown_user_exits_non_zero_and_changes_nothing(self, monkeypatch, tmp_path, capsys):
        conn = self._db(tmp_path)
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        for _ in range(5):
            authenticate(conn, "alice", "wrong-password")
        before = conn.execute("SELECT * FROM login_failures").fetchall()
        audits = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.close()

        with pytest.raises(SystemExit) as excinfo:
            self._run(monkeypatch, tmp_path, "nobody")

        assert excinfo.value.code == 1
        assert "nobody" in capsys.readouterr().err
        conn = self._db(tmp_path)
        assert conn.execute("SELECT * FROM login_failures").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audits
        conn.close()

    def test_user_who_is_not_locked_exits_zero_and_says_so(self, monkeypatch, tmp_path, capsys):
        conn = self._db(tmp_path)
        create_user(conn, "alice", "correct-password-123", enforce_policy=False)
        conn.close()

        self._run(monkeypatch, tmp_path, "alice")  # returns, no SystemExit

        assert "not locked" in capsys.readouterr().out


class TestOldDatabaseRefusal:
    """A database older than the supported schema is refused cleanly by both CLIs."""

    def _old_db(self, monkeypatch, tmp_path):
        import sqlite3

        _enable_config(monkeypatch, tmp_path)
        path = tmp_path / "mediaman.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version=35")
        conn.close()
        return path

    @pytest.mark.parametrize(
        ("argv", "entry"),
        [
            (
                [
                    "mediaman-create-user",
                    "--username",
                    "alice",
                    "--password",
                    "Correct-Horse-Battery-9!",
                ],
                "create_user_cli",
            ),
            (["mediaman-unlock-user", "alice"], "unlock_user_cli"),
        ],
    )
    def test_cli_prints_the_refusal_and_leaves_the_file_alone(
        self, monkeypatch, tmp_path, capsys, argv, entry
    ):
        path = self._old_db(monkeypatch, tmp_path)
        before = path.read_bytes()
        monkeypatch.setattr(sys, "argv", argv)

        with pytest.raises(SystemExit) as excinfo:
            getattr(cli_mod, entry)()

        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "schema version 35" in err
        assert "Traceback" not in err
        assert path.read_bytes() == before
