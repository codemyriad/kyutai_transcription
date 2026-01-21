"""Audio stream handling for WebRTC tracks."""

import asyncio
import logging
from typing import Optional
from aiortc.mediastreams import MediaStreamTrack

logger = logging.getLogger(__name__)


class AudioStream:
    """Wrapper for aiortc audio tracks that provides frame delivery."""

    def __init__(self, track: MediaStreamTrack):
        """Initialize the audio stream.

        Args:
            track: The aiortc audio track to wrap
        """
        self.track = track
        self._running = False
        self._frame_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=100)
        self._task: Optional[asyncio.Task] = None
        self._sample_rate: Optional[int] = None
        self._channels: Optional[int] = None

    @property
    def sample_rate(self) -> Optional[int]:
        """Get the audio sample rate."""
        return self._sample_rate

    @property
    def channels(self) -> Optional[int]:
        """Get the number of audio channels."""
        return self._channels

    async def start(self) -> None:
        """Start receiving frames from the track."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._frame_loop())
        logger.info("Audio stream started")

    async def _frame_loop(self) -> None:
        """Loop to receive frames from the track."""
        try:
            while self._running:
                try:
                    frame = await asyncio.wait_for(
                        self.track.recv(),
                        timeout=5.0,
                    )

                    # Extract frame info on first frame
                    if self._sample_rate is None:
                        self._sample_rate = frame.sample_rate
                        self._channels = len(frame.layout.channels)
                        logger.info(
                            f"Audio format: {self._sample_rate}Hz, {self._channels} channels"
                        )

                    # Convert frame to raw bytes (16-bit PCM)
                    # aiortc frames are already in int16 format
                    pcm_data = frame.to_ndarray().tobytes()

                    # Put in queue (non-blocking, drop if full)
                    try:
                        self._frame_queue.put_nowait(pcm_data)
                    except asyncio.QueueFull:
                        logger.warning("Audio frame queue full, dropping frame")

                except asyncio.TimeoutError:
                    # No frame received, continue waiting
                    continue
                except Exception as e:
                    if "Track ended" in str(e) or "Connection" in str(e):
                        logger.info("Audio track ended")
                        break
                    logger.error(f"Error receiving frame: {e}")
                    break
        finally:
            # Signal end of stream
            await self._frame_queue.put(None)
            self._running = False

    async def get_frame(self) -> Optional[bytes]:
        """Get the next audio frame.

        Returns:
            Raw PCM audio bytes, or None if stream has ended
        """
        return await self._frame_queue.get()

    async def stop(self) -> None:
        """Stop receiving frames."""
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        logger.info("Audio stream stopped")

    def __aiter__(self):
        """Async iterator interface."""
        return self

    async def __anext__(self) -> bytes:
        """Get next frame for async iteration."""
        frame = await self.get_frame()
        if frame is None:
            raise StopAsyncIteration
        return frame
