"""Regression test: every static JS file must be valid, parseable JavaScript.

Nothing in the Python toolchain (ruff, mypy) ever looks inside
``static/js/`` — a stray token or a Unicode curly-quote used as a string
delimiter (indistinguishable from an ASCII quote at a glance) ships straight
to the browser and breaks every page that loads the file. ``node --check``
parses without executing, so this is a cheap, deterministic guard.

CI's ubuntu runners ship node on PATH, so the parse check asserts
unconditionally rather than skipping when the interpreter is missing — a
missing interpreter must never mask a broken file (CODE_GUIDELINES §11.15:
no conditional logic in tests). The interpreter's presence is its own test
so a missing `node` fails once, loudly, instead of masking every file
check as a subprocess-spawn error.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
JS_DIR = REPO / "src/mediaman/web/static/js"


def _js_files() -> list[Path]:
    return sorted(JS_DIR.rglob("*.js"))


def test_node_is_on_path() -> None:
    assert shutil.which("node") is not None, "no `node` binary found on PATH — install Node.js"


def test_static_js_files_found() -> None:
    assert _js_files() != [], f"expected at least one .js file under {JS_DIR}"


@pytest.mark.parametrize("js_file", _js_files(), ids=lambda p: str(p.relative_to(JS_DIR)))
def test_static_js_file_parses(js_file: Path) -> None:
    result = subprocess.run(
        ["node", "--check", str(js_file)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
