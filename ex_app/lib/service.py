"""Application service for managing transcription sessions."""

import asyncio
import logging
from typing import Optional

from .livetypes import (
    HPBSettings,
    SigConnectResult,
    TranscriptionProviderException,
)
from .spreed_client import SpreedClient
from .utils import get_hpb_settings

logger = logging.getLogger(__name__)


class Application:
    """Application service that manages SpreedClients for each room."""

    def __init__(self) -> None:
        """Initialize the application."""
        self.clients: dict[str, SpreedClient] = {}
        self.client_lock = asyncio.Lock()
        self._hpb_settings: Optional[HPBSettings] = None

    async def get_hpb_settings(self) -> HPBSettings:
        """Get HPB settings, caching the result.

        Returns:
            HPBSettings with STUN/TURN configuration
        """
        if self._hpb_settings is None:
            self._hpb_settings = get_hpb_settings()
        return self._hpb_settings

    async def _leave_call_cb(self, room_token: str) -> None:
        """Callback when a SpreedClient leaves a call.

        Args:
            room_token: The room token being left
        """
        async with self.client_lock:
            if room_token in self.clients:
                logger.debug(
                    "Removing client for room",
                    extra={"room_token": room_token},
                )
                del self.clients[room_token]

    async def transcript_req(
        self,
        room_token: str,
        nc_session_id: str,
        enable: bool,
        lang_id: str,
    ) -> None:
        """Handle a transcription request.

        Args:
            room_token: Talk room token
            nc_session_id: Nextcloud session ID
            enable: Whether to enable or disable transcription
            lang_id: Language code for transcription

        Raises:
            TranscriptionProviderException: If connection fails
        """
        async with self.client_lock:
            client = self.clients.get(room_token)

            if not enable:
                if client:
                    await client.remove_target(nc_session_id)
                    logger.info(
                        "Removed transcription target",
                        extra={
                            "room_token": room_token,
                            "nc_session_id": nc_session_id,
                        },
                    )
                return

            # Enable transcription
            if client and not client.defunct.is_set():
                # Client exists and is active
                await client.add_target(nc_session_id)
                logger.info(
                    "Added transcription target to existing client",
                    extra={
                        "room_token": room_token,
                        "nc_session_id": nc_session_id,
                    },
                )
                return

            # Need to create a new client
            logger.info(
                "Creating new SpreedClient for room",
                extra={
                    "room_token": room_token,
                    "lang_id": lang_id,
                },
            )

            hpb_settings = await self.get_hpb_settings()
            client = SpreedClient(
                room_token=room_token,
                hpb_settings=hpb_settings,
                lang_id=lang_id,
                leave_call_cb=self._leave_call_cb,
            )
            self.clients[room_token] = client

        # Connect outside the lock to avoid blocking
        result = await client.connect()

        if result == SigConnectResult.FAILURE:
            async with self.client_lock:
                if room_token in self.clients:
                    del self.clients[room_token]
            raise TranscriptionProviderException(
                "Failed to connect to HPB signaling server",
                retcode=503,
            )

        if result == SigConnectResult.RETRY:
            # Connection is being retried, add target anyway
            logger.debug(
                "Connection being retried, adding target",
                extra={"room_token": room_token},
            )

        await client.add_target(nc_session_id)
        logger.info(
            "Started transcription",
            extra={
                "room_token": room_token,
                "nc_session_id": nc_session_id,
                "lang_id": lang_id,
            },
        )

    async def set_language(self, room_token: str, lang_id: str) -> None:
        """Set the transcription language for a room.

        Args:
            room_token: Talk room token
            lang_id: Language code

        Raises:
            TranscriptionProviderException: If no active session or language change fails
        """
        async with self.client_lock:
            client = self.clients.get(room_token)

        if not client or client.defunct.is_set():
            raise TranscriptionProviderException(
                f"No active transcription session for room {room_token}",
                retcode=404,
            )

        await client.set_language(lang_id)
        logger.info(
            "Changed language for room",
            extra={
                "room_token": room_token,
                "lang_id": lang_id,
            },
        )

    async def leave_call(self, room_token: str) -> None:
        """Explicitly leave a call.

        Args:
            room_token: Talk room token
        """
        async with self.client_lock:
            client = self.clients.get(room_token)

        if client:
            await client.close()
            logger.info(
                "Left call",
                extra={"room_token": room_token},
            )

    async def shutdown(self) -> None:
        """Shutdown all clients."""
        logger.info("Shutting down application")
        async with self.client_lock:
            clients = list(self.clients.values())
            self.clients.clear()

        for client in clients:
            try:
                await asyncio.wait_for(client.close(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout closing client during shutdown",
                    extra={"room_token": client.room_token},
                )
            except Exception as e:
                logger.exception(
                    "Error closing client during shutdown",
                    exc_info=e,
                    extra={"room_token": client.room_token},
                )

        logger.info("Application shutdown complete")

    def get_active_rooms(self) -> list[str]:
        """Get list of active room tokens.

        Returns:
            List of room tokens with active transcription
        """
        return [
            token
            for token, client in self.clients.items()
            if not client.defunct.is_set()
        ]
