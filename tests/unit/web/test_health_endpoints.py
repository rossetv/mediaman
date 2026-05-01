"""Tests for /healthz (liveness) and /readyz (readiness).

Finding 10: a green ``/healthz`` should not hide a dead scheduler. The
two probes are now decoupled — ``/healthz`` reports liveness only,
``/readyz`` returns 503 until the scheduler and crypto canary have
both come up.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_test_app(scheduler_healthy: bool, canary_ok: bool) -> FastAPI:
    """Build a minimal app that exposes only the health endpoints.

    Reaches into :func:`mediaman.main.create_app` would also load every
    router, so we replicate the two endpoints here. This keeps the test
    fast and isolates the readiness logic from the rest of the app.
    """
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        sched = bool(getattr(app.state, "scheduler_healthy", False))
        canary = bool(getattr(app.state, "canary_ok", True))
        ready = sched and canary
        body = {
            "status": "ready" if ready else "not_ready",
            "scheduler": "ok" if sched else "down",
            "crypto": "ok" if canary else "down",
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    app.state.scheduler_healthy = scheduler_healthy
    app.state.canary_ok = canary_ok
    return app


class TestHealthzAlwaysOk:
    def test_healthz_returns_200_when_scheduler_is_down(self):
        """Liveness must stay green even when the scheduler is dead.

        Otherwise the orchestrator would tear the container down on a
        failure that is recoverable from inside the running process.
        """
        app = _make_test_app(scheduler_healthy=False, canary_ok=True)
        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestReadyzReflectsBackgroundServices:
    def test_returns_200_when_everything_is_up(self):
        app = _make_test_app(scheduler_healthy=True, canary_ok=True)
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "ready", "scheduler": "ok", "crypto": "ok"}

    def test_returns_503_when_scheduler_failed(self):
        """Finding 10: a dead scheduler must surface as a 503 readiness probe."""
        app = _make_test_app(scheduler_healthy=False, canary_ok=True)
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert body["scheduler"] == "down"
        assert body["crypto"] == "ok"

    def test_returns_503_when_canary_failed(self):
        app = _make_test_app(scheduler_healthy=True, canary_ok=False)
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert body["crypto"] == "down"


class TestReadyzInRealApp:
    """Proves the route is wired into create_app, not just the test stub."""

    def test_create_app_exposes_readyz(self, monkeypatch):
        # Stub the scheduler/canary so we don't actually start anything.
        from mediaman.main import create_app

        app = create_app()
        # Without any bootstrap, scheduler_healthy is unset → 503.
        client = TestClient(app)
        resp = client.get("/readyz")
        assert resp.status_code == 503

    def test_create_app_exposes_healthz(self):
        from mediaman.main import create_app

        app = create_app()
        client = TestClient(app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
