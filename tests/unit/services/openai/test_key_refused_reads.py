"""Asking whether the key was refused reads the stored key only when a refusal is held."""

from __future__ import annotations

from mediaman.services.openai import client as client_mod
from mediaman.services.openai.client import key_refused


def _reads(monkeypatch) -> list[object]:
    reads: list[object] = []

    def read(conn, secret_key=None):
        reads.append(conn)
        return "sk-test"

    monkeypatch.setattr(client_mod, "get_openai_key", read)
    return reads


def test_with_no_refusal_held_the_key_is_not_read(monkeypatch) -> None:
    reads = _reads(monkeypatch)
    monkeypatch.setattr(client_mod, "_refused_key", None)
    assert key_refused(None) is False
    assert reads == []


def test_with_a_refusal_past_its_hour_the_key_is_not_read(monkeypatch) -> None:
    reads = _reads(monkeypatch)
    monkeypatch.setattr(client_mod, "_refused_key", client_mod._digest("sk-test"))
    monkeypatch.setattr(client_mod, "_refused_at", client_mod.monotonic() - 3601)
    assert key_refused(None) is False
    assert reads == []
