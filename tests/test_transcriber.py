"""Tests for transcriber module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from ex_app.lib.transcriber import (
    AudioResampler,
    ModalTranscriber,
    TranscriptionResult,
    TranscriberFactory,
)


class TestAudioResampler:
    """Tests for AudioResampler class."""

    def test_same_rate_no_change(self):
        """Should not resample if rates are equal."""
        resampler = AudioResampler(48000, 48000)
        audio = np.array([1, 2, 3, 4, 5], dtype=np.int16)
        result = resampler.resample(audio)
        np.testing.assert_array_equal(result, audio)

    def test_downsample(self):
        """Should downsample audio."""
        resampler = AudioResampler(48000, 24000)
        audio = np.zeros(4800, dtype=np.int16)
        result = resampler.resample(audio)
        # Should be roughly half the length
        assert len(result) == pytest.approx(2400, rel=0.1)

    def test_upsample(self):
        """Should upsample audio."""
        resampler = AudioResampler(24000, 48000)
        audio = np.zeros(2400, dtype=np.int16)
        result = resampler.resample(audio)
        # Should be roughly double the length
        assert len(result) == pytest.approx(4800, rel=0.1)


class TestTranscriptionResult:
    """Tests for TranscriptionResult dataclass."""

    def test_create_result(self):
        """Should create result with all fields."""
        result = TranscriptionResult(
            text="Hello",
            is_final=True,
            is_vad_end=True,
        )
        assert result.text == "Hello"
        assert result.is_final is True
        assert result.is_vad_end is True

    def test_default_vad_end(self):
        """Should default is_vad_end to False."""
        result = TranscriptionResult(text="Hi", is_final=False)
        assert result.is_vad_end is False


class TestModalTranscriber:
    """Tests for ModalTranscriber class."""

    def test_create_transcriber(self):
        """Should create transcriber with session ID."""
        transcriber = ModalTranscriber(
            session_id="test123",
            language="en",
            workspace="test-workspace",
            modal_key="test-key",
            modal_secret="test-secret",
        )
        assert transcriber.session_id == "test123"
        assert transcriber.language == "en"
        assert transcriber.workspace == "test-workspace"

    def test_url_construction(self):
        """Should construct correct Modal URL."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="my-workspace",
            modal_key="key",
            modal_secret="secret",
        )
        expected = "wss://my-workspace--kyutai-stt-kyutaisttservice-serve.modal.run/v1/stream"
        assert transcriber.url == expected

    def test_set_language(self):
        """Should allow setting language."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        transcriber.set_language("fr")
        assert transcriber.language == "fr"

    def test_parse_token_result(self):
        """Should parse token message."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        result = transcriber._parse_result('{"type": "token", "text": " Hello"}')
        assert result is not None
        assert result.text == " Hello"
        assert result.is_final is False

    def test_parse_vad_end_result(self):
        """Should parse VAD end message."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        result = transcriber._parse_result('{"type": "vad_end"}')
        assert result is not None
        assert result.is_vad_end is True
        assert result.is_final is True

    def test_parse_ping_result(self):
        """Should ignore ping messages."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        result = transcriber._parse_result('{"type": "ping"}')
        assert result is None

    def test_parse_error_result(self):
        """Should handle error messages."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        result = transcriber._parse_result('{"type": "error", "message": "Something went wrong"}')
        assert result is None

    def test_parse_invalid_json(self):
        """Should handle invalid JSON."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        result = transcriber._parse_result('not valid json')
        assert result is None


class TestTranscriberFactory:
    """Tests for TranscriberFactory class."""

    def test_create_transcriber(self):
        """Should create transcriber via factory."""
        with patch.dict(
            "os.environ",
            {
                "MODAL_WORKSPACE": "test-ws",
                "MODAL_KEY": "test-key",
                "MODAL_SECRET": "test-secret",
            },
        ):
            transcriber = TranscriberFactory.create(
                session_id="sess123",
                language="fr",
            )
            assert isinstance(transcriber, ModalTranscriber)
            assert transcriber.session_id == "sess123"
            assert transcriber.language == "fr"


class TestModalTranscriberAsync:
    """Async tests for ModalTranscriber."""

    @pytest.mark.asyncio
    async def test_connect_missing_credentials(self):
        """Should raise error when credentials are missing."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="",
            modal_key="",
            modal_secret="",
        )
        from ex_app.lib.livetypes import ModalConnectionError

        with pytest.raises(ModalConnectionError):
            await transcriber.connect()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        """Should handle stop when not running."""
        transcriber = ModalTranscriber(
            session_id="test",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        # Should not raise
        await transcriber.stop()
