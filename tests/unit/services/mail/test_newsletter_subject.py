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


def test_1023_bytes_reads_as_one_kb():
    assert "1 KB to reclaim" in _build_subject(_items(1023), dry_run=False)


def test_size_that_rounds_to_1024_kb_reads_as_one_mb():
    subject = _build_subject(_items((1 << 20) - 1), dry_run=False)
    assert "1 MB to reclaim" in subject
    assert "1024" not in subject


def test_size_that_rounds_to_1024_mb_reads_as_one_gb():
    subject = _build_subject(_items((1 << 30) - 1), dry_run=False)
    assert "1.0 GB to reclaim" in subject
    assert "1024" not in subject


def test_gb_value_keeps_one_decimal():
    assert "2.5 GB to reclaim" in _build_subject(_items(int(2.5 * (1 << 30))), dry_run=False)


def test_size_that_rounds_to_1024_gb_reads_as_one_tb():
    subject = _build_subject(_items((1 << 40) - 1), dry_run=False)
    assert "1.0 TB to reclaim" in subject
    assert "1024" not in subject
