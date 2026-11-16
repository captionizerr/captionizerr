"""Translation profile: the style decisions a user sets once and reuses."""
from __future__ import annotations

from dataclasses import dataclass

LANG_NAMES = {
    "ru": "Russian", "uk": "Ukrainian", "de": "German", "es": "Spanish", "en": "English",
    "fr": "French", "it": "Italian", "pt": "Portuguese", "pl": "Polish", "nl": "Dutch",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
    "fi": "Finnish", "tr": "Turkish", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi", "cs": "Czech",
    "hu": "Hungarian", "ro": "Romanian", "el": "Greek", "th": "Thai", "id": "Indonesian", "vi": "Vietnamese",
    "be": "Belarusian", "kk": "Kazakh",
}

# Script the target language is written in; used to spot untranslated leftovers.
LANG_SCRIPT = {"ru": "cyrillic", "uk": "cyrillic", "de": "latin", "es": "latin", "en": "latin",
               "fr": "latin", "it": "latin", "pt": "latin", "pl": "latin", "nl": "latin", "sv": "latin",
               "no": "latin", "da": "latin", "fi": "latin", "tr": "latin", "cs": "latin", "hu": "latin",
               "ro": "latin", "id": "latin", "vi": "latin", "be": "cyrillic", "kk": "cyrillic", "el": "greek",
               "ja": "cjk", "ko": "hangul", "zh": "cjk", "ar": "arabic", "he": "hebrew", "hi": "devanagari",
               "th": "thai"}

# Typographic conventions per target language (what a professional subtitler would do).
LANG_STYLE = {
    "ru": "Use «ёлочки» quotes, an em dash (—) at the start of each speaker's line in two-speaker cues, "
          "natural spoken Russian; ты/вы and verb/adjective gender must match speaker and addressee.",
    "uk": "Use «ялинки» quotes, an em dash (—) for speaker changes, natural spoken Ukrainian (not Russian calques); "
          "ти/ви and grammatical gender must match speaker and addressee.",
    "de": "Use „deutsche“ quotes, a hyphen dash (-) for speaker changes, du/Sie chosen per relationship and kept consistent.",
    "es": "Use «comillas» or \"comillas\" consistently, a dash (-) for speaker changes, tú/usted per relationship, "
          "inverted ¿¡ marks; neutral Spanish unless the profile says otherwise.",
    "en": "Natural idiomatic English; keep the register of the original.",
}

TONE_RULES = {
    "faithful": "Keep profanity, slurs-as-characterisation and vulgarity exactly as strong as the original. "
                "Never soften, never add swearing the source doesn't have.",
    "softened": "Soften profanity by one level (e.g. 'fuck' → a strong but non-obscene word). Keep insults' intent.",
    "family": "Family-friendly: replace profanity with mild expressions; keep the emotion and intent.",
}

SONG_RULES = {
    "verse": "Render songs, poems and rhymes as rhymed, metrically natural verse in the target language, in italics "
             "(<i>…</i>), keeping ♪ markers. Meaning may bend to keep rhyme; never output a literal gloss.",
    "literal": "Translate songs and poems literally, in italics, keeping ♪ markers.",
    "original": "Leave song lyrics in the original language unchanged.",
    "skip": "Drop song lyric cues (output them empty).",
}


@dataclass
class Profile:
    target_lang: str = "ru"
    source_lang: str = "en"
    tone: str = "faithful"           # faithful | softened | family
    songs: str = "verse"             # verse | literal | original | skip
    drop_sdh: bool = True            # drop [SOUND] descriptions and speaker labels
    names: str = "tradition"         # tradition | transliterate | original
    max_line: int = 42
    max_lines: int = 2
    target_cps: float = 20.0

    @property
    def target_name(self) -> str:
        return LANG_NAMES.get(self.target_lang, self.target_lang)

    @property
    def source_name(self) -> str:
        return LANG_NAMES.get(self.source_lang, self.source_lang)
