"""Tests for mediaman.services.media_meta.anime_detect."""

from __future__ import annotations

from unittest.mock import MagicMock

from mediaman.services.media_meta.anime_detect import _JP_STUDIOS, is_anime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _show(genres: list[str], studio: str = "") -> MagicMock:
    """Build a minimal Plex show stub with the given genres and studio."""
    obj = MagicMock()
    obj.genres = [_genre(g) for g in genres]
    obj.studio = studio
    return obj


def _genre(tag: str) -> MagicMock:
    g = MagicMock()
    g.tag = tag
    return g


# ---------------------------------------------------------------------------
# Explicit Anime genre tag
# ---------------------------------------------------------------------------


class TestExplicitAnimeTag:
    def test_anime_genre_tag_returns_true(self):
        """Explicit 'Anime' genre tag should always return True, regardless of studio."""
        assert is_anime(_show(["Anime"], studio="Pixar")) is True

    def test_anime_tag_case_insensitive(self):
        """Genre matching is case-insensitive."""
        assert is_anime(_show(["ANIME"])) is True

    def test_anime_tag_alongside_animation(self):
        """Both Anime and Animation tags present — must still return True."""
        assert is_anime(_show(["Animation", "Anime"], studio="")) is True


# ---------------------------------------------------------------------------
# Animation genre + Japanese studio heuristic
# ---------------------------------------------------------------------------


class TestAnimationStudioHeuristic:
    def test_animation_plus_known_jp_studio_returns_true(self):
        """Animation genre + known JP studio → True."""
        assert is_anime(_show(["Animation"], studio="MAPPA")) is True

    def test_animation_plus_known_studio_mixed_case(self):
        """Studio matching is case-insensitive."""
        assert is_anime(_show(["Animation"], studio="Kyoto Animation")) is True

    def test_animation_plus_unknown_studio_returns_false(self):
        """Animation genre + Western studio must not be flagged as anime."""
        assert is_anime(_show(["Animation"], studio="Pixar")) is False

    def test_animation_without_studio_returns_false(self):
        """Animation + empty studio → no match → False."""
        assert is_anime(_show(["Animation"], studio="")) is False

    def test_animation_with_none_studio_returns_false(self):
        """studio=None must not raise and must return False."""
        obj = _show(["Animation"])
        obj.studio = None
        assert is_anime(obj) is False


# ---------------------------------------------------------------------------
# Non-anime cases
# ---------------------------------------------------------------------------


class TestNonAnime:
    def test_no_genres_returns_false(self):
        """No genre tags → False."""
        assert is_anime(_show([])) is False

    def test_unrelated_genres_returns_false(self):
        """Drama + Comedy with a JP studio should not be flagged as anime."""
        assert is_anime(_show(["Drama", "Comedy"], studio="toei animation")) is False

    def test_action_only_returns_false(self):
        assert is_anime(_show(["Action"])) is False


# ---------------------------------------------------------------------------
# Known studio constants sanity-checks
# ---------------------------------------------------------------------------


class TestJpStudiosConstant:
    def test_studio_set_is_not_empty(self):
        assert len(_JP_STUDIOS) > 0

    def test_all_studios_are_lower_case(self):
        """Every entry must already be lower-cased so the .lower() compare works."""
        for s in _JP_STUDIOS:
            assert s == s.lower(), f"Studio {s!r} is not lower-cased"

    def test_mappa_present(self):
        assert "mappa" in _JP_STUDIOS

    def test_toei_present(self):
        assert "toei animation" in _JP_STUDIOS
