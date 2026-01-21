"""Tests for models module."""

import pytest

from ex_app.lib.models import (
    DEFAULT_LANGUAGE,
    LANGUAGE_MAP,
    LanguageInfo,
    get_supported_languages,
    is_language_supported,
)


class TestLanguageMap:
    """Tests for language map functionality."""

    def test_language_map_not_empty(self):
        """Language map should have entries."""
        assert len(LANGUAGE_MAP) > 0

    def test_language_map_has_english(self):
        """Language map should include English."""
        assert "en" in LANGUAGE_MAP

    def test_language_map_has_french(self):
        """Language map should include French."""
        assert "fr" in LANGUAGE_MAP

    def test_language_info_structure(self):
        """Each language should have proper structure."""
        for code, info in LANGUAGE_MAP.items():
            assert isinstance(info, LanguageInfo)
            assert info.code == code
            assert info.name
            assert info.native_name
            assert isinstance(info.rtl, bool)


class TestGetSupportedLanguages:
    """Tests for get_supported_languages function."""

    def test_returns_list(self):
        """Should return a list."""
        result = get_supported_languages()
        assert isinstance(result, list)

    def test_returns_dicts(self):
        """Should return list of dicts."""
        result = get_supported_languages()
        assert all(isinstance(item, dict) for item in result)

    def test_dict_structure(self):
        """Each dict should have required keys."""
        result = get_supported_languages()
        required_keys = {"code", "name", "nativeName", "rtl"}
        for item in result:
            assert required_keys.issubset(item.keys())


class TestIsLanguageSupported:
    """Tests for is_language_supported function."""

    def test_english_supported(self):
        """English should be supported."""
        assert is_language_supported("en") is True

    def test_french_supported(self):
        """French should be supported."""
        assert is_language_supported("fr") is True

    def test_unsupported_language(self):
        """Unknown language should not be supported."""
        assert is_language_supported("xyz") is False

    def test_case_insensitive(self):
        """Language check should be case insensitive."""
        assert is_language_supported("EN") is True
        assert is_language_supported("Fr") is True


class TestDefaultLanguage:
    """Tests for default language constant."""

    def test_default_is_supported(self):
        """Default language should be supported."""
        assert is_language_supported(DEFAULT_LANGUAGE)

    def test_default_is_english(self):
        """Default language should be English."""
        assert DEFAULT_LANGUAGE == "en"
