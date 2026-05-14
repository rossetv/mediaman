"""Parity tests for :func:`dispatch_loop` — the outbound retry core.

``dispatch_loop`` is exercised end-to-end through ``test_http_client.py``
(transport-error retry, 429/5xx retry, ``Retry-After`` honouring, exhaustion)
and through ``test_mailgun.py`` (the full-jitter / early-abort POST path).
These tests pin the pieces those suites do not reach directly: the
cross-iteration carry-over state (the ``consecutive_5xx`` counter and the
``last_status`` / ``last_snippet`` values) that the function decomposition
threads through a :class:`_LoopState`. If a future change mis-threads that
state, the retry / early-abort policy breaks silently — these tests fail loudly
instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mediaman.services.infra.http.retry import dispatch_loop


class _Err(Exception):
    """Minimal ``SafeHTTPError``-shaped exception for ``make_error``."""

    def __init__(self, *, status_code: int, body_snippet: str, url: str) -> None:
        self.status_code = status_code
        self.body_snippet = body_snippet
        self.url = url
        super().__init__(f"HTTP {status_code}: {body_snippet}")


def _response(*, status: int = 200, body: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.close = MagicMock()
    return resp


def _run(
    responses: list[MagicMock],
    *,
    attempts: int,
    abort_after_consecutive_5xx: int | None = None,
    retryable_statuses=None,
):
    """Drive ``dispatch_loop`` over a fixed list of responses, no real sleep."""
    bodies: dict[int, bytes] = {}
    seq = iter(responses)

    def dispatch_fn():
        return next(seq)

    def read_fn(resp):
        # The body is whatever the test stashed alongside the response.
        return bodies.get(id(resp), b"")

    for resp in responses:
        bodies[id(resp)] = getattr(resp, "_body", b"")

    return dispatch_loop(
        dispatch_fn=dispatch_fn,
        read_fn=read_fn,
        method="POST",
        url="http://upstream.example.test/send",
        attempts=attempts,
        make_error=_Err,
        abort_after_consecutive_5xx=abort_after_consecutive_5xx,
        retryable_statuses=retryable_statuses,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neuter the backoff sleep so the carry-over assertions run instantly."""
    monkeypatch.setattr("mediaman.services.infra.http.retry.time.sleep", lambda *_a: None)


class TestConsecutive5xxCarryOver:
    """The ``consecutive_5xx`` counter must survive across loop iterations."""

    def test_aborts_after_threshold_consecutive_5xx(self):
        """Two consecutive 5xx with ``abort_after_consecutive_5xx=2`` aborts
        on the second — proving the counter is incremented and read across
        iterations, not reset each pass."""
        r1 = _response(status=503, body=b"down-1")
        r1._body = b"down-1"
        r2 = _response(status=503, body=b"down-2")
        r2._body = b"down-2"
        # Budget of 5 — without the early abort this would run all 5.
        with pytest.raises(_Err) as excinfo:
            _run([r1, r2], attempts=5, abort_after_consecutive_5xx=2)
        assert excinfo.value.status_code == 503
        # The abort raised on the *second* 5xx, carrying that response's body.
        assert excinfo.value.body_snippet == "down-2"
        # Both responses were closed (first on retry, second on abort).
        assert r1.close.called
        assert r2.close.called

    def test_non_5xx_retryable_resets_the_streak(self):
        """A 429 between two 5xx must reset the consecutive-5xx counter, so
        a later lone 5xx does not trip a ``abort_after_consecutive_5xx=2``
        policy. Sequence: 503, 429, 503, 200 — the streak never reaches 2."""
        statuses = [503, 429, 503]
        responses = []
        for st in statuses:
            r = _response(status=st, body=f"s{st}".encode())
            r._body = f"s{st}".encode()
            responses.append(r)
        ok = _response(status=200, body=b"{}")
        ok._body = b"{}"
        responses.append(ok)
        # attempts=4 so all four are consumed; no abort should fire.
        result = _run(responses, attempts=4, abort_after_consecutive_5xx=2)
        assert result.status_code == 200

    def test_transport_error_resets_the_streak(self, monkeypatch):
        """A transport error mid-run breaks any consecutive-5xx streak: a
        503 then a connection error then a 503 must not abort at
        ``abort_after_consecutive_5xx=2``."""
        import requests

        r_503a = _response(status=503, body=b"a")
        r_503a._body = b"a"
        r_503b = _response(status=503, body=b"b")
        r_503b._body = b"b"
        ok = _response(status=200, body=b"{}")
        ok._body = b"{}"
        events = [r_503a, requests.ConnectionError("reset"), r_503b, ok]
        seq = iter(events)

        def dispatch_fn():
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        def read_fn(resp):
            return getattr(resp, "_body", b"")

        result = dispatch_loop(
            dispatch_fn=dispatch_fn,
            read_fn=read_fn,
            method="POST",
            url="http://upstream.example.test/send",
            attempts=4,
            make_error=_Err,
            abort_after_consecutive_5xx=2,
        )
        assert result.status_code == 200


class TestExhaustedRetries:
    """When every attempt returns a retryable status, the raised error must
    carry the *final* attempt's status and body snippet — the last response
    seen, not a stale earlier one."""

    def test_exhausted_retries_raise_with_final_status_and_snippet(self):
        """Each attempt returns a different retryable status; the error
        attributes the last one."""
        r1 = _response(status=502, body=b"gateway-1")
        r1._body = b"gateway-1"
        r2 = _response(status=503, body=b"gateway-2")
        r2._body = b"gateway-2"
        with pytest.raises(_Err) as excinfo:
            _run([r1, r2], attempts=2)
        # The final attempt's response is the one attributed in the error.
        assert excinfo.value.status_code == 503
        assert excinfo.value.body_snippet == "gateway-2"

    def test_transport_error_exhaustion_raises_status_zero(self, monkeypatch):
        """Every attempt fails at the transport layer — the final raise is a
        status-0 transport error, proving the carry-over does not mask it."""
        import requests

        seq = iter([requests.Timeout("slow"), requests.ConnectionError("reset")])

        def dispatch_fn():
            raise next(seq)

        with pytest.raises(_Err) as excinfo:
            dispatch_loop(
                dispatch_fn=dispatch_fn,
                read_fn=lambda resp: b"",
                method="GET",
                url="http://upstream.example.test/x",
                attempts=2,
                make_error=_Err,
            )
        assert excinfo.value.status_code == 0
        assert "transport error" in excinfo.value.body_snippet
