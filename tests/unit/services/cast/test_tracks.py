"""Tests for reading an item's tracks and choosing which of them to play."""

from __future__ import annotations

import defusedxml.ElementTree as ET
import pytest

from mediaman.services.cast.tracks import Remembered, normalise_language, resolve, tracks_of


def _streams(*streams: str) -> tuple[list, list]:
    xml = "<Video><Media><Part>" + "".join(streams) + "</Part></Media></Video>"
    return tracks_of(ET.fromstring(xml))


def _audio(id_: str, lang: str | None, **attrs: str) -> str:
    more = "".join(f' {k}="{v}"' for k, v in attrs.items())
    code = f' languageCode="{lang}"' if lang else ""
    return f'<Stream id="{id_}" streamType="2"{code}{more}/>'


def _sub(id_: str, lang: str | None, **attrs: str) -> str:
    more = "".join(f' {k}="{v}"' for k, v in attrs.items())
    code = f' languageCode="{lang}"' if lang else ""
    return f'<Stream id="{id_}" streamType="3"{code}{more}/>'


@pytest.mark.parametrize(
    ("given", "code"),
    [
        ("en", "en"),
        ("eng", "en"),
        ("ger", "de"),
        ("deu", "de"),
        ("FRE", "fr"),
        ("fra", "fr"),
        ("pt-BR", "pt"),
        ("nob", "no"),
        ("fil", "fil"),
        ("und", None),
        ("xx", None),
        ("", None),
        (None, None),
    ],
)
def test_languages_come_to_one_code_whatever_form_plex_gives(given, code):
    assert normalise_language(given) == code


def test_a_stream_is_read_with_its_language_flags_and_layout():
    audio, subs = _streams(
        '<Stream id="1" streamType="1"/>',
        _audio("2", "ger", channels="6", codec="ac3", default="1", language="Deutsch"),
        _sub("3", "eng", forced="1", title="Signs &amp; Songs", key="/library/streams/3"),
        _sub("4", None, title="English SDH"),
        '<Stream id="../5" streamType="3"/>',
    )
    assert audio == [
        {
            "id": "2",
            "label": "Track 1",
            "selected": False,
            "language": "de",
            "language_name": "Deutsch",
            "title": None,
            "codec": "ac3",
            "channels": 6,
            "forced": False,
            "default": True,
            "hearing_impaired": False,
            "commentary": False,
            "external": False,
        }
    ]
    signs, sdh = subs
    assert (signs["language"], signs["language_name"], signs["forced"], signs["external"]) == (
        "en",
        "English",
        True,
        True,
    )
    assert (sdh["language"], sdh["hearing_impaired"]) == (None, True)


def test_a_language_tag_wins_over_the_older_code():
    (track,), _ = _streams(_audio("2", "und", languageTag="pt-BR"))
    assert track["language"] == "pt"


def test_without_any_preference_plex_s_selection_plays_and_nothing_is_burned():
    audio, subs = _streams(_audio("1", "eng"), _audio("2", "ger", selected="1"), _sub("3", "eng"))
    pick = resolve(audio, subs, "10")
    assert (pick.audio, pick.subtitle, pick.burn) == ("2", None, False)


def test_a_subtitle_plex_selected_by_itself_is_not_burned():
    audio, subs = _streams(_audio("1", "jpn"), _sub("3", "eng", selected="1"))
    assert resolve(audio, subs, "10").subtitle is None


def test_the_default_language_s_audio_is_played():
    audio, subs = _streams(_audio("1", "ger", selected="1"), _audio("2", "eng"))
    assert resolve(audio, subs, "10", audio_lang="en").audio == "2"


def test_with_no_track_in_the_default_language_plex_s_selection_plays():
    audio, subs = _streams(_audio("1", "ger"), _audio("2", "fre", selected="1"))
    assert resolve(audio, subs, "10", audio_lang="en").audio == "2"


def test_of_several_tracks_in_the_language_a_commentary_comes_last():
    audio, subs = _streams(
        _audio("1", "eng", title="Director's Commentary", default="1", channels="6"),
        _audio("2", "eng", channels="2"),
    )
    assert resolve(audio, subs, "10", audio_lang="en").audio == "2"


def test_of_several_tracks_in_the_language_the_default_then_the_most_channels_wins():
    audio, subs = _streams(
        _audio("1", "eng", channels="2"),
        _audio("2", "eng", channels="8"),
        _audio("3", "eng", channels="6", default="1"),
    )
    assert resolve(audio, subs, "10", audio_lang="en").audio == "3"
    audio, subs = _streams(_audio("1", "eng", channels="2"), _audio("2", "eng", channels="8"))
    assert resolve(audio, subs, "10", audio_lang="en").audio == "2"


def test_a_title_with_one_audio_track_plays_it_whatever_the_default():
    audio, subs = _streams(_audio("1", "kor"))
    assert resolve(audio, subs, "10", audio_lang="en").audio == "1"


def test_a_title_with_no_audio_tracks_names_none():
    assert resolve([], [], "10", audio_lang="en").audio is None


def test_subtitles_in_the_default_language_show_when_the_audio_is_in_another():
    audio, subs = _streams(_audio("1", "jpn"), _sub("3", "eng"), _sub("4", "ger"))
    pick = resolve(audio, subs, "10", subtitle_lang="en")
    assert (pick.subtitle, pick.burn) == ("3", True)


