"""Tests for livetypes module."""

import pytest
from pydantic import ValidationError

from ex_app.lib.livetypes import (
    CallFlag,
    HPBSettings,
    LanguageSetRequest,
    LeaveRequest,
    ReconnectMethod,
    SigConnectResult,
    StunServer,
    Target,
    TranscribeRequest,
    Transcript,
    TranscriptionProviderException,
    TurnServer,
)


class TestTranscribeRequest:
    """Tests for TranscribeRequest model."""

    def test_valid_request(self):
        """Should create valid request."""
        req = TranscribeRequest(
            roomToken="abc123",
            ncSessionId="session456",
            enable=True,
            langId="en",
        )
        assert req.roomToken == "abc123"
        assert req.ncSessionId == "session456"
        assert req.enable is True
        assert req.langId == "en"

    def test_default_enable(self):
        """Enable should default to True."""
        req = TranscribeRequest(
            roomToken="abc",
            ncSessionId="sess",
        )
        assert req.enable is True

    def test_default_language(self):
        """Language should default to 'en'."""
        req = TranscribeRequest(
            roomToken="abc",
            ncSessionId="sess",
        )
        assert req.langId == "en"

    def test_required_fields(self):
        """Should require roomToken and ncSessionId."""
        with pytest.raises(ValidationError):
            TranscribeRequest()

        with pytest.raises(ValidationError):
            TranscribeRequest(roomToken="abc")

        with pytest.raises(ValidationError):
            TranscribeRequest(ncSessionId="sess")


class TestLanguageSetRequest:
    """Tests for LanguageSetRequest model."""

    def test_valid_request(self):
        """Should create valid request."""
        req = LanguageSetRequest(roomToken="abc", langId="fr")
        assert req.roomToken == "abc"
        assert req.langId == "fr"

    def test_required_fields(self):
        """Should require both fields."""
        with pytest.raises(ValidationError):
            LanguageSetRequest()

        with pytest.raises(ValidationError):
            LanguageSetRequest(roomToken="abc")


class TestLeaveRequest:
    """Tests for LeaveRequest model."""

    def test_valid_request(self):
        """Should create valid request."""
        req = LeaveRequest(roomToken="abc")
        assert req.roomToken == "abc"

    def test_required_field(self):
        """Should require roomToken."""
        with pytest.raises(ValidationError):
            LeaveRequest()


class TestHPBSettings:
    """Tests for HPBSettings model."""

    def test_default_values(self):
        """Should have sensible defaults."""
        settings = HPBSettings()
        assert settings.server == ""
        assert settings.stunservers == []
        assert settings.turnservers == []

    def test_with_stun_servers(self):
        """Should accept STUN servers."""
        settings = HPBSettings(
            stunservers=[
                StunServer(urls=["stun:stun.example.com:3478"]),
            ]
        )
        assert len(settings.stunservers) == 1
        assert settings.stunservers[0].urls[0] == "stun:stun.example.com:3478"

    def test_with_turn_servers(self):
        """Should accept TURN servers."""
        settings = HPBSettings(
            turnservers=[
                TurnServer(
                    urls=["turn:turn.example.com:3478"],
                    username="user",
                    credential="pass",
                ),
            ]
        )
        assert len(settings.turnservers) == 1
        assert settings.turnservers[0].username == "user"


class TestTranscript:
    """Tests for Transcript dataclass."""

    def test_create_transcript(self):
        """Should create transcript."""
        t = Transcript(
            final=True,
            lang_id="en",
            message="Hello world",
            speaker_session_id="sess123",
        )
        assert t.final is True
        assert t.lang_id == "en"
        assert t.message == "Hello world"
        assert t.speaker_session_id == "sess123"


class TestEnums:
    """Tests for enum types."""

    def test_sig_connect_result(self):
        """SigConnectResult should have correct values."""
        assert SigConnectResult.SUCCESS == 0
        assert SigConnectResult.FAILURE == 1
        assert SigConnectResult.RETRY == 2

    def test_reconnect_method(self):
        """ReconnectMethod should have correct values."""
        assert ReconnectMethod.NO_RECONNECT == 0
        assert ReconnectMethod.SHORT_RESUME == 1
        assert ReconnectMethod.FULL_RECONNECT == 2

    def test_call_flag(self):
        """CallFlag should have correct values."""
        assert CallFlag.DISCONNECTED == 0
        assert CallFlag.IN_CALL == 1
        assert CallFlag.WITH_AUDIO == 2
        assert CallFlag.WITH_VIDEO == 4
        assert CallFlag.WITH_PHONE == 8

    def test_call_flag_bitwise(self):
        """CallFlag should support bitwise operations."""
        in_call_with_audio = CallFlag.IN_CALL | CallFlag.WITH_AUDIO
        assert in_call_with_audio & CallFlag.IN_CALL
        assert in_call_with_audio & CallFlag.WITH_AUDIO
        assert not (in_call_with_audio & CallFlag.WITH_VIDEO)


class TestTranscriptionProviderException:
    """Tests for TranscriptionProviderException."""

    def test_create_exception(self):
        """Should create exception with message and retcode."""
        exc = TranscriptionProviderException("Something failed", retcode=503)
        assert str(exc) == "Something failed"
        assert exc.retcode == 503

    def test_default_retcode(self):
        """Should default to 500 retcode."""
        exc = TranscriptionProviderException("Error")
        assert exc.retcode == 500
