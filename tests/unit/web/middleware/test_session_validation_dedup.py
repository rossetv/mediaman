"""H6 regression: a cookie-bearing request must validate its session ONCE.

The ``ForcePasswordChangeMiddleware`` and the route dependency
(``get_current_admin`` / ``resolve_page_session``) both need the resolved
session. Without the request-scoped cache that was two full
``validate_session`` passes per authenticated request — two SELECTs, two
``last_used_at`` writes, two fingerprint computations — and, worse, the
two call sites fed DIFFERENT fingerprint inputs (``""`` vs ``None``),
opening a fingerprint-eviction race.

These tests pin: (1) the dependency reuses the middleware's cached result
so ``validate_session`` runs once, and (2) both passes feed identical
fingerprint inputs.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mediaman.db import init_db, set_connection
from mediaman.web.auth.middleware import get_current_admin, resolve_cached_session
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.force_password_change import ForcePasswordChangeMiddleware


@pytest.fixture
def conn(db_path):
    c = init_db(str(db_path))
    create_user(c, "alice", "correct-password-99", enforce_policy=False)
    set_connection(c)
    return c


def _app(conn) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ForcePasswordChangeMiddleware)

    @app.get("/api/whoami")
    def _whoami(user: str = Depends(get_current_admin)):
        return {"user": user}

    return app


class TestSingleValidationPerRequest:
    def test_authenticated_request_validates_session_once(self, conn, monkeypatch):
        """The middleware resolves the session and the dependency reuses
        the cached result — ``validate_session`` is invoked exactly once.
        """
        import mediaman.web.auth.middleware as mw

        # Fingerprint binding off — the TestClient's peer ("testclient")
        # is not a real IP, so a bound session would be evicted on a
        # fingerprint mismatch and confuse the call-count assertion. This
        # test is about dedup, not fingerprinting.
        monkeypatch.setenv("MEDIAMAN_FINGERPRINT_MODE", "off")
        token = create_session(conn, "alice")

        calls: list[tuple] = []
        real_validate = mw.validate_session

        def counting_validate(c, tok, **kwargs):
            calls.append((tok, kwargs))
            return real_validate(c, tok, **kwargs)

        monkeypatch.setattr(mw, "validate_session", counting_validate)

        client = TestClient(_app(conn))
        client.cookies.set("session_token", token)
        resp = client.get("/api/whoami", headers={"User-Agent": "ua"})

        assert resp.status_code == 200
        assert resp.json() == {"user": "alice"}
        # Exactly one validation for the whole request — middleware pass
        # cached, dependency reused.
        assert len(calls) == 1

    def test_both_passes_share_identical_fingerprint_inputs(self, conn, monkeypatch):
        """Unified UA-empty handling: when the request has NO User-Agent,
        both the middleware and the dependency must feed ``user_agent=None``
        (not ``""``) so the fingerprint check cannot fire in one pass but
        not the other.
        """
        import mediaman.web.auth.middleware as mw

        token = create_session(conn, "alice", user_agent="ua", client_ip="1.2.3.4")

        seen_kwargs: list[dict] = []
        real_validate = mw.validate_session

        def counting_validate(c, tok, **kwargs):
            seen_kwargs.append(kwargs)
            return real_validate(c, tok, **kwargs)

        monkeypatch.setattr(mw, "validate_session", counting_validate)

        client = TestClient(_app(conn))
        client.cookies.set("session_token", token)
        # No User-Agent header sent at all.
        resp = client.get("/api/whoami", headers={"User-Agent": ""})

        assert resp.status_code == 200
        # Only one validation, and its UA input is None (the unified
        # empty-handling), never the empty string the old middleware used.
        assert len(seen_kwargs) == 1
        assert seen_kwargs[0]["user_agent"] is None


class TestResolveCachedSession:
    def test_second_call_same_token_reuses_cache(self, conn, monkeypatch):
        """Two ``resolve_cached_session`` calls with the same token on the
        same request validate only once."""
        import mediaman.web.auth.middleware as mw

        token = create_session(conn, "alice")

        calls: list = []
        real_validate = mw.validate_session

        def counting_validate(c, tok, **kwargs):
            calls.append(tok)
            return real_validate(c, tok, **kwargs)

        monkeypatch.setattr(mw, "validate_session", counting_validate)

        class _State:
            pass

        class _Req:
            def __init__(self):
                self.state = _State()
                self.headers = {}
                self.cookies = {}

            @property
            def client(self):
                return None

        req = _Req()
        first = resolve_cached_session(req, conn, token)
        second = resolve_cached_session(req, conn, token)
        assert first == "alice"
        assert second == "alice"
        assert len(calls) == 1

    def test_different_token_bypasses_cache(self, conn, monkeypatch):
        """A second call with a DIFFERENT token must not reuse the cached
        username for the first token."""
        import mediaman.web.auth.middleware as mw

        token_a = create_session(conn, "alice")

        calls: list = []
        real_validate = mw.validate_session

        def counting_validate(c, tok, **kwargs):
            calls.append(tok)
            return real_validate(c, tok, **kwargs)

        monkeypatch.setattr(mw, "validate_session", counting_validate)

        class _State:
            pass

        class _Req:
            def __init__(self):
                self.state = _State()
                self.headers = {}
                self.cookies = {}

            @property
            def client(self):
                return None

        req = _Req()
        assert resolve_cached_session(req, conn, token_a) == "alice"
        # A bogus second token must trigger a fresh validation (and return
        # None), not echo the cached "alice".
        assert resolve_cached_session(req, conn, "f" * 64) is None
        assert len(calls) == 2
