"""The audio and subtitle tracks of the version of an item that Watch plays, and which of them
to play for a person.

Every subtitle is burned into the picture, so choosing one is choosing to burn it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypedDict
from xml.etree.ElementTree import Element

from mediaman.services.media_meta.plex_watch import PlexWatch

_ID_RE = re.compile(r"[0-9]{1,18}")
_LABEL_MAX = 120
_NAME_MAX = 60

# ISO 639-1, 639-2/B where it differs from /T, 639-2/T, English name | name in the language
# itself. A language without a 639-1 code has "-" there and goes by its 639-2 code.
_LANGUAGES = """
af - afr Afrikaans | Afrikaans
sq alb sqi Albanian | Shqip
am - amh Amharic | አማርኛ
ar - ara Arabic | العربية
hy arm hye Armenian | Հայերեն
az - aze Azerbaijani | Azərbaycanca
eu baq eus Basque | Euskara
be - bel Belarusian | Беларуская
bn - ben Bengali | বাংলা
bs - bos Bosnian | Bosanski
br - bre Breton | Brezhoneg
bg - bul Bulgarian | Български
my bur mya Burmese | မြန်မာ
ca - cat Catalan | Català
zh chi zho Chinese | 中文
- - yue Cantonese | 粵語
hr - hrv Croatian | Hrvatski
cs cze ces Czech | Čeština
da - dan Danish | Dansk
nl dut nld Dutch | Nederlands
en - eng English | English
eo - epo Esperanto | Esperanto
et - est Estonian | Eesti
fo - fao Faroese | Føroyskt
- - fil Filipino | Filipino
fi - fin Finnish | Suomi
fr fre fra French | Français
gl - glg Galician | Galego
ka geo kat Georgian | ქართული
de ger deu German | Deutsch
el gre ell Greek | Ελληνικά
gu - guj Gujarati | ગુજરાતી
ha - hau Hausa | Hausa
- - haw Hawaiian | ʻŌlelo Hawaiʻi
he - heb Hebrew | עברית
hi - hin Hindi | हिन्दी
hu - hun Hungarian | Magyar
is ice isl Icelandic | Íslenska
ig - ibo Igbo | Igbo
id - ind Indonesian | Bahasa Indonesia
ga - gle Irish | Gaeilge
it - ita Italian | Italiano
ja - jpn Japanese | 日本語
kn - kan Kannada | ಕನ್ನಡ
kk - kaz Kazakh | Қазақ тілі
km - khm Khmer | ខ្មែរ
ko - kor Korean | 한국어
ku - kur Kurdish | Kurdî
lo - lao Lao | ລາວ
la - lat Latin | Latina
lv - lav Latvian | Latviešu
lt - lit Lithuanian | Lietuvių
lb - ltz Luxembourgish | Lëtzebuergesch
mk mac mkd Macedonian | Македонски
ms may msa Malay | Bahasa Melayu
ml - mal Malayalam | മലയാളം
mt - mlt Maltese | Malti
mi mao mri Maori | Māori
mr - mar Marathi | मराठी
mn - mon Mongolian | Монгол
ne - nep Nepali | नेपाली
no - nor Norwegian | Norsk
nb - nob Norwegian | Norsk bokmål
nn - nno Norwegian Nynorsk | Norsk nynorsk
fa per fas Persian | فارسی
pl - pol Polish | Polski
pt - por Portuguese | Português
pa - pan Punjabi | ਪੰਜਾਬੀ
ps - pus Pashto | پښتو
ro rum ron Romanian | Română
ru - rus Russian | Русский
gd - gla Scottish Gaelic | Gàidhlig
sr - srp Serbian | Српски
si - sin Sinhala | සිංහල
sk slo slk Slovak | Slovenčina
sl - slv Slovenian | Slovenščina
so - som Somali | Soomaali
es - spa Spanish | Español
sw - swa Swahili | Kiswahili
sv - swe Swedish | Svenska
tl - tgl Tagalog | Tagalog
ta - tam Tamil | தமிழ்
te - tel Telugu | తెలుగు
th - tha Thai | ไทย
bo tib bod Tibetan | བོད་སྐད་
tr - tur Turkish | Türkçe
uk - ukr Ukrainian | Українська
ur - urd Urdu | اردو
uz - uzb Uzbek | Oʻzbekcha
vi - vie Vietnamese | Tiếng Việt
cy wel cym Welsh | Cymraeg
xh - xho Xhosa | isiXhosa
yi - yid Yiddish | ייִדיש
yo - yor Yoruba | Yorùbá
zu - zul Zulu | isiZulu
"""


def _language_table() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    codes: dict[str, str] = {}
    names: dict[str, str] = {}
    natives: dict[str, str] = {}
    for line in _LANGUAGES.strip().splitlines():
        one, bib, term, both = line.split(" ", 3)
        code = term if one == "-" else one
        names[code], natives[code] = both.split(" | ")
        for alias in (one, bib, term):
            if alias != "-":
                codes[alias] = code
    # Bokmål is what "Norwegian" means on nearly every file.
    codes["nb"] = codes["nob"] = "no"
    del names["nb"], natives["nb"]
    return codes, names, natives


_CODES, _NAMES, _NATIVES = _language_table()
#: Every language a default may be set to, by code: its English name.
LANGUAGES: dict[str, str] = dict(sorted(_NAMES.items(), key=lambda kv: kv[1]))
#: Each of ``LANGUAGES`` by code: its name in itself, so every browser shows the same.
NATIVE_NAMES: dict[str, str] = {code: _NATIVES[code] for code in LANGUAGES}


def normalise_language(code: str | None) -> str | None:
    """One code for each language: ISO 639-1 where it has one, else 639-2/T; None when unknown.

    Takes 639-1, 639-2/B or /T, or a BCP 47 tag such as ``pt-BR``, in any case.
    """
    if not code:
        return None
    primary = re.split(r"[-_]", code.strip().lower(), maxsplit=1)[0]
    return _CODES.get(primary)


class Track(TypedDict):
    id: str
    label: str
    selected: bool
    #: The language's code as ``normalise_language`` gives it.
    language: str | None
    language_name: str | None
    title: str | None
    codec: str | None
    channels: int | None
    forced: bool
    default: bool
    hearing_impaired: bool
    commentary: bool
    #: A subtitle file beside the video rather than one inside it.
    external: bool


def _text(value: str | None, limit: int) -> str | None:
    tidy = " ".join((value or "").split())[:limit]
    return tidy or None


def _track(stream: Element, number: int) -> Track:
    name = stream.get("extendedDisplayTitle") or stream.get("displayTitle") or ""
    title = _text(stream.get("title"), _LABEL_MAX)
    language = normalise_language(stream.get("languageTag")) or normalise_language(
        stream.get("languageCode")
    )
    channels = stream.get("channels", "")
    words = f"{title or ''} {name}".lower()
    return {
        "id": stream.get("id", ""),
        "label": _text(name, _LABEL_MAX) or f"Track {number}",
        "selected": stream.get("selected") == "1",
        "language": language,
        "language_name": _text(stream.get("language"), _NAME_MAX) or _NAMES.get(language or ""),
        "title": title,
        "codec": _text(stream.get("codec"), 20),
        "channels": int(channels) if channels.isdigit() and len(channels) <= 2 else None,
        "forced": stream.get("forced") == "1",
        "default": stream.get("default") == "1",
        "hearing_impaired": stream.get("hearingImpaired") == "1" or "sdh" in words.split(),
        "commentary": "commentary" in words,
        "external": bool(stream.get("key")),
    }


def tracks_of(node: Element) -> tuple[list[Track], list[Track]]:
    """Return ``(audio, subtitles)`` of an item's metadata node."""
    audio: list[Track] = []
    subtitles: list[Track] = []
    for stream in node.findall("Media[1]/Part[1]/Stream"):
        tracks = {"2": audio, "3": subtitles}.get(stream.get("streamType", ""))
        if tracks is None or not _ID_RE.fullmatch(stream.get("id", "")):
            continue
        tracks.append(_track(stream, len(tracks) + 1))
    return audio, subtitles


