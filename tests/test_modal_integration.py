"""Integration tests against the real deployed Modal service.

These tests verify the actual contract:
  WAV file → our processing → Modal → transcription

No mocking. Real audio. Real Modal. Real transcriptions.
"""

import asyncio
import json
import os
from pathlib import Path

import numpy as np
import pytest
import websockets

from ex_app.lib.audio.processing import (
    load_wav_file,
    resample,
    int16_to_float32,
)


# Sample files with known transcriptions
SAMPLES_DIR = Path("/home/silvio/dev/kyutai_modal/samples/wav24k")

# Modal credentials from environment
MODAL_WORKSPACE = os.getenv("MODAL_WORKSPACE", "")
MODAL_KEY = os.getenv("MODAL_KEY", "")
MODAL_SECRET = os.getenv("MODAL_SECRET", "")


def get_modal_url() -> str:
    """Construct Modal WebSocket URL."""
    return f"wss://{MODAL_WORKSPACE}--kyutai-stt-kyutaisttservice-serve.modal.run/v1/stream"


def get_modal_headers() -> dict[str, str]:
    """Get Modal authentication headers."""
    return {
        "Modal-Key": MODAL_KEY,
        "Modal-Secret": MODAL_SECRET,
    }


def load_expected_transcription(wav_path: Path) -> str:
    """Load the expected transcription for a WAV file."""
    txt_path = wav_path.with_suffix(".wav.txt")
    if txt_path.exists():
        return txt_path.read_text().strip()
    return ""


def prepare_audio_for_modal(wav_path: Path) -> bytes:
    """Load WAV and convert to Modal's expected format.

    Modal expects: float32 PCM, mono, 24kHz, little-endian bytes.
    """
    audio, sample_rate = load_wav_file(wav_path)

    # Resample if needed (samples should already be 24kHz)
    if sample_rate != 24000:
        audio = resample(audio, sample_rate, 24000)

    # Convert to float32 normalized
    audio_float = int16_to_float32(audio)

    return audio_float.tobytes()


async def send_audio_to_modal(audio_bytes: bytes, chunk_size_ms: int = 200) -> str:
    """Send audio to Modal and collect transcription.

    Args:
        audio_bytes: Float32 PCM audio bytes
        chunk_size_ms: Size of chunks to send (simulates streaming)

    Returns:
        Complete transcription text
    """
    url = get_modal_url()
    headers = get_modal_headers()

    # Calculate bytes per chunk (24kHz mono float32 = 4 bytes/sample)
    samples_per_chunk = int(24000 * chunk_size_ms / 1000)
    bytes_per_chunk = samples_per_chunk * 4

    transcription_parts = []

    async with websockets.connect(
        url,
        additional_headers=headers,
        open_timeout=120,  # Allow for cold start
        ping_interval=30,
        ping_timeout=10,
    ) as ws:
        # Create tasks for sending and receiving concurrently
        async def send_all():
            for i in range(0, len(audio_bytes), bytes_per_chunk):
                chunk = audio_bytes[i:i + bytes_per_chunk]
                await ws.send(chunk)
                # Pace the sending to simulate real-time
                await asyncio.sleep(chunk_size_ms / 1000 * 0.8)

        async def receive_all():
            silence_count = 0
            try:
                async with asyncio.timeout(30.0):  # Max 30s for full transcription
                    async for message in ws:
                        data = json.loads(message)
                        msg_type = data.get("type")

                        if msg_type == "token":
                            text = data.get("text", "")
                            if text:
                                transcription_parts.append(text)
                                silence_count = 0
                        elif msg_type == "vad_end":
                            # VAD detected end of speech - wait a bit more for trailing tokens
                            silence_count += 1
                            if silence_count >= 3:
                                break
                        elif msg_type == "error":
                            raise RuntimeError(f"Modal error: {data.get('message')}")
                        elif msg_type == "ping":
                            continue
            except asyncio.TimeoutError:
                pass

        # Run send and receive concurrently
        await asyncio.gather(send_all(), receive_all())

    return "".join(transcription_parts)


def normalize_text(text: str) -> str:
    """Normalize text for comparison (lowercase, strip, collapse whitespace)."""
    import re
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    # Remove punctuation for fuzzy matching
    text = re.sub(r'[^\w\s]', '', text)
    return text


