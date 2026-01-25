"""SpreedClient for connecting to Nextcloud Talk HPB."""

import asyncio
import dataclasses
import gc
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from secrets import token_urlsafe
from urllib.parse import urlparse

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.sdp import candidate_from_sdp
from nc_py_api import NextcloudApp
from websockets import State as WsState
from websockets import connect
from websockets.client import ClientConnection
from websockets.exceptions import WebSocketException

from .audio_stream import AudioStream
from .constants import (
    CALL_LEAVE_TIMEOUT,
    HPB_PING_TIMEOUT,
    LT_HPB_URL,
    LT_INTERNAL_SECRET,
)
from .livetypes import (
    CallFlag,
    HPBSettings,
    ReconnectMethod,
    SigConnectResult,
    SpreedRateLimitedException,
    Target,
    Transcript,
    TranscriptionProviderException,
)
from .models import LANGUAGE_MAP
from .transcriber import ModalTranscriber
from .utils import get_ssl_context, hmac_sha256, sanitize_websocket_url

logger = logging.getLogger(__name__)

# Timeout for receiving messages during connection
MSG_RECEIVE_TIMEOUT = 30


@dataclasses.dataclass
class PeerConnection:
    """Wrapper for RTCPeerConnection with session ID."""

    session_id: str
    pc: RTCPeerConnection


