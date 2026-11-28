"""A field's or a switch's edge on glass: the solid page's grey edge is near the glass's own tone over
a white page, so every glass surface that can hold one lifts it, and the lifted edge keeps 3:1."""

from __future__ import annotations

import pytest

from tests.unit.web._css import contrast, style, token
from tests.unit.web.test_glass_css import BLACK, WHITE, _colour, _glass_over, _over, _root

# Every glass surface a field, a switch or a code box can sit on.
SURFACES = [
    (".ui-confirm-card", "_ui_feedback.css"),
    (".modal-sheet", "_modal.css"),
    (".keep-dialog", "_modal.css"),
    (".play-on", "_play_on.css"),
    (".you__panel", "_you.css"),
    (".space__panel", "_watch_space.css"),
]


@pytest.mark.parametrize(("surface", "sheet"), SURFACES)
def test_a_glass_surface_lifts_the_edge_of_what_it_holds(surface: str, sheet: str) -> None:
    assert style(surface, sheet).get("--field-edge") == "var(--g-field-edge)", surface


@pytest.mark.parametrize("page", [WHITE, BLACK])
def test_the_lifted_edge_holds_3_to_1_over_the_glass(page: tuple[float, ...]) -> None:
    # A menu by its button has no dim under it: the glass over the bare page is the worst case.
    glass = _glass_over(page, float(token("--g-a")), filtered=True)
    assert contrast(_over(_colour("var(--g-field-edge)", {}), glass), glass) >= 3


def test_the_edge_follows_the_glass_s_fallbacks() -> None:
    assert _root("forced-colors")["--g-field-edge"] == "CanvasText"
    plain = _colour(token("--g-field-edge"), {})
    more = _colour(_root("prefers-contrast")["--g-field-edge"], {})
    assert more[:3] == plain[:3] and more[3] > plain[3]
