"""Shared test fixtures."""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary data directory."""
    return tmp_path


@pytest.fixture
def db_path(tmp_data_dir):
    """Provide a temporary database path."""
    return tmp_data_dir / "mediaman.db"


@pytest.fixture
def secret_key():
    """Provide a strong test secret key (64 hex chars, ~256 bits).

    Deterministic so tests get reproducible HMAC outputs; passes the
    entropy check in :mod:`mediaman.config`.
    """
    return "0123456789abcdef" * 4  # 64 hex chars, 16 unique, test-stable
