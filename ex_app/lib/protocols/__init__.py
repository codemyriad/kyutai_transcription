"""Protocol definitions for external services."""

from .modal import (
    ModalConfig,
    ModalMessage,
    ModalMessageType,
    parse_modal_message,
    get_modal_url,
    get_modal_headers,
)

__all__ = [
    "ModalConfig",
    "ModalMessage",
    "ModalMessageType",
    "parse_modal_message",
    "get_modal_url",
    "get_modal_headers",
]
