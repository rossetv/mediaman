"""Fixtures of the scanner tests."""

from __future__ import annotations

import pytest

from mediaman.scanner import engine


@pytest.fixture
def prune_gone_libraries(monkeypatch):
    """Let a scan and a sync remove the items of a library Plex lists no more, which is off."""
    monkeypatch.setattr(engine, "_PRUNE_GONE_LIBRARIES", True)
