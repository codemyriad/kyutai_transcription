"""Transcription session - orchestrates audio processing and Modal communication.

This is the thin orchestration layer that glues together:
- Audio processing (pure functions)
- Modal protocol (message parsing)
- Modal transport (WebSocket I/O)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Optional, Protocol

from ..audio.processing import process_webrtc_frame_for_modal
from ..protocols.modal import ModalConfig, ModalMessage, ModalMessageType
from ..transport.modal_client import ModalSTTClient

logger = logging.getLogger(__name__)


class AudioFrameSource(Protocol):
    """Protocol for audio frame sources (e.g., WebRTC track)."""

    async def get_frame(self) -> Optional[bytes]:
        """Get next audio frame. Returns None when stream ends."""
        ...

    async def start(self) -> None:
        """Start the audio source."""
        ...

    async def stop(self) -> None:
        """Stop the audio source."""
        ...


@dataclass
class TranscriptionResult:
    """A transcription result from the session."""

    text: str
    is_final: bool
    speaker_id: str = ""


@dataclass
class TranscriptionSession:
    """Manages a transcription session for one speaker.

    Handles:
    - Connecting to Modal
    - Processing audio frames from WebRTC
    - Streaming results back

    Usage:
        session = TranscriptionSession(
            session_id="speaker-123",
            config=ModalConfig.from_env(),
        )

        async for result in session.run(audio_source):
            print(result.text)
    """

    session_id: str
    config: ModalConfig
    language: str = "en"

    # Audio processing settings (WebRTC -> Modal)
    source_sample_rate: int = 48000
    target_sample_rate: int = 24000
    is_stereo: bool = True

    # Buffering settings
    min_buffer_ms: int = 200

    # State
    _running: bool = field(default=False, init=False)
    _audio_buffer: list[bytes] = field(default_factory=list, init=False)
    _buffer_duration_ms: float = field(default=0.0, init=False)

    async def run(
        self,
        audio_source: AudioFrameSource,
        on_result: Optional[Callable[[TranscriptionResult], None]] = None,
    ) -> AsyncGenerator[TranscriptionResult, None]:
        """Run the transcription session.

        Args:
            audio_source: Source of audio frames (e.g., AudioStream)
            on_result: Optional callback for each result

        Yields:
            TranscriptionResult objects as they arrive
        """
        if self._running:
            logger.warning(f"Session {self.session_id} already running")
            return

        self._running = True
        logger.info(f"Starting transcription session {self.session_id}")

        client = ModalSTTClient(self.config)
        accumulated_text = ""

        try:
            async with client.connect() as stream:
                # Start audio source after Modal connects (avoid queue buildup during cold start)
                await audio_source.start()

                # Create tasks for sending and receiving
                send_task = asyncio.create_task(
                    self._send_audio_loop(audio_source, stream.send)
                )

                try:
                    async for msg in stream.receive:
                        result = self._process_message(msg, accumulated_text)
                        if result:
                            accumulated_text = self._update_accumulated(
                                msg, accumulated_text
                            )
                            if on_result:
                                on_result(result)
                            yield result

                            # Reset accumulated text on final
                            if result.is_final:
                                accumulated_text = ""
                finally:
                    send_task.cancel()
                    try:
                        await send_task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            logger.error(f"Session {self.session_id} error: {e}")
            raise
        finally:
            self._running = False
            await audio_source.stop()
            logger.info(f"Transcription session {self.session_id} ended")

    async def _send_audio_loop(self, audio_source: AudioFrameSource, sender) -> None:
        """Send processed audio to Modal."""
        try:
            while self._running:
                frame = await audio_source.get_frame()
                if frame is None:
                    logger.info(f"Audio source ended for {self.session_id}")
                    break

                # Process frame and buffer
                processed = process_webrtc_frame_for_modal(
                    frame,
                    source_rate=self.source_sample_rate,
                    target_rate=self.target_sample_rate,
                    is_stereo=self.is_stereo,
                )

                self._audio_buffer.append(processed)
                # Approximate duration (4 bytes per float32 sample at target rate)
                samples = len(processed) // 4
                self._buffer_duration_ms += (samples / self.target_sample_rate) * 1000

                # Flush when buffer is full
                if self._buffer_duration_ms >= self.min_buffer_ms:
                    await self._flush_buffer(sender)

        except asyncio.CancelledError:
            pass
        finally:
            # Flush remaining buffer
            await self._flush_buffer(sender)

    async def _flush_buffer(self, sender) -> None:
        """Send buffered audio to Modal."""
        if not self._audio_buffer:
            return

        combined = b"".join(self._audio_buffer)
        await sender.send(combined)

        self._audio_buffer = []
        self._buffer_duration_ms = 0

    def _process_message(
        self, msg: ModalMessage, accumulated_text: str
    ) -> Optional[TranscriptionResult]:
        """Process a Modal message and return a result if appropriate."""
        if msg.type == ModalMessageType.TOKEN:
            new_text = accumulated_text + msg.text
            # Emit partial results periodically
            if len(new_text) > 50:
                return TranscriptionResult(
                    text=new_text,
                    is_final=False,
                    speaker_id=self.session_id,
                )
            return None

        elif msg.type == ModalMessageType.VAD_END:
            if accumulated_text.strip():
                return TranscriptionResult(
                    text=accumulated_text.strip(),
                    is_final=True,
                    speaker_id=self.session_id,
                )
            return None

        elif msg.type == ModalMessageType.ERROR:
            logger.error(f"Modal error in session {self.session_id}: {msg.error_message}")
            return None

        return None

    def _update_accumulated(self, msg: ModalMessage, accumulated: str) -> str:
        """Update accumulated text based on message."""
        if msg.type == ModalMessageType.TOKEN:
            return accumulated + msg.text
        elif msg.type == ModalMessageType.VAD_END:
            return ""  # Reset on VAD end
        return accumulated

    def set_language(self, language: str) -> None:
        """Set the transcription language."""
        self.language = language
        logger.info(f"Language set to {language} for session {self.session_id}")


async def transcribe_file(
    wav_path: str,
    config: Optional[ModalConfig] = None,
) -> str:
    """Convenience function to transcribe a WAV file.

    Args:
        wav_path: Path to WAV file
        config: Modal config (uses env vars if not provided)

    Returns:
        Complete transcription text
    """
    from pathlib import Path

    from ..audio.processing import load_wav_file, resample, int16_to_float32

    if config is None:
        config = ModalConfig.from_env()

    # Load and prepare audio
    audio, sample_rate = load_wav_file(Path(wav_path))
    if sample_rate != 24000:
        audio = resample(audio, sample_rate, 24000)
    audio_float = int16_to_float32(audio)
    audio_bytes = audio_float.tobytes()

    # Send to Modal
    import websockets
    import json

    transcription_parts = []

    async with websockets.connect(
        config.url,
        additional_headers=config.headers,
        open_timeout=120,
    ) as ws:
        # Send in chunks
        chunk_size = 24000 * 4 // 5  # 200ms chunks
        for i in range(0, len(audio_bytes), chunk_size):
            await ws.send(audio_bytes[i:i + chunk_size])
            await asyncio.sleep(0.1)

        # Receive results
        try:
            async with asyncio.timeout(30.0):
                async for message in ws:
                    data = json.loads(message)
                    if data.get("type") == "token":
                        transcription_parts.append(data.get("text", ""))
                    elif data.get("type") == "vad_end":
                        break
        except asyncio.TimeoutError:
            pass

    return "".join(transcription_parts)
