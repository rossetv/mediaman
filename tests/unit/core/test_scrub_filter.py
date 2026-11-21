"""Tests for the generic ScrubFilter logging filter."""

from __future__ import annotations

import io
import logging
import logging.config
import threading
import time

import pytest

from mediaman.core.scrub_filter import AccessLogFilter, ScrubFilter, install_root_filter


def _make_record(msg: str, args: tuple | None = None) -> logging.LogRecord:
    """Create a minimal LogRecord suitable for passing to ScrubFilter.filter."""
    record = logging.LogRecord(
        name="test",
        level=logging.DEBUG,
        pathname="",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )
    return record


class TestScrubFilterRedaction:
    def test_redacts_secret_in_message(self) -> None:
        f = ScrubFilter(secrets=["my-secret-token"])
        record = _make_record("GET http://example.com?token=my-secret-token")
        f.filter(record)
        assert "my-secret-token" not in record.msg
        assert "***REDACTED***" in record.msg

    def test_redacts_secret_in_args(self) -> None:
        f = ScrubFilter(secrets=["s3kr3t"])
        record = _make_record("Request URL: %s", args=("https://api.example.com?key=s3kr3t",))
        f.filter(record)
        assert isinstance(record.args, tuple)
        assert "s3kr3t" not in record.args[0]
        assert "***REDACTED***" in record.args[0]

    def test_redacts_multiple_secrets(self) -> None:
        f = ScrubFilter(secrets=["alpha", "beta"])
        record = _make_record("alpha and beta both present")
        f.filter(record)
        assert "alpha" not in record.msg
        assert "beta" not in record.msg
        assert record.msg.count("***REDACTED***") == 2

    def test_custom_replacement(self) -> None:
        f = ScrubFilter(secrets=["hunter2"], replacement="<hidden>")
        record = _make_record("password=hunter2")
        f.filter(record)
        assert "hunter2" not in record.msg
        assert "<hidden>" in record.msg

    def test_non_string_args_untouched(self) -> None:
        f = ScrubFilter(secrets=["secret"])
        record = _make_record("count=%d", args=(42,))
        f.filter(record)
        assert record.args == (42,)

    def test_returns_true_always(self) -> None:
        f = ScrubFilter(secrets=["x"])
        record = _make_record("no secrets here")
        assert f.filter(record) is True

    def test_returns_true_even_when_message_is_not_str(self) -> None:
        f = ScrubFilter(secrets=["x"])
        record = _make_record("placeholder")
        record.msg = 12345  # type: ignore[assignment]  # simulate non-str msg
        assert f.filter(record) is True

    def test_empty_secret_ignored(self) -> None:
        """An empty string secret must not be applied (would redact everything)."""
        f = ScrubFilter(secrets=["", "real-secret"])
        assert "" not in f._secrets
        record = _make_record("hello real-secret world")
        f.filter(record)
        assert "real-secret" not in record.msg

    def test_dict_args_scrubbed(self) -> None:
        """Dict-style args (used by some formatters) are also scrubbed."""
        f = ScrubFilter(secrets=["topsecret"])
        # LogRecord.__init__ chokes on a dict passed as args via the constructor
        # on Python 3.13+, so build the record first and patch args afterwards.
        record = _make_record("%(url)s")
        record.args = {"url": "http://x.com?k=topsecret"}  # type: ignore[assignment]
        f.filter(record)
        assert isinstance(record.args, dict)
        assert "topsecret" not in record.args["url"]


