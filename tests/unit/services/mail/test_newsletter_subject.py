"""Newsletter subject line size formatting."""

from __future__ import annotations

from mediaman.services.mail.newsletter.render import _build_subject


def _items(*sizes: int) -> list:
    return [{"file_size_bytes": n} for n in sizes]


def test_zero_bytes_leaves_the_size_out():
    subject = _build_subject(_items(0), dry_run=False)
    assert "reclaim" not in subject
    assert " B" not in subject
    assert subject == "Mediaman Weekly Report — 1 item scheduled"


def test_below_one_mb_reads_in_kb():
    assert "500 KB to reclaim" in _build_subject(_items(512000), dry_run=False)


def test_tiny_size_is_at_least_one_kb():
    assert "1 KB to reclaim" in _build_subject(_items(10), dry_run=False)


def test_one_mb_and_above_keeps_mb_format():
    assert "1 MB to reclaim" in _build_subject(_items(1 << 20), dry_run=False)
