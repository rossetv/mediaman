"""Tests for the ``mediaman-create-user`` CLI."""

from __future__ import annotations

import io
import sys

import pytest

from mediaman.auth import cli as cli_mod


def _enable_config(monkeypatch, tmp_path):
    """Set the env vars load_config() needs to succeed."""
    # 64 hex chars, 16 unique — passes the entropy check.
    monkeypatch.setenv("MEDIAMAN_SECRET_KEY", "0123456789abcdef" * 4)
    monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))


class TestCreateUserCli:
    """Verify the create-user CLI matches the README's "no flags" example."""

    def test_username_optional_prompts_interactively(self, monkeypatch, tmp_path, capsys):
        """``mediaman-create-user`` with no flags must prompt — finding 19."""
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

    def test_config_error_exits_cleanly(self, monkeypatch, tmp_path, capsys):
        """A missing MEDIAMAN_SECRET_KEY prints an actionable error — finding 21."""
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
