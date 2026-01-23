"""Memory watchdog to prevent runaway memory usage.

This module monitors memory usage and triggers shutdown if limits are exceeded.
It only takes action when system memory is actually constrained.
"""

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from .service import Application

logger = logging.getLogger(__name__)

# Memory per active transcriber (WebRTC + resampler + buffers + Modal WebSocket)
# Used for estimating capacity at startup
MEMORY_PER_TRANSCRIBER_MB = 100

# Check interval in seconds
CHECK_INTERVAL_SECONDS = 5

# Minimum available system memory before taking action (MB)
# If available memory drops below this, we start warning/shutting down
MIN_AVAILABLE_MEMORY_MB = 100

# Critical threshold - force exit if available memory drops below this (MB)
CRITICAL_AVAILABLE_MEMORY_MB = 50


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


def _is_in_container() -> bool:
    """Check if we're running inside a container.

    Returns:
        True if running in a container, False otherwise
    """
    # Check for /.dockerenv (Docker)
    if os.path.exists("/.dockerenv"):
        return True

    # Check for container indicator in cgroup (works for Docker, Podman, etc.)
    try:
        with open("/proc/1/cgroup", "r") as f:
            content = f.read()
            # cgroup v2 shows "0::/" for containers with their own cgroup namespace
            # cgroup v1 shows paths like "/docker/<id>" or "/lxc/<id>"
            if "/docker/" in content or "/lxc/" in content or "/kubepods/" in content:
                return True
            # cgroup v2 with cgroup namespace - process 1 is at root of its namespace
            if content.strip() == "0::/":
                return True
    except OSError:
        pass

    return False


def _get_container_memory_limit_mb() -> float:
    """Get container memory limit in MB (from cgroups).

    Returns:
        Memory limit in megabytes, or 0 if no limit is set
        Returns -1 if in a container but limit is "max" (unlimited)
    """
    # Try cgroup v2 first
    try:
        with open("/sys/fs/cgroup/memory.max", "r") as f:
            value = f.read().strip()
            if value == "max":
                return -1.0  # Unlimited - signal that we're in a container but no limit
            return int(value) / (1024 * 1024)  # bytes to MB
    except (OSError, ValueError):
        pass

    # Try cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
            value = int(f.read().strip())
            # Very large values mean "no limit"
            if value >= 9223372036854771712:  # Common "no limit" value
                return -1.0  # Unlimited
            return value / (1024 * 1024)  # bytes to MB
    except (OSError, ValueError):
        pass

    return 0.0


def _get_container_memory_usage_mb() -> float:
    """Get container memory usage in MB (from cgroups).

    Returns:
        Memory usage in megabytes, or 0 if not in a container
    """
    # Try cgroup v2 first
    try:
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            return int(f.read().strip()) / (1024 * 1024)  # bytes to MB
    except (OSError, ValueError):
        pass

    # Try cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
            return int(f.read().strip()) / (1024 * 1024)  # bytes to MB
    except (OSError, ValueError):
        pass

    return 0.0


def _get_available_memory_mb() -> float:
    """Get available memory in MB.

    First checks container limits (cgroups), then falls back to host memory.
    If running in a container with no memory limit, returns 0 (skip checks).

    Returns:
        Available memory in megabytes, or 0 if unable to determine or unlimited
    """
    # Check container memory first
    container_limit = _get_container_memory_limit_mb()

    if container_limit == -1.0:
        # Container with unlimited memory - don't apply system memory checks
        # The container can use as much memory as needed
        return 0.0

    if container_limit > 0:
        container_usage = _get_container_memory_usage_mb()
        if container_usage > 0:
            return container_limit - container_usage

    # Only fall back to host memory if we're NOT in a container
    # (reading /proc/meminfo from inside a container shows host memory,
    # which is misleading when the container has no explicit limit)
    if _is_in_container():
        return 0.0

    # Fall back to host memory (only when running directly on host)
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    # Format: "MemAvailable:    12345 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024  # Convert kB to MB
    except (OSError, ValueError, IndexError):
        pass

    return 0.0


