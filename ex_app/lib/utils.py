"""Utility functions for Kyutai Transcription ExApp."""

import hashlib
import hmac
import logging
import os
import re
import ssl
from urllib.parse import urlparse

from nc_py_api import NextcloudApp

from .livetypes import HPBSettings

logger = logging.getLogger(__name__)


def hmac_sha256(key: str, message: str) -> str:
    """Generate HMAC-SHA256 signature.

    Args:
        key: Secret key
        message: Message to sign

    Returns:
        Hexadecimal signature
    """
    return hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def get_ssl_context(server_addr: str) -> ssl.SSLContext | None:
    """Get SSL context for WebSocket connection.

    Args:
        server_addr: Server URL

    Returns:
        SSL context or None for insecure connections
    """
    nc = NextcloudApp()

    if server_addr.startswith(("ws://", "http://")):
        logger.info(
            "Using default SSL context for insecure WebSocket connection (ws://)",
            extra={"server_addr": server_addr},
        )
        return None

    cert_verify = os.environ.get("SKIP_CERT_VERIFY", "false").lower()
    if cert_verify in ("true", "1"):
        logger.info(
            "Skipping certificate verification for WebSocket connection",
            extra={"server_addr": server_addr, "SKIP_CERT_VERIFY": cert_verify},
        )
        ssl_ctx = ssl.SSLContext()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        return ssl_ctx

    if nc.app_cfg.options.nc_cert and isinstance(
        nc.app_cfg.options.nc_cert, ssl.SSLContext
    ):
        logger.info(
            "Using SSL context provided by nc_py_api",
            extra={"server_addr": server_addr},
        )
        return nc.app_cfg.options.nc_cert

    logger.info(
        "Using default SSL context for WebSocket connection",
        extra={"server_addr": server_addr},
    )
    return None


def check_hpb_env_vars() -> None:
    """Check that required HPB environment variables are set.

    Raises:
        ValueError: If required variables are missing or invalid
    """
    required_vars = ("LT_HPB_URL", "LT_INTERNAL_SECRET")
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

    hpb_url = os.environ["LT_HPB_URL"]
    hpb_url_host = urlparse(hpb_url).hostname
    if not hpb_url_host:
        raise ValueError(
            f"Could not detect hostname in LT_HPB_URL env var: {hpb_url}. "
            "Verify that it is a valid URL with a protocol and hostname."
        )


def check_modal_env_vars() -> None:
    """Check that required Modal environment variables are set.

    Raises:
        ValueError: If required variables are missing
    """
    required_vars = ("MODAL_WORKSPACE", "MODAL_KEY", "MODAL_SECRET")
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise ValueError(f"Missing Modal environment variables: {', '.join(missing_vars)}")


def get_hpb_settings() -> HPBSettings:
    """Get HPB settings from Nextcloud.

    Returns:
        HPBSettings with STUN/TURN server configuration

    Raises:
        Exception: If settings cannot be retrieved
    """
    check_hpb_env_vars()
    try:
        nc = NextcloudApp()
        settings = nc.ocs("GET", "/ocs/v2.php/apps/spreed/api/v3/signaling/settings")
        hpb_settings = HPBSettings(**settings)
        logger.debug(
            "HPB settings retrieved successfully",
            extra={
                "stun_servers": [s.urls for s in hpb_settings.stunservers],
                "turn_servers": [t.urls for t in hpb_settings.turnservers],
                "server": hpb_settings.server,
            },
        )
        return hpb_settings
    except Exception as e:
        raise Exception("Error getting HPB settings") from e


def sanitize_websocket_url(ws_url: str) -> str:
    """Sanitize WebSocket URL to ensure proper format.

    Args:
        ws_url: Input URL (may be http/https)

    Returns:
        Properly formatted WebSocket URL ending with /spreed
    """
    ws_url = re.sub(r"^http://", "ws://", ws_url)
    ws_url = re.sub(r"^https://", "wss://", ws_url)
    if not ws_url.removesuffix("/").endswith("/spreed"):
        ws_url = ws_url.removesuffix("/") + "/spreed"
    return ws_url


def is_hpb_configured() -> bool:
    """Check if HPB is configured.

    Returns:
        True if HPB environment variables are set
    """
    return bool(os.getenv("LT_HPB_URL") and os.getenv("LT_INTERNAL_SECRET"))


def is_modal_configured() -> bool:
    """Check if Modal is configured.

    Returns:
        True if Modal environment variables are set
    """
    return bool(
        os.getenv("MODAL_WORKSPACE")
        and os.getenv("MODAL_KEY")
        and os.getenv("MODAL_SECRET")
    )