def read_tracks(plex: PlexWatch, rating_key: str) -> tuple[list[Track], list[Track]]:
    """Return ``(audio, subtitles)``, from the metadata ``get_item`` read where it did."""
    return tracks_of(plex.item_node(rating_key))


@dataclass(frozen=True)
class Remembered:
    """A person's choice for a show or movie, made while playing *rating_key*.

    The stream ids hold for that item only; the language and flags carry to the next episode.
    """

    rating_key: str
    audio_id: str | None = None
    audio_lang: str | None = None
    #: None for no choice, "off", or "on" for the subtitle described below.
    subtitle: str | None = None
    subtitle_id: str | None = None
    subtitle_lang: str | None = None
    subtitle_forced: bool = False
    subtitle_hearing_impaired: bool = False


@dataclass(frozen=True)
class Pick:
    audio: str | None
    subtitle: str | None

    @property
    def burn(self) -> bool:
        return self.subtitle is not None


def _first(tracks: list[Track]) -> str | None:
    return tracks[0]["id"] if tracks else None


def _best_audio(audio: list[Track], language: str | None) -> str | None:
    """The track in *language* to play: not a commentary, flagged default, most channels."""
    found = [t for t in audio if language is not None and t["language"] == language]
    found.sort(key=lambda t: (t["commentary"], not t["default"], -(t["channels"] or 0)))
    return _first(found)


