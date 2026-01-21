"""Tests for Modal protocol parsing and configuration.

These tests verify:
- Message parsing handles all message types
- URL and header construction is correct
- Config validation works
"""

import pytest

from ex_app.lib.protocols.modal import (
    ModalConfig,
    ModalMessage,
    ModalMessageType,
    parse_modal_message,
    get_modal_url,
    get_modal_headers,
)


class TestParseModalMessage:
    """Tests for parse_modal_message function."""

    def test_parse_token_message(self):
        """Token message should parse text correctly."""
        raw = '{"type": "token", "text": "hello world"}'
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.TOKEN
        assert msg.text == "hello world"
        assert msg.is_token
        assert msg.has_text

    def test_parse_token_empty_text(self):
        """Token with empty text should parse."""
        raw = '{"type": "token", "text": ""}'
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.TOKEN
        assert msg.text == ""
        assert not msg.has_text

    def test_parse_vad_end(self):
        """VAD end message should be recognized."""
        raw = '{"type": "vad_end"}'
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.VAD_END
        assert msg.is_vad_end
        assert not msg.has_text

    def test_parse_error_message(self):
        """Error message should capture error text."""
        raw = '{"type": "error", "message": "Connection failed"}'
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.ERROR
        assert msg.error_message == "Connection failed"
        assert msg.is_error

    def test_parse_ping(self):
        """Ping message should be recognized."""
        raw = '{"type": "ping"}'
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.PING

    def test_parse_unknown_type(self):
        """Unknown message type should return UNKNOWN."""
        raw = '{"type": "something_new", "data": 123}'
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.UNKNOWN
        assert msg.raw == raw

    def test_parse_invalid_json(self):
        """Invalid JSON should return ERROR with message."""
        raw = "not valid json {"
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.ERROR
        assert "Invalid JSON" in msg.error_message

    def test_parse_missing_type_field(self):
        """Message without type field should return UNKNOWN."""
        raw = '{"text": "orphan text"}'
        msg = parse_modal_message(raw)

        assert msg.type == ModalMessageType.UNKNOWN

    def test_raw_preserved(self):
        """Original message should be preserved in raw field."""
        raw = '{"type": "token", "text": "test"}'
        msg = parse_modal_message(raw)

        assert msg.raw == raw


class TestModalMessageProperties:
    """Tests for ModalMessage property methods."""

    def test_is_token(self):
        """is_token property should work correctly."""
        token_msg = ModalMessage(type=ModalMessageType.TOKEN, text="hi")
        other_msg = ModalMessage(type=ModalMessageType.PING)

        assert token_msg.is_token
        assert not other_msg.is_token

    def test_is_vad_end(self):
        """is_vad_end property should work correctly."""
        vad_msg = ModalMessage(type=ModalMessageType.VAD_END)
        other_msg = ModalMessage(type=ModalMessageType.TOKEN)

        assert vad_msg.is_vad_end
        assert not other_msg.is_vad_end

    def test_is_error(self):
        """is_error property should work correctly."""
        error_msg = ModalMessage(type=ModalMessageType.ERROR, error_message="fail")
        other_msg = ModalMessage(type=ModalMessageType.TOKEN)

        assert error_msg.is_error
        assert not other_msg.is_error

    def test_has_text(self):
        """has_text should return True only when text is non-empty."""
        with_text = ModalMessage(type=ModalMessageType.TOKEN, text="hello")
        without_text = ModalMessage(type=ModalMessageType.TOKEN, text="")
        whitespace = ModalMessage(type=ModalMessageType.TOKEN, text="   ")

        assert with_text.has_text
        assert not without_text.has_text
        assert whitespace.has_text  # Note: whitespace is truthy


class TestGetModalUrl:
    """Tests for get_modal_url function."""

    def test_constructs_correct_url(self):
        """URL should include workspace and correct path."""
        url = get_modal_url("my-workspace")

        assert url == "wss://my-workspace--kyutai-stt-kyutaisttservice-serve.modal.run/v1/stream"

    def test_handles_special_characters(self):
        """Workspace with special characters should work."""
        url = get_modal_url("user-123-test")

        assert "user-123-test" in url


class TestGetModalHeaders:
    """Tests for get_modal_headers function."""

    def test_returns_correct_headers(self):
        """Should return Modal-Key and Modal-Secret headers."""
        headers = get_modal_headers("my_key", "my_secret")

        assert headers["Modal-Key"] == "my_key"
        assert headers["Modal-Secret"] == "my_secret"
        assert len(headers) == 2


class TestModalConfig:
    """Tests for ModalConfig dataclass."""

    def test_url_property(self):
        """url property should construct correct URL."""
        config = ModalConfig(
            workspace="test-workspace",
            key="key123",
            secret="secret456",
        )

        assert config.url == "wss://test-workspace--kyutai-stt-kyutaisttservice-serve.modal.run/v1/stream"

    def test_headers_property(self):
        """headers property should return auth headers."""
        config = ModalConfig(
            workspace="test-workspace",
            key="key123",
            secret="secret456",
        )

        headers = config.headers
        assert headers["Modal-Key"] == "key123"
        assert headers["Modal-Secret"] == "secret456"

    def test_is_configured_all_set(self):
        """is_configured should return True when all fields set."""
        config = ModalConfig(
            workspace="test",
            key="key",
            secret="secret",
        )

        assert config.is_configured()

    def test_is_configured_missing_workspace(self):
        """is_configured should return False when workspace missing."""
        config = ModalConfig(workspace="", key="key", secret="secret")

        assert not config.is_configured()

    def test_is_configured_missing_key(self):
        """is_configured should return False when key missing."""
        config = ModalConfig(workspace="test", key="", secret="secret")

        assert not config.is_configured()

    def test_is_configured_missing_secret(self):
        """is_configured should return False when secret missing."""
        config = ModalConfig(workspace="test", key="key", secret="")

        assert not config.is_configured()

    def test_immutable(self):
        """Config should be immutable."""
        config = ModalConfig(workspace="test", key="key", secret="secret")

        with pytest.raises(Exception):
            config.workspace = "new"


class TestModalMessageImmutable:
    """Tests for ModalMessage immutability."""

    def test_message_is_frozen(self):
        """ModalMessage should be immutable."""
        msg = ModalMessage(type=ModalMessageType.TOKEN, text="hello")

        with pytest.raises(Exception):
            msg.text = "modified"