class TestScrubFilterAttach:
    @pytest.fixture(autouse=True)
    def _clear_logger_filters(self):
        """Ensure the test logger starts with no filters."""
        logger = logging.getLogger("test.scrub_attach")
        logger.filters.clear()

    def test_attach_adds_filter(self) -> None:
        ScrubFilter.attach("test.scrub_attach", secrets=["abc"])
        logger = logging.getLogger("test.scrub_attach")
        assert any(isinstance(f, ScrubFilter) for f in logger.filters)

    def test_attach_is_idempotent(self) -> None:
        """Calling attach twice with the same secrets must not double-stack."""
        ScrubFilter.attach("test.scrub_attach", secrets=["abc"])
        ScrubFilter.attach("test.scrub_attach", secrets=["abc"])
        logger = logging.getLogger("test.scrub_attach")
        scrub_filters = [f for f in logger.filters if isinstance(f, ScrubFilter)]
        assert len(scrub_filters) == 1

    def test_attach_different_secrets_adds_new_filter(self) -> None:
        """Two attach calls with different secrets each add their own filter."""
        ScrubFilter.attach("test.scrub_attach", secrets=["abc"])
        ScrubFilter.attach("test.scrub_attach", secrets=["xyz"])
        logger = logging.getLogger("test.scrub_attach")
        scrub_filters = [f for f in logger.filters if isinstance(f, ScrubFilter)]
        assert len(scrub_filters) == 2

    def test_attach_returns_scrub_filter_instance(self) -> None:
        f = ScrubFilter.attach("test.scrub_attach", secrets=["abc"])
        assert isinstance(f, ScrubFilter)

    def test_attach_second_call_returns_existing_instance(self) -> None:
        first = ScrubFilter.attach("test.scrub_attach", secrets=["abc"])
        second = ScrubFilter.attach("test.scrub_attach", secrets=["abc"])
        assert first is second

    def test_attach_empty_secret_guard(self) -> None:
        """Empty strings passed as secrets must be silently dropped."""
        f = ScrubFilter.attach("test.scrub_attach", secrets=["", "real"])
        assert "" not in f._secrets
        assert "real" in f._secrets

    @pytest.mark.parametrize("logger_name", ["mediaman", "urllib3.connectionpool"])
    def test_attach_target_loggers(self, logger_name: str) -> None:
        """attach works on the same loggers used in production."""
        target = logging.getLogger(logger_name)
        before = len(target.filters)
        ScrubFilter.attach(logger_name, secrets=["dummy-secret-xyz"])
        after = len(target.filters)
        assert after >= before + 1 or any(
            isinstance(f, ScrubFilter) and "dummy-secret-xyz" in f._secrets for f in target.filters
        )
        # Cleanup — remove any filter we added so other tests aren't affected.
        target.filters = [
            f
            for f in target.filters
            if not (isinstance(f, ScrubFilter) and "dummy-secret-xyz" in f._secrets)
        ]


class TestScrubFilterExceptionSwallow:
    def test_filter_returns_true_on_scrub_error(self) -> None:
        """filter() must return True even when _scrub raises internally."""
        f = ScrubFilter(secrets=["secret"])
        record = _make_record("placeholder")
        # Patch _scrub to raise to simulate an unexpected scrub failure.
        original_scrub = f._scrub

        def _boom(text: str) -> str:
            raise RuntimeError("simulated scrub failure")

        f._scrub = _boom  # type: ignore[method-assign]
        result = f.filter(record)
        assert result is True
        f._scrub = original_scrub  # type: ignore[method-assign]

    def test_unexpected_exception_propagates_outside_filter(self) -> None:
        """Exceptions raised OUTSIDE filter() (e.g. in register_secret) propagate normally."""
        f = ScrubFilter(secrets=["x"])
        # register_secret with a valid string must not raise.
        f.register_secret("new-secret")
        assert "new-secret" in f._secrets


class TestScrubFilterThreadSafety:
    def test_concurrent_register_secret_no_duplicates(self) -> None:
        """Concurrent register_secret calls must not produce duplicate entries."""
        f = ScrubFilter(secrets=[])
        errors: list[Exception] = []
        barrier = threading.Barrier(20)

        def _register() -> None:
            try:
                barrier.wait()
                f.register_secret("shared-secret")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_register) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert f._secrets.count("shared-secret") == 1

    def test_concurrent_filter_and_register_no_crash(self) -> None:
        """Concurrent filter() and register_secret() must not raise or corrupt state."""
        f = ScrubFilter(secrets=["initial"])
        errors: list[Exception] = []
        stop = threading.Event()

        def _filter_loop() -> None:
            try:
                while not stop.is_set():
                    record = _make_record("message with initial secret")
                    f.filter(record)
            except Exception as exc:
                errors.append(exc)

        def _register_loop() -> None:
            try:
                for i in range(50):
                    f.register_secret(f"secret-{i}")
            except Exception as exc:
                errors.append(exc)

        reader = threading.Thread(target=_filter_loop)
        writer = threading.Thread(target=_register_loop)
        reader.start()
        writer.start()
        writer.join()
        stop.set()
        reader.join()

        assert not errors


