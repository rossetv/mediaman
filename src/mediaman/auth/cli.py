"""CLI commands for admin user management."""

import argparse
import getpass
import sys

from mediaman.config import load_config
from mediaman.db import init_db
from mediaman.auth.session import create_user


def create_user_cli() -> None:
    """CLI entry point for creating admin users."""
    parser = argparse.ArgumentParser(description="Create a mediaman admin user")
    parser.add_argument("--username", required=True, help="Admin username")
    parser.add_argument("--password", help="Password (prompted if omitted)")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    if len(password) < 8:
        print("Error: password must be at least 8 characters", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    db_path = f"{config.data_dir}/mediaman.db"
    conn = init_db(db_path)

    try:
        create_user(conn, args.username, password)
        print(f"Admin user '{args.username}' created successfully.")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()
