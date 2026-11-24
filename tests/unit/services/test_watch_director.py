"""The director of a title's TMDB fields, and the page they can be linked to."""

from __future__ import annotations

import pytest

from mediaman.services.watch_catalog import tmdb_fields

WRITER = {"id": 3, "name": "Ann", "job": "Writer"}
DIRECTOR = {"id": 4, "name": "Bo", "job": "Director"}


@pytest.mark.parametrize(
    ("kind", "details", "expected"),
    [
        ("movie", {"credits": {"crew": [WRITER, DIRECTOR]}}, ("Bo", 4)),
        ("movie", {"credits": {"crew": [{**DIRECTOR, "adult": True}]}}, ("Bo", None)),
        ("movie", {"credits": {"crew": [{**DIRECTOR, "id": "4"}]}}, ("Bo", None)),
        ("movie", {"credits": {"crew": [{**DIRECTOR, "name": None}]}}, (None, None)),
        ("movie", {}, (None, None)),
        ("tv", {"created_by": [{"id": 9, "name": "Cy"}]}, ("Cy", 9)),
    ],
)
def test_the_director_comes_with_a_person_id_where_there_is_a_page_for_them(
    kind, details, expected
):
    fields = tmdb_fields(details, kind)

    assert (fields["director"], fields["director_id"]) == expected
