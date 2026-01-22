"""Tests for TranscriptionSession using real Modal.

These test the full refactored pipeline:
  WAV file → new modules → Modal → transcription
"""

import asyncio
import os
from pathlib import Path

import pytest

from ex_app.lib.protocols.modal import ModalConfig
from ex_app.lib.transcription.session import transcribe_file


# Sample files
SAMPLES_DIR = Path("/home/silvio/dev/kyutai_modal/samples/wav24k")


def load_expected(wav_path: Path) -> str:
    """Load expected transcription."""
    txt_path = wav_path.with_suffix(".wav.txt")
    return txt_path.read_text().strip() if txt_path.exists() else ""


def normalize(text: str) -> str:
    """Normalize for comparison."""
    import re
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text


def similarity(expected: str, actual: str) -> float:
    """Word-level Jaccard similarity."""
    expected_words = set(normalize(expected).split())
    actual_words = set(normalize(actual).split())
    if not expected_words:
        return 1.0 if not actual_words else 0.0
    intersection = expected_words & actual_words
    union = expected_words | actual_words
    return len(intersection) / len(union) if union else 1.0


# Skip if Modal not configured
MODAL_CONFIGURED = all([
    os.getenv("MODAL_WORKSPACE"),
    os.getenv("MODAL_KEY"),
    os.getenv("MODAL_SECRET"),
])

pytestmark = pytest.mark.skipif(
    not MODAL_CONFIGURED,
    reason="Modal credentials not configured"
)


class TestTranscribeFile:
    """Test transcribe_file convenience function."""

    @pytest.mark.asyncio
    async def test_transcribe_chunk_0(self):
        """Transcribe chunk_0.wav with refactored code."""
        wav_path = SAMPLES_DIR / "chunk_0.wav"
        if not wav_path.exists():
            pytest.skip("Sample not available")

        expected = load_expected(wav_path)
        if not expected:
            pytest.skip("No expected transcription")

        actual = await transcribe_file(str(wav_path))

        print(f"\nExpected: {expected}")
        print(f"Actual:   {actual}")

        sim = similarity(expected, actual)
        print(f"Similarity: {sim:.2%}")

        assert sim > 0.5, f"Too different: {actual}"

    @pytest.mark.asyncio
    async def test_transcribe_chunk_2(self):
        """Transcribe chunk_2.wav with refactored code."""
        wav_path = SAMPLES_DIR / "chunk_2.wav"
        if not wav_path.exists():
            pytest.skip("Sample not available")

        expected = load_expected(wav_path)
        if not expected:
            pytest.skip("No expected transcription")

        actual = await transcribe_file(str(wav_path))

        print(f"\nExpected: {expected}")
        print(f"Actual:   {actual}")

        sim = similarity(expected, actual)
        print(f"Similarity: {sim:.2%}")

        assert sim > 0.5


class TestModalConfig:
    """Test ModalConfig from environment."""

    def test_from_env_configured(self):
        """ModalConfig.from_env should work when vars are set."""
        config = ModalConfig.from_env()

        if not MODAL_CONFIGURED:
            assert not config.is_configured()
        else:
            assert config.is_configured()
            assert config.workspace
            assert config.key
            assert config.secret


class TestNewModulesIntegration:
    """Integration tests verifying new modules work together."""

    @pytest.mark.asyncio
    async def test_audio_processing_to_modal(self):
        """Test that audio.processing output is accepted by Modal."""
        from ex_app.lib.audio.processing import (
            load_wav_file,
            resample,
            int16_to_float32,
        )
        from ex_app.lib.transport.modal_client import ModalSTTClient

        wav_path = SAMPLES_DIR / "chunk_0.wav"
        if not wav_path.exists():
            pytest.skip("Sample not available")

        # Use new audio processing
        audio, sr = load_wav_file(wav_path)
        if sr != 24000:
            audio = resample(audio, sr, 24000)
        audio_float = int16_to_float32(audio)
        audio_bytes = audio_float.tobytes()

        # Use new transport
        config = ModalConfig.from_env()
        client = ModalSTTClient(config)

        tokens = []
        async with client.connect() as stream:
            # Send audio in one chunk
            await stream.send.send(audio_bytes)

            # Collect some tokens
            count = 0
            async for msg in stream.receive:
                if msg.is_token and msg.has_text:
                    tokens.append(msg.text)
                    count += 1
                    if count >= 10:
                        break
                elif msg.is_vad_end:
                    break

        # Should have received some transcription
        text = "".join(tokens)
        print(f"\nReceived: {text}")
        assert len(tokens) > 0, "No tokens received"

    @pytest.mark.asyncio
    async def test_protocol_parsing_real_messages(self):
        """Test that protocol parsing works with real Modal messages."""
        from ex_app.lib.protocols.modal import parse_modal_message, ModalMessageType
        from ex_app.lib.transport.modal_client import ModalSTTClient
        import numpy as np

        config = ModalConfig.from_env()
        client = ModalSTTClient(config)

        # Send silence and verify we can parse responses
        silence = np.zeros(24000, dtype=np.float32).tobytes()

        message_types_seen = set()

        async with client.connect() as stream:
            await stream.send.send(silence)

            # Collect a few messages
            count = 0
            async for msg in stream.receive:
                message_types_seen.add(msg.type)
                count += 1
                if count >= 5:
                    break

        # Should at least see PING or TOKEN
        print(f"\nMessage types seen: {message_types_seen}")
        assert len(message_types_seen) > 0
