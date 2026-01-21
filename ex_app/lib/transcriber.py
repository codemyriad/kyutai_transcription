"""Modal-based transcriber using Kyutai STT."""

import asyncio
import logging
import struct
import io
from typing import Optional, AsyncGenerator, Protocol
from dataclasses import dataclass

import numpy as np
import websockets
from websockets.client import WebSocketClientProtocol

from .constants import (
    MODAL_WORKSPACE,
    MODAL_KEY,
    MODAL_SECRET,
    MODAL_STT_URL,
    MODAL_CONNECT_TIMEOUT,
    KYUTAI_SAMPLE_RATE,
    WEBRTC_SAMPLE_RATE,
)
from .livetypes import Transcript, ModalConnectionError, TranscriptionError

logger = logging.getLogger(__name__)


class AudioStream(Protocol):
    """Protocol for audio streams."""

    async def get_frame(self) -> Optional[bytes]:
        """Get the next audio frame. Returns None when stream ends."""
        ...


@dataclass
class TranscriptionResult:
    """Result from transcription service."""

    text: str
    is_final: bool
    is_vad_end: bool = False


class OpusEncoder:
    """Simple Opus encoder wrapper using the opuslib library."""

    def __init__(self, sample_rate: int = KYUTAI_SAMPLE_RATE, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._encoder = None
        self._buffer = io.BytesIO()

    def _ensure_encoder(self):
        """Lazily initialize encoder."""
        if self._encoder is None:
            try:
                import opuslib

                self._encoder = opuslib.Encoder(
                    self.sample_rate,
                    self.channels,
                    opuslib.APPLICATION_VOIP,
                )
            except ImportError:
                logger.warning("opuslib not available, using raw audio fallback")
                self._encoder = False

    def encode(self, pcm_data: np.ndarray) -> bytes:
        """Encode PCM data to Opus format.

        Args:
            pcm_data: PCM audio data as int16 numpy array

        Returns:
            Opus-encoded audio bytes
        """
        self._ensure_encoder()

        if self._encoder is False:
            # Fallback: send raw PCM (server may handle it)
            return pcm_data.tobytes()

        # Opus frame sizes: 2.5, 5, 10, 20, 40, 60 ms
        # At 24kHz, 40ms = 960 samples
        frame_size = 960
        encoded_frames = []

        for i in range(0, len(pcm_data), frame_size):
            frame = pcm_data[i : i + frame_size]
            if len(frame) < frame_size:
                # Pad with zeros if needed
                frame = np.pad(frame, (0, frame_size - len(frame)))

            encoded = self._encoder.encode(frame.tobytes(), frame_size)
            encoded_frames.append(encoded)

        return b"".join(encoded_frames)


class AudioResampler:
    """Resample audio between sample rates."""

    def __init__(self, source_rate: int, target_rate: int):
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.ratio = target_rate / source_rate

    def resample(self, audio: np.ndarray) -> np.ndarray:
        """Resample audio data.

        Args:
            audio: Input audio as numpy array

        Returns:
            Resampled audio
        """
        if self.source_rate == self.target_rate:
            return audio

        # Use scipy for high-quality resampling if available
        try:
            from scipy import signal

            num_samples = int(len(audio) * self.ratio)
            return signal.resample(audio, num_samples).astype(np.int16)
        except ImportError:
            # Simple linear interpolation fallback
            indices = np.arange(0, len(audio), 1 / self.ratio)
            indices = indices[indices < len(audio) - 1].astype(int)
            return audio[indices]


class ModalTranscriber:
    """Transcriber that sends audio to Kyutai STT on Modal."""

    def __init__(
        self,
        session_id: str,
        language: str = "en",
        workspace: Optional[str] = None,
        modal_key: Optional[str] = None,
        modal_secret: Optional[str] = None,
    ):
        """Initialize the Modal transcriber.

        Args:
            session_id: Unique session identifier for this speaker
            language: Language code (en or fr)
            workspace: Modal workspace (uses env var if not provided)
            modal_key: Modal API key (uses env var if not provided)
            modal_secret: Modal API secret (uses env var if not provided)
        """
        self.session_id = session_id
        self.language = language
        self.workspace = workspace or MODAL_WORKSPACE
        self.modal_key = modal_key or MODAL_KEY
        self.modal_secret = modal_secret or MODAL_SECRET

        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._result_queue: asyncio.Queue[TranscriptionResult] = asyncio.Queue()

        # Audio processing
        self._resampler = AudioResampler(WEBRTC_SAMPLE_RATE, KYUTAI_SAMPLE_RATE)
        self._encoder = OpusEncoder(KYUTAI_SAMPLE_RATE, 1)
        self._audio_buffer: list[np.ndarray] = []
        self._buffer_duration_ms = 0
        self._min_buffer_ms = 1000  # Buffer 1 second before sending

        logger.info(
            f"Created ModalTranscriber for session {session_id}, language={language}"
        )

    @property
    def url(self) -> str:
        """Get the Modal WebSocket URL."""
        return f"wss://{self.workspace}--kyutai-stt-kyutaisttservice-serve.modal.run/v1/stream"

    def _get_headers(self) -> dict[str, str]:
        """Get authentication headers for Modal."""
        return {
            "Modal-Key": self.modal_key,
            "Modal-Secret": self.modal_secret,
        }

    async def connect(self) -> None:
        """Connect to the Modal transcription service."""
        if not self.workspace or not self.modal_key or not self.modal_secret:
            raise ModalConnectionError(
                "Modal credentials not configured. Set MODAL_WORKSPACE, MODAL_KEY, and MODAL_SECRET."
            )

        try:
            logger.info(f"Connecting to Modal STT at {self.url}")
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.url,
                    additional_headers=self._get_headers(),
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=None,
                ),
                timeout=MODAL_CONNECT_TIMEOUT,
            )
            logger.info("Connected to Modal STT")
        except asyncio.TimeoutError:
            raise ModalConnectionError(
                f"Timeout connecting to Modal STT after {MODAL_CONNECT_TIMEOUT}s"
            )
        except Exception as e:
            raise ModalConnectionError(f"Failed to connect to Modal STT: {e}")

    async def start(self, audio_stream: AudioStream) -> None:
        """Start transcription for the given audio stream.

        Args:
            audio_stream: Audio stream to transcribe
        """
        if self._running:
            logger.warning("Transcriber already running")
            return

        await self.connect()
        self._running = True

        # Start send and receive tasks
        self._send_task = asyncio.create_task(self._send_audio_loop(audio_stream))
        self._recv_task = asyncio.create_task(self._receive_results_loop())

        logger.info(f"Started transcription for session {self.session_id}")

    async def _send_audio_loop(self, audio_stream: AudioStream) -> None:
        """Loop to send audio to Modal."""
        try:
            while self._running:
                frame = await audio_stream.get_frame()
                if frame is None:
                    logger.info("Audio stream ended")
                    break

                await self._process_and_send_audio(frame)
        except asyncio.CancelledError:
            logger.info("Audio send loop cancelled")
        except Exception as e:
            logger.error(f"Error in audio send loop: {e}")
        finally:
            # Send any remaining buffered audio
            await self._flush_buffer()

    async def _process_and_send_audio(self, frame_data: bytes) -> None:
        """Process audio frame and send to Modal when buffer is full.

        Args:
            frame_data: Raw PCM audio data (typically 48kHz)
        """
        if not self._ws:
            return

        # Convert bytes to numpy array (assuming 16-bit PCM)
        audio = np.frombuffer(frame_data, dtype=np.int16)

        # Resample from WebRTC rate to Kyutai rate
        resampled = self._resampler.resample(audio)

        # Add to buffer
        self._audio_buffer.append(resampled)
        self._buffer_duration_ms += len(resampled) * 1000 / KYUTAI_SAMPLE_RATE

        # Send when we have enough audio
        if self._buffer_duration_ms >= self._min_buffer_ms:
            await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Flush audio buffer to Modal."""
        if not self._audio_buffer or not self._ws:
            return

        try:
            # Concatenate all buffered audio
            combined = np.concatenate(self._audio_buffer)

            # Encode to Opus
            encoded = self._encoder.encode(combined)

            # Send to Modal
            await self._ws.send(encoded)

            logger.debug(
                f"Sent {self._buffer_duration_ms:.0f}ms of audio ({len(encoded)} bytes)"
            )

            # Clear buffer
            self._audio_buffer = []
            self._buffer_duration_ms = 0
        except Exception as e:
            logger.error(f"Error sending audio: {e}")

    async def _receive_results_loop(self) -> None:
        """Loop to receive transcription results from Modal."""
        if not self._ws:
            return

        try:
            async for message in self._ws:
                if not self._running:
                    break

                result = self._parse_result(message)
                if result:
                    await self._result_queue.put(result)
        except asyncio.CancelledError:
            logger.info("Results receive loop cancelled")
        except websockets.ConnectionClosed:
            logger.info("Modal connection closed")
        except Exception as e:
            logger.error(f"Error in receive loop: {e}")

    def _parse_result(self, message: str) -> Optional[TranscriptionResult]:
        """Parse a result message from Modal.

        Args:
            message: JSON message from Modal

        Returns:
            Parsed TranscriptionResult or None
        """
        import json

        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "token":
                text = data.get("text", "")
                return TranscriptionResult(text=text, is_final=False)
            elif msg_type == "vad_end":
                return TranscriptionResult(text="", is_final=True, is_vad_end=True)
            elif msg_type == "error":
                logger.error(f"Modal error: {data.get('message')}")
                return None
            elif msg_type == "ping":
                # Ignore keepalive pings
                return None
            else:
                logger.debug(f"Unknown message type: {msg_type}")
                return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Modal message: {e}")
            return None

    async def get_results(self) -> AsyncGenerator[TranscriptionResult, None]:
        """Async generator that yields transcription results.

        Yields:
            TranscriptionResult objects as they arrive
        """
        while self._running or not self._result_queue.empty():
            try:
                result = await asyncio.wait_for(
                    self._result_queue.get(), timeout=1.0
                )
                yield result
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        """Stop transcription and clean up."""
        logger.info(f"Stopping transcriber for session {self.session_id}")
        self._running = False

        # Cancel tasks
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass

        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        # Close WebSocket
        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info(f"Transcriber stopped for session {self.session_id}")

    def set_language(self, language: str) -> None:
        """Set the transcription language.

        Note: Kyutai's model handles en/fr automatically,
        but this may be used for future language-specific processing.

        Args:
            language: Language code
        """
        self.language = language
        logger.info(f"Language set to {language} for session {self.session_id}")


class TranscriberFactory:
    """Factory for creating transcriber instances."""

    @staticmethod
    def create(
        session_id: str,
        language: str = "en",
        **kwargs,
    ) -> ModalTranscriber:
        """Create a new transcriber instance.

        Args:
            session_id: Unique session identifier
            language: Language code
            **kwargs: Additional arguments for the transcriber

        Returns:
            New ModalTranscriber instance
        """
        return ModalTranscriber(session_id=session_id, language=language, **kwargs)
