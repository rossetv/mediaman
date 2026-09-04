"""Regression test: no static JS file may call the undefined `mediamanToast`.

`window.mediamanToast` is never defined anywhere in the app — every call to
it silently failed to report an error to the user. `window.UIFeedback.error`
(see `ui-feedback.js`) is the app's real feedback API and is what every
other caller already uses.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
JS_DIR = REPO / "src/mediaman/web/static/js"


def test_no_static_js_file_references_mediaman_toast() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in sorted(JS_DIR.rglob("*.js"))
        if "mediamanToast" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"mediamanToast is undefined; use window.UIFeedback.error instead: {offenders}"
    )
