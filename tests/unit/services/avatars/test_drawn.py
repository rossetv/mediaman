"""The drawn avatars: which one an account has, and that they are only pictures."""

from __future__ import annotations

import re
from xml.etree import ElementTree

from mediaman.app_factory import _STATIC_DIR
from mediaman.services.avatars import drawn


def test_an_account_keeps_the_default_it_was_given():
    assert [drawn.default_choice(user_id) for user_id in range(1, 9)] == [8, 5, 2, 9, 6, 3, 10, 7]


def test_every_choice_is_someones_default_and_has_a_drawing():
    # ten accounts in a row have ten different ones
    choices = {drawn.default_choice(user_id) for user_id in range(1, drawn.COUNT + 1)}
    assert choices == set(range(1, drawn.COUNT + 1))
    for choice in choices:
        assert (_STATIC_DIR.parent / drawn.url(choice).lstrip("/")).is_file()


def test_the_drawings_carry_no_script_and_load_nothing():
    files = list((_STATIC_DIR / "avatars").iterdir())
    assert len(files) == drawn.COUNT
    tags = {"svg", "defs", "linearGradient", "stop", "clipPath", "g", "path", "rect", "circle"}
    for file in files:
        for element in ElementTree.fromstring(file.read_text()).iter():
            assert element.tag.partition("}")[2] in tags | {"ellipse"}
            for name, value in element.attrib.items():
                assert not name.lower().startswith("on") and "href" not in name
                assert not re.search(r"url\((?!#)|script", value)