class TestInstalledRootFilter:
    """Records logged through the ``mediaman`` logger as the app configures it."""

    SECRET = "tok-installed-filter-9f3a"

    def test_secret_in_message_is_scrubbed_on_stderr(self, capsys) -> None:
        install_root_filter(secrets=[self.SECRET])
        logging.getLogger("mediaman.scrubtest").info(f"token is {self.SECRET}")
        err = capsys.readouterr().err
        assert "token is ***REDACTED***" in err
        assert self.SECRET not in err

    def test_secret_in_exception_arg_is_scrubbed_on_stderr(self, capsys) -> None:
        install_root_filter(secrets=[self.SECRET])
        exc = RuntimeError(f"401 for key={self.SECRET}")
        logging.getLogger("mediaman.scrubtest").warning("call failed: %s", exc)
        err = capsys.readouterr().err
        assert "call failed: 401 for key=***REDACTED***" in err
        assert self.SECRET not in err

    def test_secret_in_traceback_is_scrubbed_on_stderr(self, capsys) -> None:
        install_root_filter(secrets=[self.SECRET])
        try:
            raise ValueError(f"bad token {self.SECRET}")
        except ValueError:
            logging.getLogger("mediaman.scrubtest").exception("lookup crashed")
        err = capsys.readouterr().err
        assert "lookup crashed" in err
        assert "ValueError: bad token ***REDACTED***" in err
        assert self.SECRET not in err

    def test_info_line_is_emitted_exactly_once_after_installing_twice(self, capsys) -> None:
        install_root_filter()
        install_root_filter()
        logging.getLogger("mediaman.scrubtest").info("started marker 51c2")
        assert capsys.readouterr().err.count("started marker 51c2") == 1


class TestPercentRArgs:
    def test_raising_repr_does_not_stop_other_args_being_scrubbed(self) -> None:
        class Broken:
            def __str__(self) -> str:
                return "broken token=repr-secret-77"

            def __repr__(self) -> str:
                raise RuntimeError("no repr")

        f = ScrubFilter(secrets=["repr-secret-77"])
        record = _make_record("%s then %s", args=(Broken(), "repr-secret-77"))
        f.filter(record)
        assert record.getMessage() == "broken token=***REDACTED*** then ***REDACTED***"

    def test_raising_str_does_not_stop_other_args_being_scrubbed(self) -> None:
        class Broken:
            def __str__(self) -> str:
                raise RuntimeError("no str")

        f = ScrubFilter(secrets=["str-secret-91"])
        record = _make_record("%s then %s", args=(Broken(), "str-secret-91"))
        f.filter(record)
        message = record.getMessage()
        assert "str-secret-91" not in message
        assert message.endswith("then ***REDACTED***")

    def test_secret_only_in_repr_is_scrubbed_for_percent_r(self) -> None:
        class Client:
            def __str__(self) -> str:
                return "client"

            def __repr__(self) -> str:
                return "Client(token='repr-secret-77')"

        f = ScrubFilter(secrets=["repr-secret-77"])
        record = _make_record("using %r", args=(Client(),))
        f.filter(record)
        assert record.getMessage() == "using Client(token='***REDACTED***')"


@pytest.fixture
def server_logs():
    """Apply uvicorn's own log config and yield its (stdout, stderr) buffers.

    The loggers it touched are restored afterwards.
    """
    from uvicorn.config import LOGGING_CONFIG

    names = ["uvicorn", "uvicorn.error", "uvicorn.access", "apscheduler"]
    saved = {
        n: (
            list(logging.getLogger(n).handlers),
            list(logging.getLogger(n).filters),
            logging.getLogger(n).level,
            logging.getLogger(n).propagate,
        )
        for n in names
    }
    install_root_filter(secrets=[TestServerLogs.SECRET])
    # uvicorn.run applies this after the app (and so the filters) exists.
    logging.config.dictConfig(LOGGING_CONFIG)
    out, err = io.StringIO(), io.StringIO()
    logging.getLogger("uvicorn.access").handlers[0].setStream(out)
    logging.getLogger("uvicorn").handlers[0].setStream(err)
    yield out, err
    for n, (handlers, filters, level, propagate) in saved.items():
        logger = logging.getLogger(n)
        logger.handlers[:] = handlers
        logger.filters[:] = filters
        logger.setLevel(level)
        logger.propagate = propagate


