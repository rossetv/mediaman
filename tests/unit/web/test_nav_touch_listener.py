"""iOS Safari applies :active only when a touchstart listener exists on the document."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

NAV_JS = Path(__file__).resolve().parents[3] / "src/mediaman/web/static/js/nav.js"

_HARNESS = """
const calls = [];
global.document = {
  addEventListener: (type, fn, opts) => calls.push({ type, opts }),
  getElementById: () => null,
  querySelector: () => null,
};
global.window = {};
require(process.argv[1]);
console.log(JSON.stringify(calls));
"""


def test_nav_registers_passive_document_touchstart() -> None:
    out = subprocess.run(
        ["node", "-e", _HARNESS, str(NAV_JS)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    calls = json.loads(out)
    assert {"type": "touchstart", "opts": {"passive": True}} in calls
