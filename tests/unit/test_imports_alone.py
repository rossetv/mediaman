"""Every module of mediaman imports on its own, as the first of the package a process loads.

An import cycle hides while some other module happens to be imported first; this test
imports each module with none of mediaman loaded before it, so a cycle shows at once.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import mediaman

_SRC = Path(mediaman.__file__).parent.parent

# Run in a child, as each module is imported with every mediaman module forgotten first.
_IMPORT_EACH = """
import importlib, sys, traceback
failed = []
for name in sys.argv[1:]:
    for loaded in [m for m in sys.modules if m == "mediaman" or m.startswith("mediaman.")]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except Exception:
        failed.append(name + ": " + traceback.format_exc().strip().splitlines()[-1])
print("\\n".join(failed))
"""


def _modules() -> list[str]:
    package = _SRC / "mediaman"
    return sorted(
        ".".join(path.relative_to(_SRC).with_suffix("").parts).removesuffix(".__init__")
        for path in package.rglob("*.py")
    )


def test_every_module_imports_with_nothing_of_mediaman_loaded_before_it() -> None:
    modules = _modules()
    assert "mediaman.services.arr.import_retry" in modules
    # Several children at once: one alone takes seconds over the whole package.
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _IMPORT_EACH, *modules[start::8]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=_SRC,
        )
        for start in range(8)
    ]
    failed = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        failed += out.strip().splitlines()
    assert failed == []
