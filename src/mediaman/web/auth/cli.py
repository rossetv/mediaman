"""CLI commands for admin user management."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from mediaman.bootstrap.data_dir import DataDirNotWritableError, assert_data_dir_writable
from mediaman.config import Config, ConfigError, load_config
from mediaman.db import init_db
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.password_policy import password_issues


def _prompt_username() -> str:
    """Prompt interactively for a non-empty username.

    Mirrors the password-prompt fallback so an operator running the
    standard ``docker compose exec mediaman mediaman-create-user`` (no
    flags) gets a guided flow rather than an argparse error. Re-prompts
    on empty input rather than letting an unusable empty string through.
    """
    for _ in range(3):
        candidate = input("Username: ").strip()
        if candidate:
            return candidate
        print("Username must not be empty.", file=sys.stderr)
    print("No username provided after 3 attempts; aborting.", file=sys.stderr)
    sys.exit(1)


def _read_password_from_stdin() -> str:
    """Read a password from stdin (no prompt, no echo handling).

    Used by ``--password-stdin`` so an operator can pipe a secret in via
    ``cat secret | mediaman-create-user --password-stdin`` without it
    appearing in the process table or shell history (the failure mode
    that ``--password`` exposes).
    """
    pw = sys.stdin.readline()
    # Strip exactly one trailing newline; preserve any internal whitespace
    # the operator may legitimately want as part of the password.
    return pw.rstrip("\n").rstrip("\r")


def _resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Acquire ``(username, password)`` from flags, stdin, or interactive prompt.

    Enforces the ``--password`` / ``--password-stdin`` mutual exclusion,
    then resolves the username (flag or interactive prompt) and the
    password (stdin, flag, or interactive prompt). Exits the process
    with the appropriate non-zero status on a mutual-exclusion clash or
    a password that fails the strength policy.
    """
    if args.password and args.password_stdin:
        print(
            "Error: --password and --password-stdin are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(2)

    username = (args.username or "").strip() or _prompt_username()

    if args.password_stdin:
        password = _read_password_from_stdin()
    else:
        password = args.password or getpass.getpass("Password: ")

    issues = password_issues(password, username=username)
    if issues:
        print("Error: password does not meet the strength policy:", file=sys.stderr)
        for item in issues:
            print(f"  - {item}", file=sys.stderr)
        sys.exit(1)

    return username, password


def _preflight_data_dir(config: Config) -> Path:
    """Create the data dir if absent and confirm it is writable.

    Mirrors the bootstrap layer: runs the same data-dir writability
    preflight ``bootstrap_db`` uses so the operator gets the actionable
    ``chown`` hint instead of an opaque sqlite traceback when the bind
    mount is owned by the wrong uid. Exits the process with status 1 on
    a writability or creation failure; otherwise returns the data-dir
    :class:`~pathlib.Path`.
    """
    data_dir = Path(config.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        assert_data_dir_writable(data_dir)
    except DataDirNotWritableError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(
            f"Error: data dir {data_dir} could not be created: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    return data_dir


def create_user_cli() -> None:
    """CLI entry point for creating admin users.

    ``--username`` and ``--password`` are both optional and prompted
    interactively when omitted. ``--password`` on the command line is
    accepted but discouraged (it leaks via ``ps``, shell history, and
    audit logs); ``--password-stdin`` is the preferred non-interactive
    path.
    """
    parser = argparse.ArgumentParser(description="Create a mediaman admin user")
    parser.add_argument(
        "--username",
        help="Admin username (prompted interactively if omitted)",
    )
    parser.add_argument(
        "--password",
        help=(
            "Password (prompted if omitted). Avoid in production — the value "
            "is captured by the process table and shell history. Prefer "
            "--password-stdin."
        ),
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin (one line). Mutually exclusive with --password.",
    )
    args = parser.parse_args()

    username, password = _resolve_credentials(args)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Error: configuration is invalid: {exc}", file=sys.stderr)
        sys.exit(1)

    data_dir = _preflight_data_dir(config)

    db_path = str(data_dir / "mediaman.db")
    conn = init_db(db_path)

    try:
        create_user(conn, username, password)
        print(f"Admin user '{username}' created successfully.")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()
