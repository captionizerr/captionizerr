"""Language codes: ISO 639-1 (what users type) <-> the 639-2 forms found in MKV tags and sidecar names."""
from __future__ import annotations

# 639-1 -> all 639-2 spellings (bibliographic and terminology) seen in the wild
ISO3 = {
    "en": ("eng",), "ru": ("rus",), "uk": ("ukr",), "de": ("ger", "deu"), "fr": ("fre", "fra"),
    "es": ("spa",), "it": ("ita",), "pt": ("por",), "pl": ("pol",), "nl": ("dut", "nld"),
    "ja": ("jpn",), "ko": ("kor",), "zh": ("chi", "zho"), "sv": ("swe",), "no": ("nor", "nob", "nno"),
    "da": ("dan",), "fi": ("fin",), "tr": ("tur",), "ar": ("ara",), "he": ("heb",), "hi": ("hin",),
    "cs": ("cze", "ces"), "hu": ("hun",), "ro": ("rum", "ron"), "el": ("gre", "ell"), "th": ("tha",),
    "id": ("ind",), "vi": ("vie",), "be": ("bel",), "kk": ("kaz",),
}
_TO2 = {c3: c2 for c2, c3s in ISO3.items() for c3 in c3s}


def norm(code: str | None) -> str | None:
    """'eng' / 'EN' / 'en-US' / 'pt-BR' -> 'en' / 'pt'. Unknown or 'und' -> None."""
    if not code:
        return None
    c = code.strip().lower().replace("_", "-").split("-")[0]
    if c in ISO3:
        return c
    return _TO2.get(c)


def same(a: str | None, b: str | None) -> bool:
    return norm(a) is not None and norm(a) == norm(b)
