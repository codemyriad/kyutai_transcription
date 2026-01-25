"""Modal-based transcriber using Kyutai STT."""

import asyncio
import contextlib
import gc
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, Optional, Protocol

import numpy as np
import websockets
from websockets.client import WebSocketClientProtocol

from .constants import (
    KYUTAI_SAMPLE_RATE,
    MODAL_CONNECT_TIMEOUT,
    MODAL_KEY,
    MODAL_SECRET,
    MODAL_STALE_TIMEOUT,
    MODAL_STT_HOST_SUFFIX,
    MODAL_WORKSPACE,
    WEBRTC_SAMPLE_RATE,
)
from .livetypes import ModalConnectionError

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


class RawPCMEncoder:
    """Simple encoder that converts int16 PCM to float32 for Modal.

    Format: 32-bit float little-endian, mono, at the target sample rate.
    Modal server expects float32 in range [-1.0, 1.0].
    """

    def __init__(self, sample_rate: int = KYUTAI_SAMPLE_RATE, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        logger.info(f"Raw PCM encoder: {self.sample_rate}Hz, {self.channels}ch, float32le")

    def encode(self, pcm_data: np.ndarray) -> bytes:
        """Convert int16 PCM to float32 and return as bytes.

        Args:
            pcm_data: PCM audio data as int16 numpy array

        Returns:
            Raw PCM bytes (float32 LE format, range [-1.0, 1.0])
        """
        # Convert int16 (-32768 to 32767) to float32 (-1.0 to 1.0)
        float_data = pcm_data.astype(np.float32) / 32768.0
        return float_data.tobytes()

    def flush(self) -> bytes:
        """Nothing to flush for raw PCM."""
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
        self._encoder = RawPCMEncoder(KYUTAI_SAMPLE_RATE, 1)
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
        self._first_audio_sent = False  # Track if we've logged first audio send

        # Stale connection detection
        self._last_audio_sent_time = 0.0
        self._last_transcript_time = 0.0
        self._stale_warned = False

        logger.info(
            f"Created ModalTranscriber for session {session_id}, language={language}"
        )

    @property
    def url(self) -> str:
        """Get the Modal WebSocket URL."""
        return f"wss://{self.workspace}--{MODAL_STT_HOST_SUFFIX}/v1/stream"

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

        # Create debug audio directory only if explicitly enabled
        if os.environ.get("SAVE_DEBUG_AUDIO"):
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self._debug_audio_dir = Path(f"/tmp/audio_debug/{timestamp}_{self.session_id}")
            self._debug_audio_dir.mkdir(parents=True, exist_ok=True)
            # Write metadata file for playback (WebRTC typically delivers stereo)
            metadata_file = self._debug_audio_dir / "README.txt"
            with open(metadata_file, "w") as f:
                f.write(f"Audio capture for session: {self.session_id}\n")
                f.write(f"Timestamp: {timestamp}\n")
                f.write("Format: Raw PCM, 16-bit signed little-endian\n")
                f.write(f"Sample rate: {WEBRTC_SAMPLE_RATE} Hz\n")
                f.write("Channels: 2 (stereo, WebRTC default)\n")
                f.write("\nTo play with ffplay:\n")
                f.write(f"  ffplay -f s16le -ar {WEBRTC_SAMPLE_RATE} -ac 2 audio_raw.pcm\n")
                f.write("\nTo convert to WAV:\n")
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
                    self._running = False
                    break

                await self._process_and_send_audio(frame)

                # Check for stale connection (audio being sent but no transcripts)
                await self._check_stale_connection()
        except asyncio.CancelledError:
            logger.info("Audio send loop cancelled")
        except Exception as e:
            logger.error(f"Error in audio send loop: {e}")
            self._running = False
        finally:
            with contextlib.suppress(Exception):
                await audio_stream.stop()
            # Send any remaining buffered audio
            await self._flush_buffer()

    async def _check_stale_connection(self) -> None:
        """Check if connection appears stale (sending audio but no transcripts).

        If audio has been sent for MODAL_STALE_TIMEOUT seconds without receiving
        any transcripts, log a warning. This helps diagnose Modal issues.
        """
        now = time.time()

        # Only check if we've been sending audio for a while
        if self._last_audio_sent_time == 0 or (now - self._last_audio_sent_time) > 5:
            return

        # If we've never received a transcript and been sending for a while
        if self._last_transcript_time == 0 and self._first_audio_sent:
            time_since_first_send = now - self._last_audio_sent_time + (
                MODAL_STALE_TIMEOUT if self._stale_warned else 0
            )
            if time_since_first_send > MODAL_STALE_TIMEOUT and not self._stale_warned:
                logger.warning(
                    f"Stale connection detected: Sent audio for {MODAL_STALE_TIMEOUT}s "
                    f"but no transcripts received. Modal may be unresponsive."
                )
                self._stale_warned = True
        # If we were receiving transcripts but they stopped
        elif self._last_transcript_time > 0:
            time_since_transcript = now - self._last_transcript_time
            if time_since_transcript > MODAL_STALE_TIMEOUT and not self._stale_warned:
                logger.warning(
                    f"Stale connection detected: No transcripts for {time_since_transcript:.1f}s "
                    f"while audio is being sent. Modal may be unresponsive."
                )
                self._stale_warned = True

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
            # Log progress every 500 frames (~10 seconds)
            if self._audio_frame_count % 500 == 0:
                logger.debug(
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
            self._last_audio_sent_time = time.time()

            # Log first audio send at INFO level, rest at DEBUG
            if not self._first_audio_sent:
                logger.info(f"First audio sent to Modal ({len(encoded)} bytes)")
                self._first_audio_sent = True
            else:
                logger.debug(
                    f"Sent {self._buffer_duration_ms:.0f}ms of audio ({len(encoded)} bytes) to Modal"
                )

            # Clear buffer
            self._audio_buffer = []
            self._buffer_duration_ms = 0
        except websockets.ConnectionClosed as e:
            logger.warning(
                "Modal connection closed while sending audio (%s). Stopping transcriber.",
                e,
            )
            self._running = False
            self._ws = None
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
        except websockets.ConnectionClosed as e:
            logger.info("Modal connection closed: %s", e)
            self._running = False
            self._ws = None
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
                    self._last_transcript_time = time.time()
                    # Log accumulated transcript every N seconds
                    import time
                    now = time.time()
                    if now - self._last_transcript_log >= self._transcript_log_interval:
                        transcript = "".join(self._transcript_buffer)
                        if transcript.strip():
                            self._log_transcript(transcript)
                        self._transcript_buffer = []
                        self._last_transcript_log = now
                return TranscriptionResult(text=text, is_final=False)
            elif msg_type == "vad_end":
                # Log any remaining transcript on VAD end
                if self._transcript_buffer:
                    transcript = "".join(self._transcript_buffer)
                    if transcript.strip():
                        self._log_transcript(transcript, final=True)
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

        # Clear buffers and queues to release memory
        self._audio_buffer.clear()
        self._transcript_buffer.clear()
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        while not self._result_queue.empty():
            try:
                self._result_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Log audio capture summary and cleanup debug files
        if self._debug_audio_dir and self._audio_frame_count > 0:
            # Stereo 16-bit = 4 bytes per sample (2 channels × 2 bytes)
            duration_sec = self._total_audio_bytes / (WEBRTC_SAMPLE_RATE * 4)
            logger.info(
                f"Audio capture complete: {self._audio_frame_count} frames, "
                f"{self._total_audio_bytes / 1024:.1f} KB, ~{duration_sec:.1f}s of audio. "
                f"Saved to {self._debug_audio_dir}"
            )

        # Clean up debug audio files unless KEEP_DEBUG_AUDIO is set
        if self._debug_audio_dir and not os.environ.get("KEEP_DEBUG_AUDIO"):
            try:
                shutil.rmtree(self._debug_audio_dir)
                logger.debug(f"Cleaned up debug audio dir: {self._debug_audio_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up debug audio: {e}")
        self._debug_audio_dir = None

        # Force garbage collection to release numpy arrays and buffers
        gc.collect()

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

    def _log_transcript(self, transcript: str, final: bool = False) -> None:
        """Log transcript text with speaker context."""
        final_tag = " (final)" if final else ""
        logger.info(
            f"[speaker={self.session_id}]{final_tag} >>> TRANSCRIPT: {transcript}"
        )


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
