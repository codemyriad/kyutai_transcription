"""Pydantic models and types for Kyutai Transcription ExApp."""

import dataclasses
from enum import IntEnum
from typing import Optional

from pydantic import BaseModel, Field


class StunServer(BaseModel):
    """STUN server configuration."""

    urls: list[str]


class TurnServer(BaseModel):
    """TURN server configuration."""

    urls: list[str]
    username: str
    credential: str


class HPBSettings(BaseModel):
    """Settings received from HPB signaling settings endpoint."""

    server: str = ""
    stunservers: list[StunServer] = Field(default_factory=list)
    turnservers: list[TurnServer] = Field(default_factory=list)


class TranscribeRequest(BaseModel):
    """Request to start/stop transcription for a participant."""

    roomToken: str = Field(..., description="The Talk room token")
    ncSessionId: str = Field(..., description="The Nextcloud session ID")
    enable: bool = Field(default=True, description="Whether to enable or disable transcription")
    langId: str = Field(default="en", description="Language code for transcription")


class LanguageSetRequest(BaseModel):
    """Request to set the transcription language for a room."""

    roomToken: str = Field(..., description="The Talk room token")
    langId: str = Field(..., description="Language code to set")


class LeaveRequest(BaseModel):
    """Request to leave a call."""

    roomToken: str = Field(..., description="The Talk room token")


class Target(BaseModel):
    """A target for receiving transcripts (empty model as per original)."""

    pass


@dataclasses.dataclass
class Transcript:
    """A transcription result to be sent to participants."""

    final: bool
    lang_id: str
    message: str
    speaker_session_id: str


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str = ""
    modal_configured: bool = False
    hpb_configured: bool = False


class ErrorResponse(BaseModel):
    """Error response model."""

    error: str
    detail: Optional[str] = None


# Enums
class SigConnectResult(IntEnum):
    """Result of signaling connection attempt."""

    SUCCESS = 0
    FAILURE = 1  # do not retry
    RETRY = 2


class ReconnectMethod(IntEnum):
    """Method for reconnecting to HPB."""

    NO_RECONNECT = 0
    SHORT_RESUME = 1
    FULL_RECONNECT = 2


class CallFlag(IntEnum):
    """Flags for call participant state."""

    DISCONNECTED = 0
    IN_CALL = 1
    WITH_AUDIO = 2
    WITH_VIDEO = 4
    WITH_PHONE = 8


# Exceptions
class StreamEndedException(Exception):
    """Raised when an audio stream ends."""

    pass


class SpreedClientException(Exception):
    """Base exception for SpreedClient errors."""

    pass


class SpreedRateLimitedException(SpreedClientException):
    """Exception raised when rate limited by HPB server."""

    pass


class HPBConnectionError(Exception):
    """Error connecting to HPB."""

    pass


class HPBAuthenticationError(Exception):
    """Authentication error with HPB."""

    pass


class ModalConnectionError(Exception):
    """Error connecting to Modal."""

    pass


class TranscriptionProviderException(Exception):
    """Exception from transcription provider."""

    retcode: int

    def __init__(self, message: str, retcode: int = 500):
        super().__init__(message)
        self.retcode = retcode


class TranscriptionError(Exception):
    """Error during transcription."""

    pass
