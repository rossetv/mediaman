"""Taxonomy guard: ip_resolver must not import fastapi at runtime.

This test documents the §2.6 compliance fix: ``services/rate_limit/ip_resolver``
previously imported ``fastapi.Request`` unconditionally, coupling the services
layer to the web stack.  The module must remain importable in a Python process
where ``fastapi`` is absent (or blocked) — e.g. scheduled jobs, CLI scripts.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_ip_resolver_has_no_fastapi_import() -> None:
    """Verify ip_resolver has no runtime fastapi import via static AST analysis.

    Rather than mutating sys.modules (which pollutes subsequent tests), we use
    the ast module to inspect the source for any import of fastapi, fastapi.*,
    or from fastapi ... at module level (outside TYPE_CHECKING guards).
    This is deterministic and touches no global state.
    """
    resolver_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src/mediaman/services/rate_limit/ip_resolver.py"
    )
    source = resolver_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(resolver_path))

    for node in tree.body:
        # Skip type-checking blocks; imports inside TYPE_CHECKING are acceptable
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue

        # Check for "import fastapi" or "import fastapi.*"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("fastapi"), (
                    f"Found forbidden runtime import: import {alias.name}"
                )

        # Check for "from fastapi ... import ..." or "from fastapi.* ... import ..."
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("fastapi"):
            raise AssertionError(
                f"Found forbidden runtime import: from {node.module} import {', '.join(alias.name for alias in node.names)}"
            )
