"""Modal STT protocol definitions.

Pure functions for:
- Parsing Modal messages
- Constructing URLs and headers
- Message type definitions

No I/O, no state - just data transformations.
"""

import json
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class ModalMessageType(Enum):
    """Types of messages from Modal STT service."""

    TOKEN = auto()      # Partial transcription text
    VAD_END = auto()    # Voice activity detection - end of speech
    ERROR = auto()      # Error from service
    PING = auto()       # Keepalive ping
    UNKNOWN = auto()    # Unrecognized message type


@dataclass(frozen=True)
class ModalMessage:
    """Parsed message from Modal STT service.

    Immutable data class representing a single message.
    """

    type: ModalMessageType
    text: str = ""
    error_message: str = ""
    raw: str = ""

    @property
    def is_token(self) -> bool:
        return self.type == ModalMessageType.TOKEN

    @property
    def is_vad_end(self) -> bool:
        return self.type == ModalMessageType.VAD_END

    @property
    def is_error(self) -> bool:
        return self.type == ModalMessageType.ERROR

    @property
    def has_text(self) -> bool:
        return bool(self.text)


@dataclass(frozen=True)
class ModalConfig:
    """Configuration for Modal STT service.

    Immutable - create a new instance to change values.
    """

    workspace: str
    key: str
    secret: str

    @property
    def url(self) -> str:
        """Construct the WebSocket URL."""
        return get_modal_url(self.workspace)

    @property
    def headers(self) -> dict[str, str]:
        """Get authentication headers."""
        return get_modal_headers(self.key, self.secret)

    @classmethod
    def from_env(cls) -> "ModalConfig":
        """Create config from environment variables."""
        import os

        workspace = os.getenv("MODAL_WORKSPACE", "")
        key = os.getenv("MODAL_KEY", "")
        secret = os.getenv("MODAL_SECRET", "")

        return cls(workspace=workspace, key=key, secret=secret)

    def is_configured(self) -> bool:
        """Check if all required fields are set."""
        return bool(self.workspace and self.key and self.secret)


def get_modal_url(workspace: str) -> str:
    """Construct Modal WebSocket URL.

    Args:
        workspace: Modal workspace name

    Returns:
        WebSocket URL for STT service
    """
    return f"wss://{workspace}--kyutai-stt-kyutaisttservice-serve.modal.run/v1/stream"


def get_modal_headers(key: str, secret: str) -> dict[str, str]:
    """Construct Modal authentication headers.

    Args:
        key: Modal API key
        secret: Modal API secret

    Returns:
        Headers dict for WebSocket connection
    """
    return {
        "Modal-Key": key,
        "Modal-Secret": secret,
    }


def parse_modal_message(raw_message: str) -> ModalMessage:
    """Parse a raw JSON message from Modal.

    Pure function - no side effects.

    Args:
        raw_message: Raw JSON string from WebSocket

    Returns:
        Parsed ModalMessage

    Examples:
        >>> parse_modal_message('{"type": "token", "text": "hello"}')
        ModalMessage(type=ModalMessageType.TOKEN, text='hello', ...)

        >>> parse_modal_message('{"type": "vad_end"}')
        ModalMessage(type=ModalMessageType.VAD_END, ...)

        >>> parse_modal_message('{"type": "error", "message": "bad input"}')
        ModalMessage(type=ModalMessageType.ERROR, error_message='bad input', ...)
    """
    try:
        data = json.loads(raw_message)
    except json.JSONDecodeError:
        return ModalMessage(
            type=ModalMessageType.ERROR,
            error_message=f"Invalid JSON: {raw_message[:100]}",
            raw=raw_message,
        )

    msg_type_str = data.get("type", "")

    if msg_type_str == "token":
        return ModalMessage(
            type=ModalMessageType.TOKEN,
            text=data.get("text", ""),
            raw=raw_message,
        )
    elif msg_type_str == "vad_end":
        return ModalMessage(
            type=ModalMessageType.VAD_END,
            raw=raw_message,
        )
    elif msg_type_str == "error":
        return ModalMessage(
            type=ModalMessageType.ERROR,
            error_message=data.get("message", "Unknown error"),
            raw=raw_message,
        )
    elif msg_type_str == "ping":
        return ModalMessage(
            type=ModalMessageType.PING,
            raw=raw_message,
        )
    else:
        return ModalMessage(
            type=ModalMessageType.UNKNOWN,
            raw=raw_message,
        )


def create_audio_chunk(audio_float32: bytes) -> bytes:
    """Prepare audio chunk for sending to Modal.

    Modal expects raw float32 little-endian bytes.
    This function is a no-op but documents the contract.

    Args:
        audio_float32: Float32 LE audio bytes

    Returns:
        Same bytes (Modal expects raw bytes, not wrapped)
    """
    return audio_float32
