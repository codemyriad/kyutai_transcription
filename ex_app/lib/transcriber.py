"""Modal-based transcriber using Kyutai STT."""

import asyncio
import logging
import struct
import io
import os
import time
from pathlib import Path
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


class OggOpusEncoder:
    """Encoder that produces Ogg Opus format using PyAV.

    This produces a proper Ogg container that sphn.read_opus_bytes() can decode.
    The receiver accumulates all bytes, so we only send new bytes each time.
    """

    def __init__(self, sample_rate: int = KYUTAI_SAMPLE_RATE, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._container = None
        self._stream = None
        self._output = None
        self._initialized = False
        self._pts = 0
        self._bytes_sent = 0  # Track how many bytes we've already returned

    def _ensure_encoder(self):
        """Lazily initialize the Ogg Opus encoder."""
        if self._initialized:
            return True

        try:
            import av

            # Create in-memory output
            self._output = io.BytesIO()
            self._container = av.open(self._output, mode='w', format='ogg')
            # Set layout which determines channels (channels is read-only in newer PyAV)
            layout = 'mono' if self.channels == 1 else 'stereo'
            self._stream = self._container.add_stream('libopus', rate=self.sample_rate, layout=layout)

            self._initialized = True
            logger.info(f"Ogg Opus encoder initialized: {self.sample_rate}Hz, {self.channels}ch")
            return True
        except ImportError as e:
            logger.warning(f"PyAV not available ({e}), cannot encode Ogg Opus")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize Ogg Opus encoder: {e}")
            return False

    def encode(self, pcm_data: np.ndarray) -> bytes:
        """Encode PCM data to Ogg Opus format.

        Args:
            pcm_data: PCM audio data as int16 numpy array

        Returns:
            New Ogg Opus encoded audio bytes (only bytes not yet returned)
        """
        if not self._ensure_encoder():
            # Fallback: send raw PCM
            return pcm_data.tobytes()

        try:
            import av

            # Convert int16 to the format PyAV expects
            frame = av.AudioFrame.from_ndarray(
                pcm_data.reshape(1, -1),  # Shape: (channels, samples)
                format='s16',
                layout='mono' if self.channels == 1 else 'stereo'
            )
            frame.sample_rate = self.sample_rate
            frame.pts = self._pts
            self._pts += len(pcm_data)

            # Encode frame
            for packet in self._stream.encode(frame):
                self._container.mux(packet)

            # Get only the NEW bytes (not yet sent)
            current_size = self._output.tell()
            if current_size > self._bytes_sent:
                self._output.seek(self._bytes_sent)
                new_data = self._output.read()
                self._bytes_sent = current_size
                return new_data
            return b""
        except Exception as e:
            logger.error(f"Error encoding audio: {e}")
            return pcm_data.tobytes()

    def flush(self) -> bytes:
        """Flush any remaining data and finalize the stream."""
        if not self._initialized or not self._container:
            return b""

        try:
            # Flush encoder
            for packet in self._stream.encode(None):
                self._container.mux(packet)

            # Get only new bytes
            current_size = self._output.tell()
            if current_size > self._bytes_sent:
                self._output.seek(self._bytes_sent)
                new_data = self._output.read()
                self._bytes_sent = current_size
                return new_data
            return b""
        except Exception as e:
            logger.error(f"Error flushing encoder: {e}")
            return b""


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
        self._encoder = OggOpusEncoder(KYUTAI_SAMPLE_RATE, 1)
        self._audio_buffer: list[np.ndarray] = []
        self._buffer_duration_ms = 0
        self._min_buffer_ms = 200  # Buffer 200ms before sending (reduce latency)

        # Transcript logging (for debugging)
        self._transcript_buffer: list[str] = []
        self._last_transcript_log = 0.0
        self._transcript_log_interval = 5.0  # Log every 5 seconds

        # Audio capture for debugging
        self._debug_audio_dir: Optional[Path] = None
        self._audio_frame_count = 0
        self._total_audio_bytes = 0

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
                    open_timeout=MODAL_CONNECT_TIMEOUT,
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

        # Create debug audio directory
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._debug_audio_dir = Path(f"/tmp/audio_debug/{timestamp}_{self.session_id}")
        self._debug_audio_dir.mkdir(parents=True, exist_ok=True)
        # Write metadata file for playback (WebRTC typically delivers stereo)
        metadata_file = self._debug_audio_dir / "README.txt"
        with open(metadata_file, "w") as f:
            f.write(f"Audio capture for session: {self.session_id}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Format: Raw PCM, 16-bit signed little-endian\n")
            f.write(f"Sample rate: {WEBRTC_SAMPLE_RATE} Hz\n")
            f.write(f"Channels: 2 (stereo, WebRTC default)\n")
            f.write(f"\nTo play with ffplay:\n")
            f.write(f"  ffplay -f s16le -ar {WEBRTC_SAMPLE_RATE} -ac 2 audio_raw.pcm\n")
            f.write(f"\nTo convert to WAV:\n")
            f.write(f"  ffmpeg -f s16le -ar {WEBRTC_SAMPLE_RATE} -ac 2 -i audio_raw.pcm audio.wav\n")
        logger.info(f"Saving debug audio to {self._debug_audio_dir}")

        # Connect to Modal first (this can take time for cold start)
        await self.connect()

        # Now start the audio stream - don't queue audio before Modal is ready
        await audio_stream.start()

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

        # Save raw audio for debugging
        if self._debug_audio_dir:
            self._audio_frame_count += 1
            self._total_audio_bytes += len(frame_data)
            # Save every frame to a file (append mode for efficiency)
            raw_file = self._debug_audio_dir / "audio_raw.pcm"
            with open(raw_file, "ab") as f:
                f.write(frame_data)
            # Log progress every 100 frames
            if self._audio_frame_count % 100 == 0:
                logger.info(
                    f"Audio capture: {self._audio_frame_count} frames, "
                    f"{self._total_audio_bytes / 1024:.1f} KB total"
                )

        # Convert bytes to numpy array (assuming 16-bit PCM)
        audio = np.frombuffer(frame_data, dtype=np.int16)

        # Convert stereo to mono (WebRTC typically delivers stereo)
        # Interleaved stereo: L R L R L R... -> average channels
        if len(audio) % 2 == 0:
            audio = audio.reshape(-1, 2).mean(axis=1).astype(np.int16)

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

            logger.info(
                f"Sent {self._buffer_duration_ms:.0f}ms of audio ({len(encoded)} bytes) to Modal"
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
            message_count = 0
            async for message in self._ws:
                if not self._running:
                    break

                message_count += 1
                if message_count <= 5:
                    logger.info(f"Received message {message_count} from Modal: {message[:200] if len(message) > 200 else message}")

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
                if text:
                    self._transcript_buffer.append(text)
                    # Log accumulated transcript every N seconds
                    import time
                    now = time.time()
                    if now - self._last_transcript_log >= self._transcript_log_interval:
                        transcript = "".join(self._transcript_buffer)
                        if transcript.strip():
                            logger.info(f"Transcript: {transcript}")
                        self._transcript_buffer = []
                        self._last_transcript_log = now
                return TranscriptionResult(text=text, is_final=False)
            elif msg_type == "vad_end":
                # Log any remaining transcript on VAD end
                if self._transcript_buffer:
                    transcript = "".join(self._transcript_buffer)
                    if transcript.strip():
                        logger.info(f"Transcript (final): {transcript}")
                    self._transcript_buffer = []
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

        # Log audio capture summary
        if self._debug_audio_dir and self._audio_frame_count > 0:
            # Stereo 16-bit = 4 bytes per sample (2 channels × 2 bytes)
            duration_sec = self._total_audio_bytes / (WEBRTC_SAMPLE_RATE * 4)
            logger.info(
                f"Audio capture complete: {self._audio_frame_count} frames, "
                f"{self._total_audio_bytes / 1024:.1f} KB, ~{duration_sec:.1f}s of audio. "
                f"Saved to {self._debug_audio_dir}"
            )

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