class SpreedClient:
    """Client for connecting to Nextcloud Talk High-Performance Backend."""

    def __init__(
        self,
        room_token: str,
        hpb_settings: HPBSettings,
        lang_id: str,
        leave_call_cb: Callable[[str], Awaitable[None]],
    ) -> None:
        """Initialize the SpreedClient.

        Args:
            room_token: Talk room token
            hpb_settings: HPB settings with STUN/TURN servers
            lang_id: Language ID for transcription
            leave_call_cb: Callback when leaving the call
        """
        self.id = 0
        self._server: ClientConnection | None = None
        self._monitor: asyncio.Task | None = None
        self.peer_connections: dict[str, PeerConnection] = {}
        self.peer_connection_lock = asyncio.Lock()
        self.targets: dict[str, Target] = {}
        self.target_lock = asyncio.Lock()
        self.nc_sid_map: dict[str, str] = {}
        self._nc_sid_wait_stash: dict[str, None] = {}
        self.transcript_queue: asyncio.Queue[Transcript] = asyncio.Queue()
        self._transcript_sender: asyncio.Task | None = None
        self.transcribers: dict[str, ModalTranscriber] = {}
        self.transcriber_lock = asyncio.Lock()
        self._result_consumer_tasks: dict[str, asyncio.Task] = {}
        self._audio_streams: dict[str, AudioStream] = {}
        self.defunct = threading.Event()
        self._close_task: asyncio.Task | None = None
        self._deferred_close_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

        self.resumeid: str | None = None
        self.sessionid: str | None = None

        nc = NextcloudApp()
        self._websocket_url = sanitize_websocket_url(LT_HPB_URL)
        self._backendURL = (
            nc.app_cfg.endpoint
            + "/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
        )
        self.secret = LT_INTERNAL_SECRET

        self.room_token = room_token
        self.hpb_settings = hpb_settings
        self.lang_id = lang_id
        self.leave_call_cb = leave_call_cb

    async def _resume_connection(self) -> bool:
        """Attempt to resume an existing HPB session.

        Returns:
            True if resume successful, False otherwise

        Raises:
            SpreedRateLimitedException: When rate limited by HPB
        """
        try:
            await self.send_message(
                {
                    "type": "hello",
                    "hello": {
                        "version": "2.0",
                        "resumeid": self.resumeid,
                    },
                }
            )
        except Exception as e:
            logger.exception(
                "Error resuming connection to HPB with short hello",
                exc_info=e,
                extra={"room_token": self.room_token},
            )
            return False

        msg_counter = 0
        while msg_counter < 10:
            message = await self.receive(MSG_RECEIVE_TIMEOUT)
            if message is None:
                logger.error(
                    "No message received for %s secs while resuming, aborting...",
                    MSG_RECEIVE_TIMEOUT,
                    extra={"room_token": self.room_token},
                )
                return False

            if message.get("type") == "hello":
                self.sessionid = message["hello"]["sessionid"]
                logger.debug(
                    "Resumed connection with new session ID",
                    extra={
                        "sessionid": self.sessionid,
                        "resumeid": self.resumeid,
                        "room_token": self.room_token,
                    },
                )
                return True

            if message.get("type") == "error":
                logger.error(
                    "Signaling error message received during a short resume",
                    extra={
                        "room_token": self.room_token,
                        "msg_counter": msg_counter,
                        "error_received": message,
                    },
                )

                err_code = message.get("error", {}).get("code")
                if err_code == "no_such_session":
                    logger.info(
                        "Performing a full reconnect since the previous session expired",
                        extra={"room_token": self.room_token},
                    )
                    return False

                if err_code == "too_many_requests":
                    logger.error(
                        "Rate limited by the HPB during short resume, giving up",
                        extra={"room_token": self.room_token},
                    )
                    raise SpreedRateLimitedException()

                return False

            msg_counter += 1

        return False

    async def connect(
        self, reconnect: ReconnectMethod = ReconnectMethod.NO_RECONNECT
    ) -> SigConnectResult:
        """Connect to the HPB signaling server.

        Args:
            reconnect: Reconnection method to use

        Returns:
            Connection result status
        """
        if (
            self._server
            and self._server.state == WsState.OPEN
            and reconnect != ReconnectMethod.FULL_RECONNECT
        ):
            logger.debug(
                "Already connected to signaling server, skipping connect",
                extra={"room_token": self.room_token, "reconnect": reconnect},
            )
            return SigConnectResult.SUCCESS

        websocket_host = urlparse(self._websocket_url).hostname
        ssl_ctx = get_ssl_context(self._websocket_url)
        try:
            self._server = await connect(
                self._websocket_url,
                **(
                    {
                        "server_hostname": websocket_host,
                        "ssl": ssl_ctx,
                    }
                    if ssl_ctx
                    else {}
                ),
                ping_timeout=HPB_PING_TIMEOUT,
            )
        except Exception as e:
            logger.exception(
                "Error connecting to signaling server, retrying...",
                exc_info=e,
                extra={"room_token": self.room_token, "reconnect": reconnect},
            )
            if reconnect != ReconnectMethod.NO_RECONNECT:
                await asyncio.sleep(2)
                self._reconnect_task = asyncio.create_task(
                    self.connect(reconnect=ReconnectMethod.FULL_RECONNECT)
                )
            return SigConnectResult.RETRY

        if reconnect == ReconnectMethod.SHORT_RESUME:
            self._reconnect_task = None
            try:
                res = await self._resume_connection()
            except SpreedRateLimitedException:
                if not self._close_task:
                    self._close_task = asyncio.create_task(self.close())
                return SigConnectResult.FAILURE
            except Exception as e:
                logger.exception(
                    "Unexpected error during short resume, retrying connection",
                    exc_info=e,
                    extra={"room_token": self.room_token},
                )
                if reconnect != ReconnectMethod.NO_RECONNECT:
                    self._reconnect_task = asyncio.create_task(
                        self.connect(reconnect=ReconnectMethod.SHORT_RESUME)
                    )
                return SigConnectResult.RETRY

            if res:
                logger.info(
                    "Resumed connection to signaling server for room token: %s",
                    self.room_token,
                    extra={"room_token": self.room_token},
                )
                await self.send_incall()
                await self.send_join()
                return SigConnectResult.SUCCESS

            logger.info(
                "Short resume failed, performing full reconnect for room token: %s",
                self.room_token,
                extra={"room_token": self.room_token},
            )
            if reconnect != ReconnectMethod.NO_RECONNECT:
                await asyncio.sleep(2)
                self._reconnect_task = asyncio.create_task(
                    self.connect(reconnect=ReconnectMethod.FULL_RECONNECT)
                )
            return SigConnectResult.RETRY

        if reconnect == ReconnectMethod.FULL_RECONNECT:
            self._reconnect_task = None
            logger.info(
                "Performing full reconnect for room token: %s",
                self.room_token,
                extra={"room_token": self.room_token},
            )
            try:
                await asyncio.wait_for(self.close(), CALL_LEAVE_TIMEOUT)
            except TimeoutError:
                logger.warning(
                    "Timeout while closing SpreedClient during full reconnect",
                    extra={"room_token": self.room_token},
                )
            finally:
                self.defunct.set()
                self._deferred_close_task = None
                self._monitor = None
                self.resumeid = None
                self.sessionid = None
                self._server = None

        await self.send_hello()

        msg_counter = 0
        while True:
            message = await self.receive(MSG_RECEIVE_TIMEOUT)
            if message is None:
                logger.error(
                    "No message received for %s secs, aborting...",
                    MSG_RECEIVE_TIMEOUT,
                    extra={"room_token": self.room_token, "msg_counter": msg_counter},
                )
                return SigConnectResult.FAILURE

            if message.get("type") == "error":
                logger.error(
                    "Signaling error message received: %s\nDetails: %s",
                    message.get("error", {}).get("message"),
                    message.get("error", {}).get("details"),
                    extra={"room_token": self.room_token, "msg_counter": msg_counter},
                )

                message_code = message.get("error", {}).get("code")
                if message_code == "duplicate_session":
                    logger.error(
                        "Duplicate session found, aborting connection",
                        extra={"room_token": self.room_token},
                    )
                    return SigConnectResult.FAILURE
                if message_code == "room_join_failed":
                    logger.error(
                        "Room join failed, retrying...",
                        extra={"room_token": self.room_token},
                    )
                    if reconnect != ReconnectMethod.NO_RECONNECT:
                        await asyncio.sleep(2)
                        self._reconnect_task = asyncio.create_task(
                            self.connect(reconnect=ReconnectMethod.FULL_RECONNECT)
                        )
                    return SigConnectResult.RETRY

                return SigConnectResult.FAILURE

            if message.get("type") == "bye":
                logger.info(
                    "Received bye message, closing connection",
                    extra={"room_token": self.room_token},
                )
                return SigConnectResult.FAILURE

            if message.get("type") == "welcome":
                logger.debug(
                    "Welcome message received",
                    extra={"room_token": self.room_token},
                )
                continue

            if message.get("type") == "hello":
                self.sessionid = message["hello"]["sessionid"]
                self.resumeid = message["hello"]["resumeid"]
                logger.debug(
                    "Hello message received",
                    extra={
                        "sessionid": self.sessionid,
                        "resumeid": self.resumeid,
                        "room_token": self.room_token,
                    },
                )
                break

            msg_counter += 1
            if msg_counter > 10:
                logger.error(
                    "Too many messages received without 'welcome', reconnecting...",
                    extra={"room_token": self.room_token},
                )
                if reconnect != ReconnectMethod.NO_RECONNECT:
                    await asyncio.sleep(2)
                    self._reconnect_task = asyncio.create_task(
                        self.connect(reconnect=ReconnectMethod.FULL_RECONNECT)
                    )
                return SigConnectResult.RETRY

        self.defunct.clear()
        self._monitor = asyncio.create_task(self.signalling_monitor())

        if self._transcript_sender is None or self._transcript_sender.done():
            self._transcript_sender = asyncio.create_task(
                self.transcript_queue_consumer()
            )

        if reconnect == ReconnectMethod.NO_RECONNECT:
            self._deferred_close_task = asyncio.create_task(self.maybe_leave_call())

        await self.send_incall()
        await self.send_join()
        logger.info(
            "Connected to signaling server",
            extra={"room_token": self.room_token},
        )
        return SigConnectResult.SUCCESS

    async def send_message(self, message: dict) -> None:
        """Send a message to HPB.

        Args:
            message: Message to send
        """
        if not self._server:
            logger.error(
                "No server connection, cannot send message",
                extra={"room_token": self.room_token, "send_message": message},
            )
            return

        self.id += 1
        message["id"] = str(self.id)
        try:
            await self._server.send(json.dumps(message))
        except WebSocketException as e:
            logger.exception(
                "HPB websocket error, reconnecting...",
                exc_info=e,
                extra={"room_token": self.room_token},
            )
            if not self._reconnect_task or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(
                    self.connect(reconnect=ReconnectMethod.SHORT_RESUME)
                )
            return
        except Exception as e:
            logger.exception(
                "Unexpected error sending message to HPB, ignoring",
                exc_info=e,
                extra={"room_token": self.room_token},
            )
            return

        logger.debug(
            "Message sent",
            extra={"id": self.id, "room_token": self.room_token},
        )

    async def send_hello(self) -> None:
        """Send hello message to authenticate with HPB."""
        nonce = token_urlsafe(64)
        await self.send_message(
            {
                "type": "hello",
                "hello": {
                    "version": "2.0",
                    "auth": {
                        "type": "internal",
                        "params": {
                            "random": nonce,
                            "token": hmac_sha256(self.secret, nonce),
                            "backend": self._backendURL,
                        },
                    },
                },
            }
        )

    async def send_incall(self) -> None:
        """Send incall message to indicate we're in the call."""
        await self.send_message(
            {
                "type": "internal",
                "internal": {
                    "type": "incall",
                    "incall": {
                        "incall": CallFlag.IN_CALL,
                    },
                },
            }
        )

    async def send_join(self) -> None:
        """Send join message to join the room."""
        await self.send_message(
            {
                "type": "room",
                "room": {"roomid": self.room_token, "sessionid": self.sessionid},
            }
        )

    async def send_offer_request(self, publisher_session_id: str) -> None:
        """Request an offer from a participant.

        Args:
            publisher_session_id: Session ID of the publisher
        """
        await self.send_message(
            {
                "type": "message",
                "message": {
                    "recipient": {
                        "type": "session",
                        "sessionid": publisher_session_id,
                    },
                    "data": {"type": "requestoffer", "roomType": "video"},
                },
            }
        )

    async def send_offer_answer(
        self, publisher_session_id: str, offer_sid: str, sdp: str
    ) -> None:
        """Send SDP answer for an offer.

        Args:
            publisher_session_id: Session ID of the publisher
            offer_sid: Offer session ID
            sdp: SDP answer
        """
        await self.send_message(
            {
                "type": "message",
                "message": {
                    "recipient": {
                        "type": "session",
                        "sessionid": publisher_session_id,
                    },
                    "data": {
                        "to": publisher_session_id,
                        "type": "answer",
                        "roomType": "video",
                        "sid": offer_sid,
                        "payload": {
                            "nick": "Kyutai Transcriber",
                            "type": "answer",
                            "sdp": sdp,
                        },
                    },
                },
            }
        )

    async def send_candidate(
        self, sender: str, offer_sid: str, candidate_str: str
    ) -> None:
        """Send ICE candidate.

        Args:
            sender: Sender session ID
            offer_sid: Offer session ID
            candidate_str: ICE candidate string
        """
        await self.send_message(
            {
                "type": "message",
                "message": {
                    "recipient": {
                        "type": "session",
                        "sessionid": sender,
                    },
                    "data": {
                        "to": sender,
                        "type": "candidate",
                        "sid": offer_sid,
                        "roomType": "video",
                        "payload": {
                            "candidate": {
                                "candidate": candidate_str,
                                "sdpMLineIndex": 0,
                                "sdpMid": "0",
                            }
                        },
                    },
                },
            }
        )

    async def send_bye(self) -> None:
        """Send bye message to leave the call."""
        await self.send_message({"type": "bye", "bye": {}})

    async def send_transcript(self, transcript: Transcript) -> None:
        """Send transcript to all targets.

        Args:
            transcript: Transcript to send
        """
        async with self.target_lock:
            if not self.targets:
                logger.debug(
                    "No targets to send transcript to, skipping",
                    extra={"room_token": self.room_token},
                )
                return
            sids = list(self.targets.keys())
            nc_sid_map = dict(self.nc_sid_map)

        nc_targets = [
            nc_sid for nc_sid, session_id in nc_sid_map.items() if session_id in sids
        ]
        preview = transcript.message if len(transcript.message) < 200 else transcript.message[:197] + "..."
        logger.info(
            "Sending transcript",
            extra={
                "room_token": self.room_token,
                "speaker_session_id": transcript.speaker_session_id,
                "final": transcript.final,
                "targets": sids,
                "targets_nc": nc_targets,
                "lang_id": transcript.lang_id,
                "preview": preview,
            },
        )

        send_tasks = [
            self.send_message(
                {
                    "type": "message",
                    "message": {
                        "recipient": {
                            "type": "session",
                            "sessionid": sid,
                        },
                        "data": {
                            "final": transcript.final,
                            "langId": transcript.lang_id,
                            "message": transcript.message,
                            "speakerSessionId": transcript.speaker_session_id,
                            "type": "transcript",
                        },
                    },
                }
            )
            for sid in sids
        ]
        await asyncio.gather(*send_tasks)

    async def close(self) -> None:
        """Close the client and clean up resources."""
        if self.defunct.is_set():
            logger.debug(
                "SpreedClient is already defunct, skipping close",
                extra={"room_token": self.room_token},
            )
            return

        if self._deferred_close_task and not self._deferred_close_task.done():
            logger.debug(
                "Cancelling deferred close task",
                extra={"room_token": self.room_token},
            )
            self._deferred_close_task.cancel()
            self._deferred_close_task = None

        if self._reconnect_task and not self._reconnect_task.done():
            logger.debug(
                "Cancelling reconnect task",
                extra={"room_token": self.room_token},
            )
            self._reconnect_task.cancel()
            self._reconnect_task = None

        app_closing = self._monitor.cancelled() if self._monitor else False

        with suppress(Exception):
            if self._monitor and not self._monitor.done():
                logger.debug(
                    "Cancelling monitor task",
                    extra={"room_token": self.room_token},
                )
                self._monitor.cancel()
            self._monitor = None

        with suppress(Exception):
            await self.send_bye()

        with suppress(Exception):
            logger.debug(
                "Cancelling result consumer tasks",
                extra={"room_token": self.room_token},
            )
            for task in self._result_consumer_tasks.values():
                if not task.done():
                    task.cancel()
            self._result_consumer_tasks.clear()

        with suppress(Exception):
            logger.debug(
                "Stopping audio streams",
                extra={"room_token": self.room_token},
            )
            for stream in self._audio_streams.values():
                await stream.stop()
            self._audio_streams.clear()

        with suppress(Exception):
            logger.debug(
                "Shutting down all transcribers",
                extra={"room_token": self.room_token},
            )
            for transcriber in self.transcribers.values():
                await transcriber.stop()
            async with self.transcriber_lock:
                self.transcribers.clear()

        with suppress(Exception):
            for pc in self.peer_connections.values():
                if pc.pc.connectionState not in ("closed", "failed"):
                    logger.debug(
                        "Closing peer connection",
                        extra={
                            "session_id": pc.session_id,
                            "room_token": self.room_token,
                        },
                    )
                    with suppress(Exception):
                        await pc.pc.close()
            async with self.peer_connection_lock:
                self.peer_connections.clear()
            self.resumeid = None
            self.sessionid = None

        with suppress(Exception):
            if self._transcript_sender and not self._transcript_sender.done():
                logger.debug(
                    "Cancelling transcript sender task",
                    extra={"room_token": self.room_token},
                )
                self._transcript_sender.cancel()
                self._transcript_sender = None

        # Clear transcript queue to release memory
        while not self.transcript_queue.empty():
            try:
                self.transcript_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        with suppress(Exception):
            if self._server and self._server.state == WsState.OPEN:
                logger.debug(
                    "Closing WebSocket connection",
                    extra={"room_token": self.room_token},
                )
                await self._server.close()
            self._server = None

        self.defunct.set()

        # Force garbage collection to release memory from aiortc/numpy
        gc.collect()

        if not app_closing:
            await self.leave_call_cb(self.room_token)

    async def receive(self, timeout: int = 0) -> dict | None:
        """Receive a message from HPB.

        Args:
            timeout: Timeout in seconds (0 for no timeout)

        Returns:
            Parsed message or None
        """
        if not self._server:
            logger.debug(
                "No server connection, cannot receive message",
                extra={"room_token": self.room_token},
            )
            return None

        if timeout > 0:
            received_msg = await asyncio.wait_for(self._server.recv(), timeout)
        else:
            received_msg = await self._server.recv()

        message = json.loads(received_msg)
        logger.debug(
            "Message received",
            extra={"recv_message": message, "room_token": self.room_token},
        )
        return message

    async def add_target(self, nc_session_id: str) -> None:
        """Add a target for receiving transcripts.

        Args:
            nc_session_id: Nextcloud session ID
        """
        async with self.target_lock:
            if nc_session_id not in self.nc_sid_map:
                self._nc_sid_wait_stash[nc_session_id] = None
                logger.debug(
                    "HPB session ID not found, deferring add",
                    extra={
                        "nc_session_id": nc_session_id,
                        "room_token": self.room_token,
                    },
                )
                return

            self._nc_sid_wait_stash.pop(nc_session_id, None)
            session_id = self.nc_sid_map[nc_session_id]
            if session_id not in self.targets:
                self.targets[session_id] = Target()
                logger.debug(
                    "Added target",
                    extra={
                        "session_id": session_id,
                        "nc_session_id": nc_session_id,
                        "room_token": self.room_token,
                    },
                )
            if self._deferred_close_task:
                self._deferred_close_task.cancel()
                self._deferred_close_task = None

    async def remove_target(self, nc_session_id: str) -> None:
        """Remove a target.

        Args:
            nc_session_id: Nextcloud session ID
        """
        async with self.target_lock:
            self._nc_sid_wait_stash.pop(nc_session_id, None)
            if nc_session_id not in self.nc_sid_map:
                logger.debug(
                    "HPB session ID not found",
                    extra={
                        "nc_session_id": nc_session_id,
                        "room_token": self.room_token,
                    },
                )
                return

            session_id = self.nc_sid_map[nc_session_id]
            if session_id in self.targets:
                logger.debug(
                    "Removed target",
                    extra={
                        "session_id": session_id,
                        "nc_session_id": nc_session_id,
                        "room_token": self.room_token,
                    },
                )
                del self.targets[session_id]
                if len(self.targets) == 0:
                    if self._deferred_close_task:
                        self._deferred_close_task.cancel()
                    self._deferred_close_task = asyncio.create_task(
                        self.maybe_leave_call()
                    )

    async def remove_target_hpb_sid(self, session_id: str) -> None:
        """Remove a target by HPB session ID.

        Args:
            session_id: HPB session ID
        """
        async with self.target_lock:
            if session_id in self.targets:
                logger.debug(
                    "Removed target by HPB SID",
                    extra={
                        "session_id": session_id,
                        "room_token": self.room_token,
                    },
                )
                del self.targets[session_id]
                if len(self.targets) == 0:
                    if self._deferred_close_task:
                        self._deferred_close_task.cancel()
                    self._deferred_close_task = asyncio.create_task(
                        self.maybe_leave_call()
                    )

    async def signalling_monitor(self) -> None:
        """Monitor the signaling server for incoming messages."""
        while True:
            try:
                message = await self.receive()
            except WebSocketException as e:
                logger.exception(
                    "HPB websocket error, reconnecting...",
                    exc_info=e,
                    extra={"room_token": self.room_token},
                )
                if not self._reconnect_task or self._reconnect_task.done():
                    self._reconnect_task = asyncio.create_task(
                        self.connect(reconnect=ReconnectMethod.SHORT_RESUME)
                    )
                await asyncio.sleep(2)
                continue
            except asyncio.CancelledError:
                logger.debug(
                    "Signalling monitor task cancelled",
                    extra={"room_token": self.room_token},
                )
                if not self._close_task:
                    self._close_task = asyncio.create_task(self.close())
                raise
            except Exception as e:
                logger.exception(
                    "Unexpected error in signalling monitor",
                    exc_info=e,
                    extra={"room_token": self.room_token},
                )
                if not self._close_task:
                    self._close_task = asyncio.create_task(self.close())
                break

            if message is None:
                continue

            msg_type = message.get("type")

            if msg_type == "error":
                logger.error(
                    "Error message received: %s",
                    message.get("error", {}).get("message"),
                    extra={"room_token": self.room_token, "recv_message": message},
                )
                if message.get("error", {}).get("code") == "processing_failed":
                    continue
                if not self._close_task:
                    self._close_task = asyncio.create_task(self.close())
                return

            if (
                msg_type == "event"
                and message["event"]["target"] == "participants"
                and message["event"]["type"] == "update"
            ):
                await self._handle_participants_update(message)
                continue

            if msg_type == "message":
                data_type = message.get("message", {}).get("data", {}).get("type")
                if data_type == "offer":
                    logger.debug(
                        "Received offer message",
                        extra={"room_token": self.room_token},
                    )
                    await self.handle_offer(message)
                    continue
                if data_type == "candidate":
                    await self._handle_candidate(message)
                    continue

            if msg_type == "bye":
                logger.debug(
                    "Received bye message, closing connection",
                    extra={"room_token": self.room_token},
                )
                if not self._close_task:
                    self._close_task = asyncio.create_task(self.close())

    async def _handle_participants_update(self, message: dict) -> None:
        """Handle participants update event.

        Args:
            message: Update message from HPB
        """
        logger.debug(
            "Participants update received",
            extra={"room_token": self.room_token},
        )

        update = message["event"]["update"]
        if update.get("all") and update.get("incall") == 0:
            logger.debug(
                "Call ended for everyone, closing connection",
                extra={"room_token": self.room_token},
            )
            if not self._close_task:
                self._close_task = asyncio.create_task(self.close())
            return

        users_update = update.get("users", [])
        if not users_update:
            return

        for user_desc in users_update:
            if user_desc.get("internal", False):
                continue

            if user_desc["inCall"] == CallFlag.DISCONNECTED:
                logger.debug(
                    "User disconnected",
                    extra={
                        "user_desc": user_desc,
                        "room_token": self.room_token,
                    },
                )
                async with self.transcriber_lock:
                    if user_desc["sessionId"] in self.transcribers:
                        await self.transcribers[user_desc["sessionId"]].stop()
                        del self.transcribers[user_desc["sessionId"]]
                await self.remove_target_hpb_sid(user_desc["sessionId"])
                async with self.target_lock:
                    self.nc_sid_map.pop(
                        user_desc.get("nextcloudSessionId", ""), None
                    )
                continue

            async with self.target_lock:
                if "nextcloudSessionId" in user_desc:
                    self.nc_sid_map[user_desc["nextcloudSessionId"]] = user_desc[
                        "sessionId"
                    ]

            if user_desc.get("nextcloudSessionId") in self._nc_sid_wait_stash:
                logger.debug(
                    "Adding deferred target",
                    extra={
                        "nc_session_id": user_desc["nextcloudSessionId"],
                        "room_token": self.room_token,
                    },
                )
                await self.add_target(user_desc["nextcloudSessionId"])
                async with self.target_lock:
                    self._nc_sid_wait_stash.pop(
                        user_desc["nextcloudSessionId"], None
                    )

            if user_desc["inCall"] & CallFlag.IN_CALL and user_desc[
                "inCall"
            ] & CallFlag.WITH_AUDIO:
                logger.debug(
                    "User joined with audio",
                    extra={
                        "user_desc": user_desc,
                        "room_token": self.room_token,
                    },
                )
                async with self.peer_connection_lock:
                    if (
                        user_desc["sessionId"] in self.peer_connections
                        and self.peer_connections[
                            user_desc["sessionId"]
                        ].pc.connectionState
                        not in ("closed", "failed")
                    ):
                        logger.debug(
                            "Peer connection already exists, skipping offer request",
                            extra={"room_token": self.room_token},
                        )
                        continue
                await self.send_offer_request(user_desc["sessionId"])

        # Check if we're the last one in the call
        if len(users_update) == 2:
            if (
                users_update[0].get("sessionId") != self.sessionid
                and users_update[1].get("sessionId") != self.sessionid
            ):
                return

            transcriber_index = (
                0 if users_update[0].get("sessionId") == self.sessionid else 1
            )
            if (
                users_update[transcriber_index].get("inCall") & CallFlag.IN_CALL
                and users_update[transcriber_index ^ 1].get("inCall")
                == CallFlag.DISCONNECTED
            ):
                logger.debug(
                    "Last user left the call, closing connection",
                    extra={"room_token": self.room_token},
                )
                if not self._close_task:
                    self._close_task = asyncio.create_task(self.close())

    async def _handle_candidate(self, message: dict) -> None:
        """Handle ICE candidate message.

        Args:
            message: Candidate message from HPB
        """
        logger.debug(
            "Received candidate message",
            extra={
                "peer_session_id": message["message"]["sender"]["sessionid"],
                "room_token": self.room_token,
            },
        )
        candidate = candidate_from_sdp(
            message["message"]["data"]["payload"]["candidate"]["candidate"]
        )
        candidate.sdpMid = message["message"]["data"]["payload"]["candidate"]["sdpMid"]
        candidate.sdpMLineIndex = message["message"]["data"]["payload"]["candidate"][
            "sdpMLineIndex"
        ]
        async with self.peer_connection_lock:
            if message["message"]["sender"]["sessionid"] not in self.peer_connections:
                return
            await self.peer_connections[
                message["message"]["sender"]["sessionid"]
            ].pc.addIceCandidate(candidate)

    async def maybe_leave_call(self) -> None:
        """Leave the call if there are no targets."""
        logger.debug(
            "Waiting to leave call if there are no targets",
            extra={"room_token": self.room_token},
        )
        await asyncio.sleep(CALL_LEAVE_TIMEOUT)

        if self.defunct.is_set():
            logger.debug(
                "SpreedClient is already defunct, clearing deferred close task",
                extra={"room_token": self.room_token},
            )
            self._deferred_close_task = None
            return

        async with self.target_lock:
            len_targets = len(self.targets)
        if len_targets == 0:
            logger.debug(
                "No transcript receivers for %s secs, leaving the call",
                CALL_LEAVE_TIMEOUT,
                extra={"room_token": self.room_token},
            )
            if not self._close_task:
                self._close_task = asyncio.create_task(self.close())
        self._deferred_close_task = None

    async def handle_offer(self, message: dict) -> None:
        """Handle incoming WebRTC offer.

        Args:
            message: Offer message from HPB
        """
        if self.defunct.is_set():
            return

        spkr_sid = message["message"]["sender"]["sessionid"]
        async with self.peer_connection_lock:
            if (
                spkr_sid in self.peer_connections
                and self.peer_connections[spkr_sid].pc.connectionState
                not in ("closed", "failed")
            ):
                logger.debug(
                    "Peer connection already exists, skipping",
                    extra={
                        "session_id": spkr_sid,
                        "room_token": self.room_token,
                    },
                )
                return

        # Build ICE server configuration
        ice_servers = []
        for stunserver in self.hpb_settings.stunservers:
            ice_servers.append(RTCIceServer(urls=stunserver.urls))
        for turnserver in self.hpb_settings.turnservers:
            ice_servers.append(
                RTCIceServer(
                    urls=turnserver.urls,
                    username=turnserver.username,
                    credential=turnserver.credential,
                )
            )
        if len(ice_servers) == 0:
            ice_servers = None

        rtc_config = RTCConfiguration(iceServers=ice_servers)
        pc = RTCPeerConnection(configuration=rtc_config)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.debug(
                "Peer connection state changed",
                extra={
                    "session_id": spkr_sid,
                    "connection_state": pc.connectionState,
                    "room_token": self.room_token,
                },
            )
            if pc.connectionState in ("failed", "closed"):
                async with self.peer_connection_lock:
                    if spkr_sid in self.peer_connections:
                        del self.peer_connections[spkr_sid]

        pc.addTransceiver("audio", direction="recvonly")

        @pc.on("track")
        async def on_track(track):
            if track.kind == "audio":
                logger.debug(
                    "Receiving audio track from %s",
                    spkr_sid,
                    extra={
                        "session_id": spkr_sid,
                        "room_token": self.room_token,
                    },
                )
                stream = AudioStream(track)
                # Don't start stream yet - wait for Modal to connect first
                # to avoid filling the queue during cold start

                async with self.transcriber_lock:
                    transcriber = ModalTranscriber(
                        session_id=spkr_sid,
                        language=self.lang_id,
                    )
                    self.transcribers[spkr_sid] = transcriber
                    self._audio_streams[spkr_sid] = stream

                    try:
                        await transcriber.start(audio_stream=stream)
                        # Start consuming results in the background
                        task = asyncio.create_task(
                            self._consume_transcriber_results(transcriber, spkr_sid)
                        )
                        self._result_consumer_tasks[spkr_sid] = task
                    except Exception:
                        logger.exception(
                            "Error starting transcriber",
                            extra={
                                "session_id": spkr_sid,
                                "room_token": self.room_token,
                            },
                        )
                        if not self._close_task:
                            self._close_task = asyncio.create_task(self.close())
                        return

                    lang_name = LANGUAGE_MAP.get(self.lang_id)
                    lang_display = lang_name.name if lang_name else self.lang_id
                    logger.debug(
                        "Started transcriber for %s in %s",
                        spkr_sid,
                        lang_display,
                        extra={
                            "session_id": spkr_sid,
                            "language": self.lang_id,
                            "room_token": self.room_token,
                        },
                    )

        async with self.peer_connection_lock:
            self.peer_connections[spkr_sid] = PeerConnection(
                session_id=spkr_sid, pc=pc
            )

        await pc.setRemoteDescription(
            RTCSessionDescription(
                type="offer", sdp=message["message"]["data"]["payload"]["sdp"]
            )
        )

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Check if client is still active before sending answer
        if self.defunct.is_set():
            logger.debug(
                "Client defunct before sending answer, cleaning up",
                extra={"session_id": spkr_sid, "room_token": self.room_token},
            )
            await pc.close()
            return

        await self.send_offer_answer(
            spkr_sid,
            message["message"]["data"]["sid"],
            answer.sdp,
        )
        logger.debug(
            "Sent answer for offer from %s",
            spkr_sid,
            extra={
                "session_id": spkr_sid,
                "room_token": self.room_token,
            },
        )

        local_sdp = pc.localDescription.sdp
        for line in local_sdp.splitlines():
            if line.startswith("a=candidate:"):
                if self.defunct.is_set():
                    break
                await self.send_candidate(
                    message["message"]["sender"]["sessionid"],
                    message["message"]["data"]["sid"],
                    line[2:],
                )

    async def _consume_transcriber_results(
        self, transcriber: ModalTranscriber, speaker_sid: str
    ) -> None:
        """Consume results from a transcriber and queue them for sending.

        Args:
            transcriber: The transcriber instance
            speaker_sid: Speaker session ID
        """
        accumulated_text = ""
        try:
            async for result in transcriber.get_results():
                if result.text:
                    accumulated_text += result.text

                if result.is_vad_end or result.is_final:
                    if accumulated_text.strip():
                        transcript = Transcript(
                            final=True,
                            lang_id=self.lang_id,
                            message=accumulated_text.strip(),
                            speaker_session_id=speaker_sid,
                        )
                        await self.transcript_queue.put(transcript)
                    accumulated_text = ""
                elif accumulated_text and len(accumulated_text) > 50:
                    # Send partial results periodically
                    transcript = Transcript(
                        final=False,
                        lang_id=self.lang_id,
                        message=accumulated_text,
                        speaker_session_id=speaker_sid,
                    )
                    await self.transcript_queue.put(transcript)
        except asyncio.CancelledError:
            logger.debug(
                "Transcriber result consumer cancelled",
                extra={
                    "speaker_sid": speaker_sid,
                    "room_token": self.room_token,
                },
            )
        except Exception as e:
            logger.exception(
                "Error consuming transcriber results",
                exc_info=e,
                extra={
                    "speaker_sid": speaker_sid,
                    "room_token": self.room_token,
                },
            )

    async def set_language(self, lang_id: str) -> None:
        """Set the transcription language.

        Args:
            lang_id: Language code
        """
        excs: list[Exception] = []
        async with self.transcriber_lock:
            transcribers = list(self.transcribers.values())
        for transcriber in transcribers:
            try:
                transcriber.set_language(lang_id)
            except Exception as e:
                excs.append(e)
        if len(excs) > 1:
            logger.error(
                "Failed to set language for multiple transcribers",
                extra={
                    "lang_id": lang_id,
                    "room_token": self.room_token,
                    "excs": excs,
                },
            )
            raise TranscriptionProviderException(
                f"Failed to set language for multiple transcribers: {excs[0]}",
                retcode=500,
            )
        if len(excs) == 1:
            raise TranscriptionProviderException(
                f"Failed to set language for one transcriber: {excs[0]}",
                retcode=500,
            )
        self.lang_id = lang_id

    async def transcript_queue_consumer(self) -> None:
        """Consume transcripts from the queue and send them."""
        logger.debug(
            "Starting the transcript queue consumer",
            extra={"room_token": self.room_token},
        )
        while True:
            if self.defunct.is_set():
                logger.debug(
                    "SpreedClient is defunct, waiting before sending transcripts",
                    extra={"room_token": self.room_token},
                )
                await asyncio.sleep(2)
                continue

            transcript: Transcript = await self.transcript_queue.get()

            try:
                await asyncio.wait_for(
                    self.send_transcript(transcript),
                    timeout=10,
                )
            except TimeoutError:
                logger.error(
                    "Timeout while sending a transcript",
                    extra={
                        "speaker_session_id": transcript.speaker_session_id,
                        "room_token": self.room_token,
                    },
                )
                continue
            except asyncio.CancelledError:
                logger.debug(
                    "Transcript consumer task cancelled",
                    extra={"room_token": self.room_token},
                )
                raise
            except Exception as e:
                logger.exception(
                    "Error while sending transcript",
                    exc_info=e,
                    extra={
                        "speaker_session_id": transcript.speaker_session_id,
                        "room_token": self.room_token,
                    },
                )
                continue
