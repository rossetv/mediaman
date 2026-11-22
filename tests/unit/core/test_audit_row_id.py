"""log_audit must fail loudly when SQLite reports no row id."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mediaman.core.audit import log_audit


def test_missing_row_id_raises_a_clear_error():
    conn = SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(lastrowid=None))
    with pytest.raises(RuntimeError, match="audit_log"):
        log_audit(conn, "item", "deleted", "detail")  # type: ignore[arg-type]
