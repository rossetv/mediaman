"""Parity tests for ``dispatch_loop`` — the outbound retry core.

``dispatch_loop`` is exercised end-to-end through ``test_http_client.py``
(transport-error retry, 429/5xx retry, ``Retry-After`` honouring, exhaustion)
and through ``test_mailgun.py`` (the full-jitter, connect-only POST path).
These tests pin the pieces those suites do not reach directly: the
cross-iteration carry-over state (the ``last_status`` / ``last_snippet``
values) that the retry loop threads through a ``_LoopState``, and
the per-call retry options. If a future change mis-threads that state, the
retry policy breaks silently — these tests fail loudly instead.
"""

from __future__ import annotations

import contextlib
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
        retryable_statuses=retryable_statuses,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Neuter the backoff sleep so the carry-over assertions run instantly."""
    monkeypatch.setattr("mediaman.services.infra.http.retry.time.sleep", lambda *_a: None)


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


class TestDegenerateAttemptBudget:
    """A zero/negative ``attempts`` must fail closed, not raise ``AssertionError``."""

    def test_zero_attempts_raises_make_error_not_assertion(self):
        """``attempts=0`` skips the loop entirely. The old code asserted
        ``state.last_exc is not None`` and would raise a bare
        ``AssertionError`` (stripped under ``python -O``). It must now raise
        the typed ``make_error`` with status 0."""

        def dispatch_fn():  # pragma: no cover - never called with attempts=0
            raise AssertionError("dispatch_fn must not run with attempts=0")

        with pytest.raises(_Err) as excinfo:
            dispatch_loop(
                dispatch_fn=dispatch_fn,
                read_fn=lambda resp: b"",
                method="GET",
                url="http://upstream.example.test/x",
                attempts=0,
                make_error=_Err,
            )
        assert excinfo.value.status_code == 0
        assert "attempt budget" in excinfo.value.body_snippet


class TestRetryTransportErrorOption:
    """A caller can narrow which transport errors are worth another attempt."""

    @staticmethod
    def _run_raising(errors, *, retry_transport_error, attempts=3):
        calls = []
        seq = iter(errors)

        def dispatch_fn():
            calls.append(1)
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        result = dispatch_loop(
            dispatch_fn=dispatch_fn,
            read_fn=lambda resp: b"",
            method="POST",
            url="http://upstream.example.test/send",
            attempts=attempts,
            make_error=_Err,
            retry_transport_error=retry_transport_error,
        )
        return result, len(calls)

    def test_an_error_the_caller_rejects_is_raised_without_another_attempt(self):
        import requests

        with pytest.raises(_Err) as excinfo:
            self._run_raising(
                [requests.Timeout("slow"), _response()],
                retry_transport_error=lambda exc: False,
            )
        assert excinfo.value.status_code == 0
        assert "transport error" in excinfo.value.body_snippet

    def test_an_error_the_caller_accepts_is_retried(self):
        import requests

        ok = _response()
        result, calls = self._run_raising(
            [requests.Timeout("slow"), ok], retry_transport_error=lambda exc: True
        )
        assert result is ok
        assert calls == 2

    def test_the_predicate_sees_the_exception_raised(self):
        import requests

        seen = []

        def accept_connect_timeout_only(exc):
            seen.append(type(exc))
            return isinstance(exc, requests.ConnectTimeout)

        ok = _response()
        result, calls = self._run_raising(
            [requests.ConnectTimeout("a"), requests.ConnectTimeout("b"), ok],
            retry_transport_error=accept_connect_timeout_only,
        )
        assert result is ok
        assert calls == 3
        assert seen == [requests.ConnectTimeout, requests.ConnectTimeout]


class TestRetryAfterNotANumber:
    """A ``Retry-After`` of ``nan`` must fall back to the normal backoff and
    end in the module's error, not a bare ``ValueError`` from ``time.sleep``."""

    def test_nan_retry_after_ends_in_make_error_not_value_error(self, monkeypatch):
        slept: list[float] = []

        def strict_sleep(seconds):
            if seconds != seconds:
                raise ValueError("Invalid value NaN (not a number)")
            slept.append(seconds)

        monkeypatch.setattr("mediaman.services.infra.http.retry.time.sleep", strict_sleep)
        r1 = _response(status=429)
        r1.headers = {"Retry-After": "nan"}
        r2 = _response(status=429)
        with pytest.raises(_Err) as excinfo:
            _run([r1, r2], attempts=2)
        assert excinfo.value.status_code == 429
        assert len(slept) == 1


class TestTheLogSaysHowARetryEnded:
    """A retried request leaves a line that says whether it got through in the end."""

    @staticmethod
    def _drive(outcomes, caplog):
        import requests

        seq = iter(outcomes)

        def dispatch_fn():
            outcome = next(seq)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            caplog.at_level("INFO", logger="mediaman.services.infra.http.retry"),
            contextlib.suppress(_Err, requests.RequestException),
        ):
            dispatch_loop(
                dispatch_fn=dispatch_fn,
                read_fn=lambda resp: b"",
                method="GET",
                url="https://image.example.test/p/w342/a.jpg",
                attempts=3,
                make_error=_Err,
            )
        return [r.getMessage() for r in caplog.records]

    def test_a_request_that_got_through_on_a_retry_says_so(self, caplog):
        import requests

        lines = self._drive([requests.ReadTimeout(), _response(status=200)], caplog)
        assert any("succeeded" in line and "attempt 2" in line for line in lines)

    def test_a_request_that_failed_every_attempt_says_it_gave_up(self, caplog):
        import requests

        lines = self._drive([requests.ReadTimeout()] * 3, caplog)
        assert any("giving up" in line for line in lines)

    def test_a_request_still_answered_with_an_outage_at_the_end_says_it_gave_up(self, caplog):
        lines = self._drive([_response(status=503)] * 3, caplog)
        assert any("giving up" in line and "503" in line for line in lines)

    def test_a_request_that_got_through_first_time_logs_nothing(self, caplog):
        assert self._drive([_response(status=200)], caplog) == []
