"""The lifespan stops the Watch stream reaper before it closes the database."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI

from mediaman import app_factory


def test_the_stream_reaper_is_stopped_before_the_database_is_closed(monkeypatch):
    order: list[str] = []
    config = SimpleNamespace(port=8282, secret_key="k")
    monkeypatch.setattr(app_factory, "load_config", lambda: config)
    monkeypatch.setattr(app_factory, "bootstrap_db", lambda app, cfg: None)
    monkeypatch.setattr(app_factory, "bootstrap_crypto", lambda app, cfg: None)
    monkeypatch.setattr(app_factory, "bootstrap_scheduling", lambda app, cfg: False)
    monkeypatch.setattr(app_factory, "_run_startup_reconciliation", lambda app: None)
    monkeypatch.setattr(
        app_factory, "_start_watch_streams", lambda key: lambda: order.append("reaper stopped")
    )
    monkeypatch.setattr(app_factory, "_shutdown", lambda app, started: order.append("db closed"))

    async def run() -> None:
        async with app_factory.lifespan(FastAPI()):
            order.append("serving")

    asyncio.run(run())

    assert order == ["serving", "reaper stopped", "db closed"]
