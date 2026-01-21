"""FastAPI application for Kyutai Transcription ExApp."""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, run_app, nc_app

from .constants import APP_ID, APP_PORT, APP_VERSION
from .livetypes import (
    HealthResponse,
    LanguageSetRequest,
    LeaveRequest,
    TranscribeRequest,
    TranscriptionProviderException,
)
from .models import DEFAULT_LANGUAGE, get_supported_languages, is_language_supported
from .service import Application
from .utils import is_hpb_configured, is_modal_configured

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Application instance
app_service = Application()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info(
        "Starting Kyutai Transcription ExApp",
        extra={
            "app_id": APP_ID,
            "version": APP_VERSION,
            "port": APP_PORT,
        },
    )

    # Check configuration
    if not is_hpb_configured():
        logger.warning(
            "HPB not configured. Set LT_HPB_URL and LT_INTERNAL_SECRET environment variables."
        )
    if not is_modal_configured():
        logger.warning(
            "Modal not configured. Set MODAL_WORKSPACE, MODAL_KEY, and MODAL_SECRET environment variables."
        )

    yield

    # Shutdown
    logger.info("Shutting down Kyutai Transcription ExApp")
    await app_service.shutdown()


# Create FastAPI app
app = FastAPI(
    title="Kyutai Live Transcription",
    description="Live transcription for Nextcloud Talk using Kyutai STT on Modal",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Add Nextcloud AppAPI authentication middleware (exclude endpoints AppAPI calls during setup)
# Note: /init needs auth to call set_init_status(), so don't exclude it
app.add_middleware(AppAPIAuthMiddleware, disable_for=["heartbeat", "enabled"])


@app.get("/heartbeat")
async def heartbeat():
    """Health check endpoint excluded from AppAPI authentication."""
    return {"status": "ok"}


@app.post("/init")
async def init(nc: NextcloudApp = Depends(nc_app)):
    """Initialization endpoint called by AppAPI after deployment."""
    logger.info("Init endpoint called")
    # Signal initialization complete to AppAPI
    nc.set_init_status(100)
    logger.info("Init complete, status set to 100%")
    return {}


@app.put("/enabled")
async def set_enabled(enabled: int = 1):
    """Enable/disable callback from AppAPI."""
    logger.info(f"App {'enabled' if enabled else 'disabled'} by Nextcloud")
    return {}


@app.exception_handler(TranscriptionProviderException)
async def transcription_exception_handler(
    request: Request, exc: TranscriptionProviderException
):
    """Handle transcription provider exceptions."""
    return JSONResponse(
        status_code=exc.retcode,
        content={"error": str(exc)},
    )


@app.get("/enabled")
async def enabled():
    """Check if the app is enabled and configured."""
    return {"enabled": is_hpb_configured() and is_modal_configured()}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return HealthResponse(
        status="ok",
        version=APP_VERSION,
        modal_configured=is_modal_configured(),
        hpb_configured=is_hpb_configured(),
    )


@app.get("/capabilities")
async def capabilities():
    """Return app capabilities for Nextcloud."""
    return {
        "kyutai_transcription": {
            "version": APP_VERSION,
            "languages": get_supported_languages(),
            "features": ["streaming", "vad"],
        }
    }


@app.get("/api/v1/languages")
async def get_languages():
    """Get available transcription languages."""
    return {"languages": get_supported_languages()}


@app.post("/api/v1/call/transcribe")
async def transcribe(request: TranscribeRequest):
    """Start or stop transcription for a participant.

    Args:
        request: Transcription request with room token, session ID, and enable flag

    Returns:
        Status response
    """
    logger.info(
        "Transcription request received",
        extra={
            "room_token": request.roomToken,
            "nc_session_id": request.ncSessionId,
            "enable": request.enable,
            "lang_id": request.langId,
        },
    )

    if not is_hpb_configured():
        raise HTTPException(
            status_code=503,
            detail="HPB not configured. Set LT_HPB_URL and LT_INTERNAL_SECRET.",
        )

    if not is_modal_configured():
        raise HTTPException(
            status_code=503,
            detail="Modal not configured. Set MODAL_WORKSPACE, MODAL_KEY, and MODAL_SECRET.",
        )

    # Validate language
    lang_id = request.langId or DEFAULT_LANGUAGE
    if not is_language_supported(lang_id):
        logger.warning(
            "Unsupported language requested, using default",
            extra={
                "requested": lang_id,
                "default": DEFAULT_LANGUAGE,
            },
        )
        lang_id = DEFAULT_LANGUAGE

    try:
        await app_service.transcript_req(
            room_token=request.roomToken,
            nc_session_id=request.ncSessionId,
            enable=request.enable,
            lang_id=lang_id,
        )
    except TranscriptionProviderException:
        raise
    except Exception as e:
        logger.exception(
            "Error handling transcription request",
            exc_info=e,
            extra={
                "room_token": request.roomToken,
                "nc_session_id": request.ncSessionId,
            },
        )
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "status": "ok",
        "enabled": request.enable,
        "language": lang_id,
    }


@app.post("/api/v1/call/set-language")
async def set_language(request: LanguageSetRequest):
    """Set the transcription language for a room.

    Args:
        request: Language set request with room token and language ID

    Returns:
        Status response
    """
    logger.info(
        "Language change request received",
        extra={
            "room_token": request.roomToken,
            "lang_id": request.langId,
        },
    )

    if not is_language_supported(request.langId):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language: {request.langId}. Supported: {list(get_supported_languages())}",
        )

    try:
        await app_service.set_language(
            room_token=request.roomToken,
            lang_id=request.langId,
        )
    except TranscriptionProviderException:
        raise
    except Exception as e:
        logger.exception(
            "Error setting language",
            exc_info=e,
            extra={"room_token": request.roomToken},
        )
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "status": "ok",
        "language": request.langId,
    }


@app.post("/api/v1/call/leave")
async def leave_call(request: LeaveRequest):
    """Explicitly leave a call.

    Args:
        request: Leave request with room token

    Returns:
        Status response
    """
    logger.info(
        "Leave call request received",
        extra={"room_token": request.roomToken},
    )

    try:
        await app_service.leave_call(room_token=request.roomToken)
    except Exception as e:
        logger.exception(
            "Error leaving call",
            exc_info=e,
            extra={"room_token": request.roomToken},
        )
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "ok"}


@app.get("/api/v1/status")
async def status():
    """Get current status of the transcription service."""
    return {
        "active_rooms": app_service.get_active_rooms(),
        "version": APP_VERSION,
        "modal_configured": is_modal_configured(),
        "hpb_configured": is_hpb_configured(),
    }


if __name__ == "__main__":
    run_app(app, log_level="info")
