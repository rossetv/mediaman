"""Regression test: quick-keep controls must send canonical duration strings.

``POST /api/media/{id}/keep`` only accepts the keys of ``VALID_KEEP_DURATIONS``
("7 days", "30 days", "90 days", "forever") — see
``mediaman.web.models._common``. Any ``data-duration`` / ``data-keep-dur`` /
``name="duration" value="..."`` template literal, or a JS default fallback for
a duration value, that still uses the short form ("7d") sends a request the
route rejects with 400 ``invalid_duration`` — the "quick keep" buttons on the
Watch list and token-keep pages silently fail for every duration except Forever.
"""

from __future__ import annotations

import re
from pathlib import Path

from mediaman.web.models._common import VALID_KEEP_DURATIONS

REPO = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = REPO / "src/mediaman/web/templates"
JS_DIR = REPO / "src/mediaman/web/static/js"

# `data-duration="..."` / `data-keep-dur="..."` attribute literals in markup,
# plus the `"duration": "..."` entry passed to the `c.btn` macro's
# `data_attrs` dict, plus keep.html's plain-form `name="duration" value="..."`
# buttons.
_TEMPLATE_LITERAL_RE = re.compile(r'data-(?:duration|keep-dur)="([^"]+)"')
_MACRO_DURATION_RE = re.compile(r'data_attrs\s*=\s*\{[^}]*"duration":\s*"([^"]+)"')
_BUTTON_VALUE_RE = re.compile(r'name="duration"[^>]*\svalue="([^"]+)"')

# The JS default fallback for a duration value, e.g.
# `keepBtn.dataset.duration || '30d'` or `_keepDialogState.duration || "30d"`.
# Not anchored to `dataset.` — watch/admin.js's keep-dialog submit
# fall back on a bare `duration` variable and on `_keepDialogState.duration`,
# neither of which reads a `dataset.` property at the fallback expression.
_JS_FALLBACK_RE = re.compile(r"""(?:duration|keepDur)\s*\|\|\s*['"]([^'"]+)['"]""")


def _template_duration_literals() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        values = (
            _TEMPLATE_LITERAL_RE.findall(text)
            + _MACRO_DURATION_RE.findall(text)
            + _BUTTON_VALUE_RE.findall(text)
        )
        # The keep page's come from VALID_KEEP_DURATIONS itself.
        values = [v for v in values if not v.startswith("{{")]
        if values:
            found[str(path.relative_to(REPO))] = values
    return found


def _js_duration_fallbacks() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(JS_DIR.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        values = _JS_FALLBACK_RE.findall(text)
        if values:
            found[str(path.relative_to(REPO))] = values
    return found


def test_template_duration_literals_are_valid_keep_durations() -> None:
    literals = _template_duration_literals()
    assert literals != {}, "expected duration literals in the templates"
    assert "src/mediaman/web/templates/_watch_list.html" in literals
    for path, values in literals.items():
        for value in values:
            assert value in VALID_KEEP_DURATIONS, (
                f"{path}: {value!r} is not a key of VALID_KEEP_DURATIONS "
                f"({sorted(VALID_KEEP_DURATIONS)}) — POST /keep will 400 invalid_duration"
            )


def test_js_duration_fallbacks_are_valid_keep_durations() -> None:
    fallbacks = _js_duration_fallbacks()
    assert fallbacks != {}, (
        "expected a duration fallback (e.g. `duration || '30 days'`) in static/js"
    )
    assert "src/mediaman/web/static/js/watch/admin.js" in fallbacks
    for path, values in fallbacks.items():
        for value in values:
            assert value in VALID_KEEP_DURATIONS, (
                f"{path}: default fallback {value!r} is not a key of VALID_KEEP_DURATIONS "
                f"({sorted(VALID_KEEP_DURATIONS)}) — POST /keep will 400 invalid_duration"
            )


def test_the_shared_keep_menu_offers_only_valid_keep_durations() -> None:
    # watch/keep.js sends "<data-days> days" by the admins' route.
    card = (TEMPLATES_DIR / "_watch_card.html").read_text(encoding="utf-8")
    days = re.findall(r'data-days="(\d+)"', card)
    assert days != []
    assert {f"{d} days" for d in days} <= set(VALID_KEEP_DURATIONS)
