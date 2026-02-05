"""Tests for transcriber module."""

import asyncio
import logging
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
        expected = "wss://my-workspace--kyutai-stt-rust-kyutaisttrustservice-serve.modal.run/v1/stream"
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
        """Should raise error when credentials are actually missing on the instance."""
        from ex_app.lib.livetypes import ModalConnectionError

        # Create transcriber and manually clear credentials after construction
        transcriber = ModalTranscriber(session_id="test")
        transcriber.workspace = ""
        transcriber.modal_key = ""
        transcriber.modal_secret = ""

        with pytest.raises(ModalConnectionError):
            await transcriber.connect()

    def test_log_transcript_includes_speaker(self, caplog):
        """Should log speaker context with transcript."""
        transcriber = ModalTranscriber(
            session_id="speaker123",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        with caplog.at_level(logging.INFO):
            transcriber._log_transcript("hello world", final=True)
        assert any(
            "[speaker=speaker123]" in record.message for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_flush_buffer_stops_on_connection_closed(self):
        """Should stop transcriber when Modal closes during send."""
        transcriber = ModalTranscriber(
            session_id="sess123",
            workspace="ws",
            modal_key="key",
            modal_secret="secret",
        )
        transcriber._ws = MagicMock()
        transcriber._ws.send = AsyncMock(
            side_effect=Exception("placeholder")
        )
        transcriber._running = True
        transcriber._audio_buffer = [np.zeros(100, dtype=np.int16)]
        transcriber._buffer_duration_ms = transcriber._min_buffer_ms

        # Simulate websockets connection closed during send
        from websockets.frames import Close
        from websockets.exceptions import ConnectionClosedOK

        close_frame = Close(1000, "OK")
        transcriber._ws.send.side_effect = ConnectionClosedOK(
            rcvd=close_frame, sent=close_frame, rcvd_then_sent=True
        )

        await transcriber._flush_buffer()

        assert transcriber._running is False
        assert transcriber._ws is None
        assert transcriber._audio_buffer == []

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
