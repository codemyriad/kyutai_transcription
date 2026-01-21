"""Language models and mappings for Kyutai Transcription."""

from dataclasses import dataclass


@dataclass
class LanguageInfo:
    """Information about a supported language."""

    code: str
    name: str
    native_name: str
    rtl: bool = False


# Kyutai currently supports English and French
# The model is kyutai/stt-1b-en_fr
LANGUAGE_MAP: dict[str, LanguageInfo] = {
    "en": LanguageInfo(
        code="en",
        name="English",
        native_name="English",
        rtl=False,
    ),
    "fr": LanguageInfo(
        code="fr",
        name="French",
        native_name="Français",
        rtl=False,
    ),
}

DEFAULT_LANGUAGE = "en"


def get_supported_languages() -> list[dict]:
    """Get list of supported languages for API response."""
    return [
        {
            "code": lang.code,
            "name": lang.name,
            "nativeName": lang.native_name,
            "rtl": lang.rtl,
        }
        for lang in LANGUAGE_MAP.values()
    ]


def is_language_supported(lang_code: str) -> bool:
    """Check if a language is supported."""
    return lang_code.lower() in LANGUAGE_MAP