def _forced_in(subtitles: list[Track], language: str | None) -> str | None:
    """The signs and songs in *language*, which show whatever else is chosen."""
    found = [t for t in subtitles if t["forced"] and language and t["language"] == language]
    found.sort(key=lambda t: (not t["default"], t["hearing_impaired"]))
    return _first(found)


def _full_in(subtitles: list[Track], language: str) -> str | None:
    """The whole of the dialogue in *language*: plain before SDH, default first."""
    found = [t for t in subtitles if not t["forced"] and t["language"] == language]
    found.sort(key=lambda t: (t["hearing_impaired"], not t["default"]))
    return _first(found)


def _like(subtitles: list[Track], chosen: Remembered) -> str | None:
    """The track of this item that is what *chosen* names on another."""
    found = [
        t
        for t in subtitles
        if chosen.subtitle_lang
        and t["language"] == chosen.subtitle_lang
        and t["forced"] == chosen.subtitle_forced
    ]
    found.sort(
        key=lambda t: (t["hearing_impaired"] != chosen.subtitle_hearing_impaired, not t["default"])
    )
    return _first(found)


def resolve(
    audio: list[Track],
    subtitles: list[Track],
    rating_key: str,
    *,
    audio_lang: str | None = None,
    subtitle_lang: str | None = None,
    remembered: Remembered | None = None,
    audio_id: str | None = None,
    subtitle: str | None = None,
) -> Pick:
    """The tracks to play of *rating_key* for a person.

    *audio_id* and *subtitle* (a track id or "off") are asked for this one start and come first,
    then what the person *remembered* for the title, then their defaults: *audio_lang* (None for
    what Plex has selected) and *subtitle_lang* (None for only what is needed, "off" for none, a
    code for subtitles in that language whenever the audio is in another). A forced track in the
    audio's language shows unless subtitles were turned off, for the title or by default. Ids must be among the tracks.
    """
    same = remembered is not None and remembered.rating_key == rating_key
    audio_ids = {t["id"] for t in audio}
    chosen = audio_id
    if chosen is None and remembered is not None:
        if same and remembered.audio_id in audio_ids:
            chosen = remembered.audio_id
        else:
            chosen = _best_audio(audio, remembered.audio_lang)
    if chosen is None:
        chosen = _best_audio(audio, audio_lang)
    if chosen is None:
        chosen = next((t["id"] for t in audio if t["selected"]), None) or _first(audio)
    playing = next((t["language"] for t in audio if t["id"] == chosen), None)

    if subtitle is not None:
        return Pick(chosen, None if subtitle == "off" else subtitle)
    if remembered is not None and remembered.subtitle == "off":
        return Pick(chosen, None)
    if remembered is not None and remembered.subtitle == "on":
        ids = {t["id"] for t in subtitles}
        kept = remembered.subtitle_id if same and remembered.subtitle_id in ids else None
        found = kept or _like(subtitles, remembered)
        if found is not None:
            return Pick(chosen, found)
    if subtitle_lang == "off":
        return Pick(chosen, None)
    wanted = normalise_language(subtitle_lang)
    if wanted is not None and wanted != playing:
        full = _full_in(subtitles, wanted)
        if full is not None:
            return Pick(chosen, full)
    return Pick(chosen, _forced_in(subtitles, playing))