class InsufficientMemoryError(Exception):
    """Raised when there is not enough memory to accept a new transcriber."""

    pass


class MemoryWatchdog:
    """Monitors memory usage and triggers shutdown if system memory is low."""

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

        # Allow hard limit override via environment variable
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
        """Calculate memory limit for status reporting.

        If LT_MAX_MEMORY_MB is set, returns that.
        Otherwise returns 0 (no limit).

        Returns:
            Memory limit in megabytes, or 0 if no limit
        """
        if self._env_max_memory_mb:
            return float(self._env_max_memory_mb)
        return 0.0

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        logger.info(
            "Memory watchdog started",
            extra={
                "check_interval_seconds": CHECK_INTERVAL_SECONDS,
                "min_available_memory_mb": MIN_AVAILABLE_MEMORY_MB,
                "critical_available_memory_mb": CRITICAL_AVAILABLE_MEMORY_MB,
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
        available_mb = _get_available_memory_mb()
        transcriber_count = self._count_active_transcribers()

        # Check hard limit from environment variable
        if self._env_max_memory_mb and current_mb > 0:
            if current_mb >= self._env_max_memory_mb:
                logger.critical(
                    "Memory limit exceeded (LT_MAX_MEMORY_MB), forcing exit",
                    extra={
                        "current_mb": round(current_mb, 1),
                        "limit_mb": self._env_max_memory_mb,
                        "available_mb": round(available_mb, 1),
                        "transcriber_count": transcriber_count,
                    },
                )
                sys.exit(137)

            if current_mb >= self._env_max_memory_mb * 0.95:
                if not self._shutdown_triggered:
                    self._shutdown_triggered = True
                    logger.error(
                        "Approaching memory limit, initiating graceful shutdown",
                        extra={
                            "current_mb": round(current_mb, 1),
                            "limit_mb": self._env_max_memory_mb,
                            "available_mb": round(available_mb, 1),
                            "transcriber_count": transcriber_count,
                        },
                    )
                    try:
                        await asyncio.wait_for(self._shutdown_callback(), timeout=30)
                    except asyncio.TimeoutError:
                        logger.error("Graceful shutdown timed out, forcing exit")
                    except Exception as e:
                        logger.exception("Error during graceful shutdown", exc_info=e)
                    sys.exit(137)
                return

            if current_mb >= self._env_max_memory_mb * 0.80:
                logger.warning(
                    "Memory usage approaching limit",
                    extra={
                        "current_mb": round(current_mb, 1),
                        "limit_mb": self._env_max_memory_mb,
                        "usage_percent": round(
                            current_mb / self._env_max_memory_mb * 100, 1
                        ),
                        "transcriber_count": transcriber_count,
                    },
                )
                return

        # Check system available memory (only if we can read it)
        if available_mb > 0:
            if available_mb < CRITICAL_AVAILABLE_MEMORY_MB:
                logger.critical(
                    "System memory critically low, forcing exit",
                    extra={
                        "current_mb": round(current_mb, 1),
                        "available_mb": round(available_mb, 1),
                        "critical_threshold_mb": CRITICAL_AVAILABLE_MEMORY_MB,
                        "transcriber_count": transcriber_count,
                    },
                )
                sys.exit(137)

            if available_mb < MIN_AVAILABLE_MEMORY_MB:
                if not self._shutdown_triggered:
                    self._shutdown_triggered = True
                    logger.error(
                        "System memory low, initiating graceful shutdown",
                        extra={
                            "current_mb": round(current_mb, 1),
                            "available_mb": round(available_mb, 1),
                            "min_threshold_mb": MIN_AVAILABLE_MEMORY_MB,
                            "transcriber_count": transcriber_count,
                        },
                    )
                    try:
                        await asyncio.wait_for(self._shutdown_callback(), timeout=30)
                    except asyncio.TimeoutError:
                        logger.error("Graceful shutdown timed out, forcing exit")
                    except Exception as e:
                        logger.exception("Error during graceful shutdown", exc_info=e)
                    sys.exit(137)
                return

        # Normal operation - log at debug level periodically
        logger.debug(
            "Memory check OK",
            extra={
                "current_mb": round(current_mb, 1),
                "available_mb": round(available_mb, 1),
                "transcriber_count": transcriber_count,
            },
        )

    def check_memory_available_for_new_transcriber(self) -> None:
        """Check if there's enough memory to accept a new transcriber.

        Raises:
            InsufficientMemoryError: If there's not enough memory
        """
        current_mb = _get_current_rss_mb()
        available_mb = _get_available_memory_mb()

        # Check against environment limit if set
        if self._env_max_memory_mb and current_mb > 0:
            projected_usage = current_mb + MEMORY_PER_TRANSCRIBER_MB
            if projected_usage > self._env_max_memory_mb:
                transcriber_count = self._count_active_transcribers()
                logger.error(
                    "Cannot accept new transcriber: would exceed configured memory limit",
                    extra={
                        "current_mb": round(current_mb, 1),
                        "projected_mb": round(projected_usage, 1),
                        "limit_mb": self._env_max_memory_mb,
                        "transcriber_count": transcriber_count,
                    },
                )
                raise InsufficientMemoryError(
                    f"Insufficient memory: accepting a new transcriber would use "
                    f"{projected_usage:.0f}MB, exceeding limit of {self._env_max_memory_mb}MB"
                )

        # Check against available system memory
        if available_mb > 0 and available_mb < MEMORY_PER_TRANSCRIBER_MB + MIN_AVAILABLE_MEMORY_MB:
            transcriber_count = self._count_active_transcribers()
            logger.error(
                "Cannot accept new transcriber: insufficient system memory available",
                extra={
                    "current_mb": round(current_mb, 1),
                    "available_mb": round(available_mb, 1),
                    "needed_mb": MEMORY_PER_TRANSCRIBER_MB,
                    "transcriber_count": transcriber_count,
                },
            )
            raise InsufficientMemoryError(
                f"Insufficient memory: only {available_mb:.0f}MB available, "
                f"need {MEMORY_PER_TRANSCRIBER_MB}MB for new transcriber plus "
                f"{MIN_AVAILABLE_MEMORY_MB}MB reserve"
            )

    def check_startup_memory(self) -> None:
        """Check if system has enough memory at startup.

        Logs warnings or errors if memory is constrained.
        """
        available_mb = _get_available_memory_mb()
        current_mb = _get_current_rss_mb()

        if available_mb == 0:
            logger.warning(
                "Could not determine available system memory - memory checks disabled"
            )
            return

        # Estimate how many transcribers we can support
        usable_memory = available_mb - MIN_AVAILABLE_MEMORY_MB
        if self._env_max_memory_mb:
            # Factor in the hard limit
            max_from_limit = self._env_max_memory_mb - current_mb
            usable_memory = min(usable_memory, max_from_limit)

        estimated_capacity = max(0, int(usable_memory / MEMORY_PER_TRANSCRIBER_MB))

        if estimated_capacity == 0:
            logger.error(
                "System has insufficient memory for transcription",
                extra={
                    "available_mb": round(available_mb, 1),
                    "current_rss_mb": round(current_mb, 1),
                    "env_max_memory_mb": self._env_max_memory_mb,
                },
            )
        elif estimated_capacity <= 2:
            logger.warning(
                "System memory is limited",
                extra={
                    "available_mb": round(available_mb, 1),
                    "current_rss_mb": round(current_mb, 1),
                    "estimated_max_transcribers": estimated_capacity,
                    "env_max_memory_mb": self._env_max_memory_mb,
                },
            )
        else:
            logger.info(
                "Memory check passed",
                extra={
                    "available_mb": round(available_mb, 1),
                    "current_rss_mb": round(current_mb, 1),
                    "estimated_max_transcribers": estimated_capacity,
                    "env_max_memory_mb": self._env_max_memory_mb,
                },
            )

    def start(self) -> None:
        """Start the memory watchdog."""
        self.check_startup_memory()
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
