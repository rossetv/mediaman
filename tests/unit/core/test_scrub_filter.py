"""Tests for the generic ScrubFilter logging filter."""

from __future__ import annotations

import logging

import pytest

from mediaman.core.scrub_filter import ScrubFilter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ScrubFilter.filter — redaction behaviour
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ScrubFilter.attach — idempotent attachment
# ---------------------------------------------------------------------------


class TestScrubFilterAttach:
    def setup_method(self) -> None:
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
