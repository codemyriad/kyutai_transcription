"""Constants for Kyutai Transcription ExApp."""

import os

# App identification
APP_ID = os.getenv("APP_ID", "live_transcription")
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
APP_PORT = int(os.getenv("APP_PORT", "23000"))

# Modal configuration
MODAL_WORKSPACE = os.getenv("MODAL_WORKSPACE", "")
MODAL_KEY = os.getenv("MODAL_KEY", "")
MODAL_SECRET = os.getenv("MODAL_SECRET", "")
# Allow overriding the Modal host suffix in case the deployed app name changes
MODAL_STT_HOST_SUFFIX = os.getenv(
    "MODAL_STT_HOST_SUFFIX",
    "kyutai-stt-rust-kyutaisttrustservice-serve.modal.run",
)

# Construct Modal WebSocket URL
MODAL_STT_URL = (
    f"wss://{MODAL_WORKSPACE}--{MODAL_STT_HOST_SUFFIX}/v1/stream"
    if MODAL_WORKSPACE
    else ""
)

# HPB configuration
LT_HPB_URL = os.getenv("LT_HPB_URL", "")
LT_INTERNAL_SECRET = os.getenv("LT_INTERNAL_SECRET", "")
SKIP_CERT_VERIFY = os.getenv("SKIP_CERT_VERIFY", "").lower() in ("true", "1", "yes")

# Audio configuration (Kyutai expects 24kHz mono Opus)
KYUTAI_SAMPLE_RATE = 24000
KYUTAI_CHANNELS = 1
KYUTAI_FRAME_MS = 80  # Frame duration in ms
KYUTAI_FRAME_SAMPLES = int(KYUTAI_SAMPLE_RATE * KYUTAI_FRAME_MS / 1000)  # 1920 samples

# WebRTC audio typically comes as 48kHz
WEBRTC_SAMPLE_RATE = 48000

# Opus encoding parameters
OPUS_FRAME_MS = 40  # Standard Opus frame duration
OPUS_CHUNK_MS = 2000  # How much audio to buffer before sending

# Connection timeouts (seconds)
HPB_CONNECT_TIMEOUT = 60
HPB_PING_TIMEOUT = 120
HPB_SHUTDOWN_TIMEOUT = 30
MODAL_CONNECT_TIMEOUT = 120  # Allow time for Modal cold start
MODAL_STALE_TIMEOUT = 30  # Reconnect if no transcripts received for this long
CALL_LEAVE_TIMEOUT = 2  # Short delay - allows initial setup but stops quickly

# Retry configuration
MAX_CONNECTION_RETRIES = 5
RETRY_BACKOFF_BASE = 2  # Exponential backoff base

# Transcription settings
MIN_TRANSCRIPT_SEND_INTERVAL = 0.3  # Minimum interval between sending transcripts
MAX_AUDIO_FRAMES = 20  # Max frames to batch before processing

# Worker configuration
LT_MAX_WORKERS = int(os.getenv("LT_MAX_WORKERS", os.cpu_count() or 4))
