"""Regression test: every static JS file must be valid, parseable JavaScript.

Nothing in the Python toolchain (ruff, mypy) ever looks inside
``static/js/`` — a stray token or a Unicode curly-quote used as a string
delimiter (indistinguishable from an ASCII quote at a glance) ships straight
to the browser and breaks every page that loads the file. ``node --check``
parses without executing, so this is a cheap, deterministic guard.

CI's ubuntu runners ship node. If this test can't find a ``node`` binary it
fails loudly rather than skipping — a missing interpreter must never mask a
broken file. Set ``MEDIAMAN_SKIP_NODE_CHECK=1`` to explicitly opt out (e.g.
a stripped-down sandbox with no node available).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
JS_DIR = REPO / "src/mediaman/web/static/js"

# Homebrew's default install location on macOS dev machines — `node` isn't
# always on a non-interactive shell's PATH even when it's installed.
_FALLBACK_NODE = Path("/opt/homebrew/bin/node")


def _node_binary() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    if _FALLBACK_NODE.exists():
        return str(_FALLBACK_NODE)
    return None


def _js_files() -> list[Path]:
    return sorted(JS_DIR.rglob("*.js"))


def test_static_js_files_parse() -> None:
    node = _node_binary()
    if node is None:
        if os.environ.get("MEDIAMAN_SKIP_NODE_CHECK") == "1":
            pytest.skip("MEDIAMAN_SKIP_NODE_CHECK=1 set and no node binary on PATH")
        pytest.fail(
            "no `node` binary found on PATH (checked "
            f"{_FALLBACK_NODE} too) — install Node.js, or set "
            "MEDIAMAN_SKIP_NODE_CHECK=1 to explicitly skip this check"
        )

    failures = []
    for js_file in _js_files():
        result = subprocess.run(
            [node, "--check", str(js_file)], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            failures.append(f"{js_file.relative_to(REPO)}:\n{result.stderr.strip()}")

    assert not failures, "node --check failed for:\n\n" + "\n\n".join(failures)