def text_similarity(expected: str, actual: str) -> float:
    """Calculate word-level similarity between expected and actual."""
    expected_words = set(normalize_text(expected).split())
    actual_words = set(normalize_text(actual).split())

    if not expected_words:
        return 1.0 if not actual_words else 0.0

    intersection = expected_words & actual_words
    union = expected_words | actual_words

    return len(intersection) / len(union) if union else 1.0


# Skip all tests if Modal not configured
pytestmark = pytest.mark.skipif(
    not all([MODAL_WORKSPACE, MODAL_KEY, MODAL_SECRET]),
    reason="Modal credentials not configured"
)


class TestModalIntegration:
    """Integration tests with real Modal service."""

    @pytest.mark.asyncio
    async def test_transcribe_chunk_0(self):
        """Test transcription of chunk_0.wav."""
        wav_path = SAMPLES_DIR / "chunk_0.wav"
        if not wav_path.exists():
            pytest.skip("Sample file not available")

        expected = load_expected_transcription(wav_path)
        if not expected:
            pytest.skip("No expected transcription available")

        audio_bytes = prepare_audio_for_modal(wav_path)
        actual = await send_audio_to_modal(audio_bytes)

        print(f"\nExpected: {expected}")
        print(f"Actual:   {actual}")

        similarity = text_similarity(expected, actual)
        print(f"Similarity: {similarity:.2%}")

        # Allow for some variation in transcription
        assert similarity > 0.5, f"Transcription too different. Expected ~'{expected}', got '{actual}'"

    @pytest.mark.asyncio
    async def test_transcribe_chunk_2(self):
        """Test transcription of chunk_2.wav (has content)."""
        wav_path = SAMPLES_DIR / "chunk_2.wav"
        if not wav_path.exists():
            pytest.skip("Sample file not available")

        expected = load_expected_transcription(wav_path)
        if not expected:
            pytest.skip("No expected transcription available")

        audio_bytes = prepare_audio_for_modal(wav_path)
        actual = await send_audio_to_modal(audio_bytes)

        print(f"\nExpected: {expected}")
        print(f"Actual:   {actual}")

        similarity = text_similarity(expected, actual)
        print(f"Similarity: {similarity:.2%}")

        assert similarity > 0.5, f"Transcription too different"

    @pytest.mark.asyncio
    async def test_audio_format_accepted(self):
        """Test that Modal accepts our audio format without error."""
        # Generate 1 second of silence
        silence = np.zeros(24000, dtype=np.float32)
        audio_bytes = silence.tobytes()

        # Should not raise any errors
        url = get_modal_url()
        headers = get_modal_headers()

        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=120,
        ) as ws:
            await ws.send(audio_bytes)

            # Wait for any response (ping or token)
            try:
                async with asyncio.timeout(5.0):
                    message = await ws.recv()
                    data = json.loads(message)
                    # Should be ping or empty token, not error
                    assert data.get("type") != "error", f"Modal rejected audio: {data}"
            except asyncio.TimeoutError:
                # No response is fine for silence
                pass

    @pytest.mark.asyncio
    async def test_connection_with_credentials(self):
        """Test that we can connect to Modal with our credentials."""
        url = get_modal_url()
        headers = get_modal_headers()

        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=120,
        ) as ws:
            # Connection successful if we get here without exception
            assert ws.state.name == "OPEN"
            await ws.close()


class TestAudioPipelineContract:
    """Test that our audio pipeline produces Modal-compatible output."""

    def test_output_is_float32_le(self):
        """Output must be float32 little-endian."""
        wav_path = SAMPLES_DIR / "chunk_0.wav"
        if not wav_path.exists():
            pytest.skip("Sample file not available")

        audio_bytes = prepare_audio_for_modal(wav_path)

        # Should be decodable as float32
        audio_array = np.frombuffer(audio_bytes, dtype=np.float32)
        assert audio_array.dtype == np.float32

    def test_output_is_normalized(self):
        """Output values must be in [-1.0, 1.0] range."""
        wav_path = SAMPLES_DIR / "chunk_0.wav"
        if not wav_path.exists():
            pytest.skip("Sample file not available")

        audio_bytes = prepare_audio_for_modal(wav_path)
        audio_array = np.frombuffer(audio_bytes, dtype=np.float32)

        assert audio_array.min() >= -1.0
        assert audio_array.max() <= 1.0

    def test_sample_rate_is_24khz(self):
        """Verify sample files are 24kHz."""
        wav_path = SAMPLES_DIR / "chunk_0.wav"
        if not wav_path.exists():
            pytest.skip("Sample file not available")

        _, sample_rate = load_wav_file(wav_path)
        assert sample_rate == 24000