def test_no_subtitles_show_when_the_audio_is_already_in_their_language():
    audio, subs = _streams(_audio("1", "eng"), _sub("3", "eng"))
    assert resolve(audio, subs, "10", subtitle_lang="en").subtitle is None


def test_plain_subtitles_come_before_sdh_and_full_before_forced():
    audio, subs = _streams(
        _audio("1", "jpn"),
        _sub("3", "eng", forced="1"),
        _sub("4", "eng", hearingImpaired="1", default="1"),
        _sub("5", "eng"),
    )
    assert resolve(audio, subs, "10", subtitle_lang="en").subtitle == "5"


def test_sdh_shows_where_it_is_the_only_one_in_the_language():
    audio, subs = _streams(_audio("1", "jpn"), _sub("4", "eng", title="SDH"))
    assert resolve(audio, subs, "10", subtitle_lang="en").subtitle == "4"


def test_an_external_subtitle_counts_as_any_other():
    audio, subs = _streams(_audio("1", "jpn"), _sub("4", "eng", key="/library/streams/4"))
    assert resolve(audio, subs, "10", subtitle_lang="en").subtitle == "4"


def test_an_image_subtitle_is_burned_as_a_text_one_is():
    audio, subs = _streams(_audio("1", "jpn"), _sub("4", "eng", codec="pgs"))
    pick = resolve(audio, subs, "10", subtitle_lang="en")
    assert (pick.subtitle, pick.burn) == ("4", True)


@pytest.mark.parametrize("subtitle_lang", [None, "en"])
def test_forced_signs_in_the_audio_s_language_show_unless_fuller_subtitles_do(subtitle_lang):
    audio, subs = _streams(
        _audio("1", "eng"),
        _sub("3", "jpn", forced="1"),
        _sub("4", "eng", forced="1"),
        _sub("5", "eng"),
    )
    assert resolve(audio, subs, "10", subtitle_lang=subtitle_lang).subtitle == "4"


def test_subtitles_off_by_default_hide_forced_signs_too():
    audio, subs = _streams(_audio("1", "eng"), _sub("4", "eng", forced="1"), _sub("5", "eng"))
    assert resolve(audio, subs, "10", subtitle_lang="off").subtitle is None


def test_with_no_subtitle_in_the_language_forced_signs_still_show():
    audio, subs = _streams(_audio("1", "eng"), _sub("4", "eng", forced="1"), _sub("5", "ger"))
    assert resolve(audio, subs, "10", subtitle_lang="fr").subtitle == "4"


def test_the_title_s_choice_comes_before_the_defaults_on_the_same_item():
    audio, subs = _streams(_audio("1", "eng"), _audio("2", "ger"), _sub("3", "eng"))
    chosen = Remembered("10", audio_id="2", audio_lang="de", subtitle="on", subtitle_id="3")
    pick = resolve(audio, subs, "10", audio_lang="en", remembered=chosen)
    assert (pick.audio, pick.subtitle) == ("2", "3")


def test_the_title_s_choice_carries_to_the_next_episode_by_language_and_kind():
    audio, subs = _streams(
        _audio("21", "eng"),
        _audio("22", "ger"),
        _sub("23", "eng", forced="1"),
        _sub("24", "eng", hearingImpaired="1"),
        _sub("25", "eng"),
    )
    chosen = Remembered(
        "10",
        audio_id="2",
        audio_lang="de",
        subtitle="on",
        subtitle_id="9",
        subtitle_lang="en",
        subtitle_hearing_impaired=True,
    )
    pick = resolve(audio, subs, "20", audio_lang="en", remembered=chosen)
    assert (pick.audio, pick.subtitle) == ("22", "24")


def test_a_title_s_choice_the_next_episode_cannot_follow_falls_back_to_the_defaults():
    audio, subs = _streams(_audio("21", "eng"), _sub("25", "fre"))
    chosen = Remembered("10", audio_lang="de", subtitle="on", subtitle_lang="pl")
    pick = resolve(audio, subs, "20", subtitle_lang="fr", remembered=chosen)
    assert (pick.audio, pick.subtitle) == ("21", "25")


def test_subtitles_turned_off_for_the_title_hide_forced_signs_too():
    audio, subs = _streams(_audio("1", "eng"), _sub("4", "eng", forced="1"))
    pick = resolve(audio, subs, "10", remembered=Remembered("10", subtitle="off"))
    assert pick.subtitle is None


def test_a_title_s_audio_alone_leaves_subtitles_to_the_defaults_for_that_audio():
    audio, subs = _streams(_audio("1", "eng"), _audio("2", "jpn"), _sub("3", "eng"))
    chosen = Remembered("10", audio_id="2", audio_lang="ja")
    assert resolve(audio, subs, "10", subtitle_lang="en", remembered=chosen).subtitle == "3"


def test_tracks_asked_for_one_start_come_before_everything():
    audio, subs = _streams(_audio("1", "eng"), _audio("2", "ger"), _sub("3", "eng"))
    chosen = Remembered("10", audio_id="1", subtitle="on", subtitle_id="3")
    pick = resolve(audio, subs, "10", remembered=chosen, audio_id="2", subtitle="off")
    assert (pick.audio, pick.subtitle) == ("2", None)
