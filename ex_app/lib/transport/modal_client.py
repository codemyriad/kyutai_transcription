"""Modal STT WebSocket client.

This module handles the WebSocket connection to Modal's Kyutai STT service.
It separates I/O concerns from the business logic.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import AsyncGenerator, Optional, Protocol

import websockets
from websockets import ClientConnection

from ..protocols.modal import (
    ModalConfig,
    ModalMessage,
    ModalMessageType,
    parse_modal_message,
)

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionStream:
    """Result of starting a transcription session.

    Contains async generators for sending audio and receiving results.
    """

    send: "AudioSender"
    receive: AsyncGenerator[ModalMessage, None]


class AudioSender:
    """Sends audio chunks to Modal over WebSocket."""

    def __init__(self, ws: ClientConnection):
        self._ws = ws
        self._closed = False

    async def send(self, audio_bytes: bytes) -> bool:
        """Send audio chunk to Modal.

        Args:
            audio_bytes: Float32 LE PCM audio bytes

        Returns:
            True if sent successfully, False if connection closed
        """
        if self._closed:
            return False

        try:
            await self._ws.send(audio_bytes)
            return True
        except websockets.ConnectionClosed:
            self._closed = True
            return False

    async def close(self) -> None:
        """Close the sender (signal end of audio)."""
        self._closed = True


class ModalSTTClient:
    """Client for Modal's Kyutai STT service.

    Handles WebSocket connection lifecycle and message streaming.

    Usage:
        config = ModalConfig.from_env()
        client = ModalSTTClient(config)

        async with client.connect() as stream:
            # Send audio in a task
            for chunk in audio_chunks:
                await stream.send.send(chunk)

            # Receive results
            async for msg in stream.receive:
                if msg.is_token:
                    print(msg.text)
    """

    # Default timeouts
    CONNECT_TIMEOUT = 120  # Allow for Modal cold start
    PING_INTERVAL = 30
    PING_TIMEOUT = 10

    def __init__(
        self,
        config: ModalConfig,
        connect_timeout: float = CONNECT_TIMEOUT,
    ):
        """Initialize the client.

        Args:
            config: Modal configuration with credentials
            connect_timeout: Timeout for initial connection
        """
        self.config = config
        self.connect_timeout = connect_timeout
        self._ws: Optional[ClientConnection] = None

    def connect(self) -> "ModalConnection":
        """Connect to Modal STT service.

        Returns:
            Context manager that yields TranscriptionStream

        Raises:
            ModalConnectionError: If connection fails

        Usage:
            async with client.connect() as stream:
                ...
        """
        return ModalConnection(self)

    async def _establish_connection(self) -> ClientConnection:
        """Establish WebSocket connection.

        Internal method - use connect() for proper lifecycle management.
        """
        if not self.config.is_configured():
            raise ValueError(
                "Modal not configured. Set MODAL_WORKSPACE, MODAL_KEY, and MODAL_SECRET."
            )

        logger.info(f"Connecting to Modal STT at {self.config.url}")

        ws = await asyncio.wait_for(
            websockets.connect(
                self.config.url,
                additional_headers=self.config.headers,
                open_timeout=self.connect_timeout,
                ping_interval=self.PING_INTERVAL,
                ping_timeout=self.PING_TIMEOUT,
                max_size=None,
            ),
            timeout=self.connect_timeout,
        )

        logger.info("Connected to Modal STT")
        return ws


class ModalConnection:
    """Context manager for Modal STT connection.

    Use with 'async with' to ensure proper cleanup.
    """

    def __init__(self, client: ModalSTTClient):
        self._client = client
        self._ws: Optional[ClientConnection] = None
        self._sender: Optional[AudioSender] = None

    async def __aenter__(self) -> TranscriptionStream:
        """Connect and return transcription stream."""
        self._ws = await self._client._establish_connection()
        self._sender = AudioSender(self._ws)

        return TranscriptionStream(
            send=self._sender,
            receive=self._receive_messages(),
        )

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Clean up connection."""
        if self._sender:
            await self._sender.close()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _receive_messages(self) -> AsyncGenerator[ModalMessage, None]:
        """Receive and parse messages from Modal.

        Yields:
            Parsed ModalMessage objects
        """
        if not self._ws:
            return

        try:
            async for raw_message in self._ws:
                msg = parse_modal_message(raw_message)
                yield msg

                # Stop on error
                if msg.is_error:
                    logger.error(f"Modal error: {msg.error_message}")
                    break

        except websockets.ConnectionClosed as e:
            logger.info(f"Modal connection closed: {e}")
        except Exception as e:
            logger.error(f"Error receiving Modal messages: {e}")


async def transcribe_audio(
    config: ModalConfig,
    audio_chunks: AsyncGenerator[bytes, None],
    chunk_delay: float = 0.0,
) -> AsyncGenerator[str, None]:
    """High-level function to transcribe streaming audio.

    Args:
        config: Modal configuration
        audio_chunks: Async generator yielding float32 PCM audio chunks
        chunk_delay: Delay between chunks (for rate limiting)

    Yields:
        Transcription text tokens as they arrive

    Example:
        async for text in transcribe_audio(config, audio_stream()):
            print(text, end="", flush=True)
    """
    client = ModalSTTClient(config)

    async with client.connect() as stream:
        # Start receiving task
        results: list[str] = []
        receive_done = asyncio.Event()

        async def receive_results():
            async for msg in stream.receive:
                if msg.is_token and msg.has_text:
                    results.append(msg.text)
                elif msg.is_vad_end:
                    pass  # Continue receiving
            receive_done.set()

        receive_task = asyncio.create_task(receive_results())

        try:
            # Send all audio
            async for chunk in audio_chunks:
                await stream.send.send(chunk)
                if chunk_delay > 0:
                    await asyncio.sleep(chunk_delay)

            # Wait for results with timeout
            try:
                await asyncio.wait_for(receive_done.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass

        finally:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass

        # Yield collected results
        for text in results:
            yield text
