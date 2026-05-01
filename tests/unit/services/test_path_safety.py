"""Tests for path_safety helpers (finding 31 — unified delete-roots parser)."""

from __future__ import annotations

from mediaman.services.infra.path_safety import parse_delete_roots_env


class TestParseDeleteRootsEnv:
    """parse_delete_roots_env must behave identically to the deletion code path."""

    def test_empty_string_returns_empty_list(self):
        assert parse_delete_roots_env("") == []

    def test_colon_separated_paths(self):
        result = parse_delete_roots_env("/media/movies:/media/tv")
        assert result == ["/media/movies", "/media/tv"]

    def test_comma_separated_paths(self):
        """Legacy comma separator is accepted (with warning)."""
        result = parse_delete_roots_env("/media/movies,/media/tv")
        assert result == ["/media/movies", "/media/tv"]

    def test_mixed_separators(self):
        """Mixed separators are accepted for robustness (with error log)."""
        result = parse_delete_roots_env("/media/movies:/media/tv,/data")
        assert set(result) == {"/media/movies", "/media/tv", "/data"}

    def test_whitespace_around_paths_stripped(self):
        result = parse_delete_roots_env("  /media/movies  :  /media/tv  ")
        assert result == ["/media/movies", "/media/tv"]

    def test_single_path_no_separator(self):
        result = parse_delete_roots_env("/media/movies")
        assert result == ["/media/movies"]

    def test_trailing_separator_ignored(self):
        """A trailing ':' should not produce an empty-string entry."""
        result = parse_delete_roots_env("/media/movies:")
        assert result == ["/media/movies"]

    def test_colon_only_returns_empty_list(self):
        assert parse_delete_roots_env(":") == []

    def test_comma_only_returns_empty_list(self):
        assert parse_delete_roots_env(",") == []

    def test_consistent_with_deletion_parser(self):
        """Colon and comma inputs yield identical path lists (finding 31 — same parser)."""
        colon_result = parse_delete_roots_env("/data/movies:/data/tv")
        comma_result = parse_delete_roots_env("/data/movies,/data/tv")
        assert colon_result == comma_result
