"""Memory watchdog to prevent runaway memory usage.

This module monitors memory usage and triggers shutdown if limits are exceeded.
It dynamically calculates expected memory based on active transcriber count.
"""

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from .service import Application

logger = logging.getLogger(__name__)

# Memory constants (in bytes)
MB = 1024 * 1024

# Base memory for Python + FastAPI + libraries (conservative estimate)
BASE_MEMORY_MB = 150

# Memory per active transcriber (WebRTC + resampler + buffers + Modal WebSocket)
MEMORY_PER_TRANSCRIBER_MB = 50

# Additional headroom percentage (20% leeway)
MEMORY_HEADROOM_PERCENT = 0.20

# Absolute maximum memory limit (fallback if no transcribers)
# Can be overridden with LT_MAX_MEMORY_MB environment variable
DEFAULT_MAX_MEMORY_MB = 512

# Check interval in seconds
CHECK_INTERVAL_SECONDS = 5

# Thresholds for warnings and actions (as fraction of calculated limit)
THRESHOLD_WARNING = 0.80
THRESHOLD_GRACEFUL_SHUTDOWN = 0.95
THRESHOLD_FORCE_EXIT = 1.0


def _get_current_rss_mb() -> float:
    """Get current RSS memory usage in MB.

    Returns:
        Current RSS memory in megabytes
    """
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # Format: "VmRSS:    12345 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024  # Convert kB to MB
    except (OSError, ValueError, IndexError):
        pass

    # Fallback: try resource module
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is in kilobytes on Linux
        return usage.ru_maxrss / 1024
    except (ImportError, AttributeError):
        pass

    return 0.0


class MemoryWatchdog:
    """Monitors memory usage and triggers shutdown if limits are exceeded."""

    def __init__(
        self,
        app_service: "Application",
        shutdown_callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Initialize the memory watchdog.

        Args:
            app_service: Application service to query for active transcribers
            shutdown_callback: Async function to call for graceful shutdown
        """
        self._app_service = app_service
        self._shutdown_callback = shutdown_callback
        self._task: asyncio.Task | None = None
        self._shutdown_triggered = False

        # Allow override via environment variable
        env_max = os.getenv("LT_MAX_MEMORY_MB")
        self._env_max_memory_mb = int(env_max) if env_max else None

    def _count_active_transcribers(self) -> int:
        """Count total active transcribers across all rooms.

        Returns:
            Total number of active transcribers
        """
        count = 0
        for client in self._app_service.clients.values():
            if not client.defunct.is_set():
                count += len(client.transcribers)
        return count

    def _calculate_memory_limit_mb(self) -> float:
        """Calculate dynamic memory limit based on active transcribers.

        Returns:
            Memory limit in megabytes
        """
        transcriber_count = self._count_active_transcribers()

        # Calculate expected memory
        expected_mb = BASE_MEMORY_MB + (transcriber_count * MEMORY_PER_TRANSCRIBER_MB)

        # Add headroom
        limit_mb = expected_mb * (1 + MEMORY_HEADROOM_PERCENT)

        # Apply environment override as ceiling if set
        if self._env_max_memory_mb:
            limit_mb = min(limit_mb, self._env_max_memory_mb)

        # Ensure minimum limit
        limit_mb = max(limit_mb, DEFAULT_MAX_MEMORY_MB)

        return limit_mb

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        logger.info(
            "Memory watchdog started",
            extra={
                "check_interval_seconds": CHECK_INTERVAL_SECONDS,
                "base_memory_mb": BASE_MEMORY_MB,
                "memory_per_transcriber_mb": MEMORY_PER_TRANSCRIBER_MB,
                "env_max_memory_mb": self._env_max_memory_mb,
            },
        )

        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                await self._check_memory()
            except asyncio.CancelledError:
                logger.debug("Memory watchdog cancelled")
                break
            except Exception as e:
                logger.exception(
                    "Error in memory watchdog loop",
                    exc_info=e,
                )

    async def _check_memory(self) -> None:
        """Check current memory usage against limits."""
        current_mb = _get_current_rss_mb()
        if current_mb == 0:
            # Could not determine memory usage
            return

        limit_mb = self._calculate_memory_limit_mb()
        transcriber_count = self._count_active_transcribers()
        usage_ratio = current_mb / limit_mb

        if usage_ratio >= THRESHOLD_FORCE_EXIT:
            logger.critical(
                "Memory limit exceeded, forcing exit",
                extra={
                    "current_mb": round(current_mb, 1),
                    "limit_mb": round(limit_mb, 1),
                    "usage_percent": round(usage_ratio * 100, 1),
                    "transcriber_count": transcriber_count,
                },
            )
            # Force exit - this is the last resort
            sys.exit(137)  # 128 + 9 (SIGKILL)

        if usage_ratio >= THRESHOLD_GRACEFUL_SHUTDOWN:
            if not self._shutdown_triggered:
                self._shutdown_triggered = True
                logger.error(
                    "Memory threshold exceeded, initiating graceful shutdown",
                    extra={
                        "current_mb": round(current_mb, 1),
                        "limit_mb": round(limit_mb, 1),
                        "usage_percent": round(usage_ratio * 100, 1),
                        "transcriber_count": transcriber_count,
                    },
                )
                # Trigger graceful shutdown
                try:
                    await asyncio.wait_for(self._shutdown_callback(), timeout=30)
                except asyncio.TimeoutError:
                    logger.error("Graceful shutdown timed out, forcing exit")
                    sys.exit(137)
                except Exception as e:
                    logger.exception("Error during graceful shutdown", exc_info=e)
                    sys.exit(137)

                # Exit after graceful shutdown
                sys.exit(137)
            return

        if usage_ratio >= THRESHOLD_WARNING:
            logger.warning(
                "Memory usage high",
                extra={
                    "current_mb": round(current_mb, 1),
                    "limit_mb": round(limit_mb, 1),
                    "usage_percent": round(usage_ratio * 100, 1),
                    "transcriber_count": transcriber_count,
                },
            )
            return

        # Normal operation - log at debug level periodically
        logger.debug(
            "Memory check OK",
            extra={
                "current_mb": round(current_mb, 1),
                "limit_mb": round(limit_mb, 1),
                "usage_percent": round(usage_ratio * 100, 1),
                "transcriber_count": transcriber_count,
            },
        )

    def start(self) -> None:
        """Start the memory watchdog."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the memory watchdog."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Memory watchdog stopped")