class TestServerLogs:
    """uvicorn and APScheduler output once uvicorn has applied its log config."""

    SECRET = "tok-server-logs-4c1d"

    @pytest.mark.parametrize(
        ("method", "target", "shape"),
        [
            ("GET", "/keep/abc.DEF-123", "GET /keep/*** HTTP/1.1"),
            ("POST", "/keep/abc.DEF-123", "POST /keep/*** HTTP/1.1"),
            ("POST", "/api/keep/abc.DEF-123/forever", "POST /api/keep/***/forever HTTP/1.1"),
            ("GET", "/download/abc.DEF-123", "GET /download/*** HTTP/1.1"),
            ("GET", "/unsubscribe?token=abc.DEF-123", "GET /unsubscribe?token=*** HTTP/1.1"),
            ("GET", "/api/poster/42?sig=abc.DEF-123", "GET /api/poster/42?sig=*** HTTP/1.1"),
            ("GET", "/x?a=1&apikey=abc.DEF-123&b=2", "GET /x?a=1&apikey=***&b=2 HTTP/1.1"),
            ("GET", "//keep/abc.DEF-123", "GET //keep/*** HTTP/1.1"),
            ("GET", "/keep//abc.DEF-123", "GET /keep//*** HTTP/1.1"),
            ("GET", "/KEEP/abc.DEF-123", "GET /KEEP/*** HTTP/1.1"),
            ("POST", "/api//keep/abc.DEF-123/forever", "POST /api//keep/***/forever HTTP/1.1"),
            ("GET", "//Download//abc.DEF-123", "GET //Download//*** HTTP/1.1"),
            ("GET", "/unsubscribe?%74oken=abc.DEF-123", "GET /unsubscribe?%74oken=*** HTTP/1.1"),
            ("GET", "/api/poster/42?s%69g=abc.DEF-123", "GET /api/poster/42?s%69g=*** HTTP/1.1"),
            ("GET", "/x?a=1&api%5Fkey=abc.DEF-123&b=2", "GET /x?a=1&api%5Fkey=***&b=2 HTTP/1.1"),
        ],
    )
    def test_access_log_masks_emailed_token(self, server_logs, method, target, shape) -> None:
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", method, target, "1.1", 200
        )
        out = server_logs[0].getvalue()
        assert "abc.DEF-123" not in out
        assert f'{shape}" 200' in out

    def test_access_log_keeps_tokenless_target(self, server_logs) -> None:
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", "GET", "/downloads?page=2", "1.1", 200
        )
        assert 'GET /downloads?page=2 HTTP/1.1" 200' in server_logs[0].getvalue()

    @pytest.mark.parametrize(
        ("target", "logged"),
        [
            ("/x?token=s1&token=s2", "/x?token=***&token=***"),
            ("/x?a=1&&sig=s1;b=2", "/x?a=1&&sig=***"),
            ("/x?%zz=1&apikey=s1#frag", "/x?%zz=1&apikey=***#frag"),
            ("/x?token&page=2", "/x?token&page=2"),
        ],
    )
    def test_access_log_query_is_read_like_the_app_reads_it(self, target, logged) -> None:
        record = _make_record("%s", args=(target,))
        AccessLogFilter().filter(record)
        assert record.args == (logged,)

    @pytest.mark.parametrize(
        "target",
        ["?" * 100_000, "/keep/abc?" + "?a" * 50_000, "/x?" + "&" * 100_000],
    )
    def test_access_log_redaction_is_fast_on_a_huge_target(self, target) -> None:
        record = _make_record("%s", args=(target,))
        start = time.perf_counter()
        AccessLogFilter().filter(record)
        assert time.perf_counter() - start < 0.5

    def test_uvicorn_error_traceback_is_scrubbed(self, server_logs) -> None:
        try:
            raise ValueError(f"bad token {self.SECRET}")
        except ValueError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
        err = server_logs[1].getvalue()
        assert "Exception in ASGI application" in err
        assert "ValueError: bad token ***REDACTED***" in err
        assert self.SECRET not in err

    def test_apscheduler_job_traceback_is_scrubbed_once(self, server_logs, capsys) -> None:
        try:
            raise ValueError(f"bad token {self.SECRET}")
        except ValueError:
            logging.getLogger("apscheduler.executors.default").exception("Job raised")
        err = capsys.readouterr().err
        assert err.count("Job raised") == 1
        assert "ValueError: bad token ***REDACTED***" in err
        assert self.SECRET not in err

    def test_apscheduler_info_stays_silent(self, server_logs, capsys) -> None:
        logging.getLogger("apscheduler.scheduler").info("Added job marker 9e1")
        assert "marker 9e1" not in capsys.readouterr().err
