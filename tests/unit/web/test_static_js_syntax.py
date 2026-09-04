"""Regression test: every static JS file must be valid, parseable JavaScript.

Nothing in the Python toolchain (ruff, mypy) ever looks inside
``static/js/`` — a stray token or a Unicode curly-quote used as a string
delimiter (indistinguishable from an ASCII quote at a glance) ships straight
to the browser and breaks every page that loads the file. ``node --check``
parses without executing, so this is a cheap, deterministic guard.

CI's ubuntu runners ship node on PATH, so this asserts unconditionally
rather than skipping when the interpreter is missing — a missing
interpreter must never mask a broken file (CODE_GUIDELINES §11.15: no
conditional logic in tests).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
JS_DIR = REPO / "src/mediaman/web/static/js"


def _js_files() -> list[Path]:
    return sorted(JS_DIR.rglob("*.js"))


def test_static_js_files_parse() -> None:
    node = shutil.which("node")
    assert node is not None, "no `node` binary found on PATH — install Node.js"

    js_files = _js_files()
    assert js_files, f"expected at least one .js file under {JS_DIR}"

    failures = []
    for js_file in js_files:
        result = subprocess.run(
            [node, "--check", str(js_file)], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            failures.append(f"{js_file.relative_to(REPO)}:\n{result.stderr.strip()}")

    assert not failures, "node --check failed for:\n\n" + "\n\n".join(failures)
