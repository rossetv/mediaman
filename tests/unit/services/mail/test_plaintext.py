"""Tests for the plain-text rendering of HTML mail."""

from mediaman.services.mail.plaintext import html_to_text


def test_adjacent_table_cells_are_kept_apart():
    html = "<table><tr><td>IMDb 7.8</td><td>92%</td></tr></table>"

    assert html_to_text(html) == "IMDb 7.8 92%\n"


def test_what_is_hidden_from_readers_is_left_out_and_what_follows_it_is_not():
    html = (
        '<table><tr><td aria-hidden="true"><table><tr><td>Dune<br></td></tr></table>'
        '<a href="https://mm.example/x">link</a></td><td>Dune</td></tr></table>'
        '<img aria-hidden="true" src="p.jpg"><p>Keep</p>'
    )

    assert html_to_text(html) == "Dune\n\nKeep\n"
