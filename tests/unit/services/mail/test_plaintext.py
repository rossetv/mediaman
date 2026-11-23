"""Tests for the plain-text rendering of HTML mail."""

from mediaman.services.mail.plaintext import html_to_text


def test_adjacent_table_cells_are_kept_apart():
    html = "<table><tr><td>IMDb 7.8</td><td>92%</td></tr></table>"

    assert html_to_text(html) == "IMDb 7.8 92%\n"
