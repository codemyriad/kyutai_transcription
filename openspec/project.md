# Project Context

## Purpose

Kyutai Live Transcription for Nextcloud Talk - a real-time speech-to-text solution that integrates with Nextcloud Talk video calls using Kyutai's streaming STT model deployed on Modal.com.

**Key Goals:**
- Low latency (~0.5 second first-token latency) streaming transcription
- GPU-accelerated inference on Modal.com's serverless infrastructure (A10G/A100)
- Automatic scaling from zero with pay-per-second billing
- Multi-language support (English and French via `kyutai/stt-1b-en_fr`)

**How it works:**
1. Connects to Nextcloud's High-Performance Backend (HPB) signaling server via WebSocket
2. Receives participant audio via WebRTC from Janus SFU
3. Sends audio to Modal's Kyutai STT inference service
4. Broadcasts transcriptions back to call participants as closed captions

## Tech Stack

**Language & Framework:**
- Python 3.11+ (requires `>=3.11`)
- FastAPI 0.110+ (web framework)
- Uvicorn 0.27+ (ASGI server)

**Core Dependencies:**
- `nc_py_api` >=0.17.0 - Nextcloud AppAPI integration, authentication
- `websockets` >=12.0 - WebSocket communication with HPB and Modal
- `aiortc` >=1.9.0 - WebRTC peer connections for audio reception
- `numpy` >=1.26.0 - Audio data processing
- `scipy` >=1.12.0 - High-quality audio resampling
- `pydantic` >=2.5.0 - Request/response validation
- `av` >=12.0.0 - PyAV for audio codec operations (Opus encoding)

**Development & Testing:**
- `pytest` >=8.0.0 with `pytest-asyncio` >=0.23.0
- `pytest-cov` >=4.1.0 for coverage
- `ruff` >=0.2.0 for linting/formatting
- `mypy` >=1.8.0 for static type checking
- `playwright` (optional) for UI testing and audio streaming helpers

**Container:**
- Docker (Python 3.12-slim base)
- System deps: libavcodec, libopus, libvpx, libsrtp2

## Project Conventions

### Code Style

- **Linter/Formatter:** Ruff with configuration in `pyproject.toml`
- **Type Checking:** mypy in strict mode
- **Naming conventions:**
  - `room_token` - Nextcloud Talk call identifier
  - `nc_session_id` - Participant's Nextcloud session ID
  - `lang_id` - Language code (en/fr)
  - `resumeid` - HPB session resume token
- **Logging:** Structured logging with `extra={}` dicts, logs to stdout

### Architecture Patterns

**Component Architecture:**
```
User enables CC in Talk
        │
        ▼
    POST /api/v1/call/transcribe
        │
        ▼
┌─────────────────────────────────┐
│  Application (service.py)       │ Manages SpreedClient lifecycle
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  SpreedClient (spreed_client.py)│ WebSocket to HPB, WebRTC SFU
└────────────┬────────────────────┘
             │ Audio frames
             ▼
┌─────────────────────────────────┐
│  ModalTranscriber (transcriber) │ WebSocket to Modal GPU
└────────────┬────────────────────┘
             │ wss://
             ▼
    Modal.com Kyutai STT Inference
```

**Key Design Elements:**
- **AppAPI Integration:** Uses `nc_py_api` middleware with HMAC-SHA256 validation
- **Async Architecture:** Full async/await with `asyncio` for concurrent call handling
- **Memory Watchdog:** Monitors RSS memory, estimates capacity, triggers shutdown if constrained
- **Error Handling:** Custom exception hierarchy (HPBConnectionError, ModalConnectionError, etc.)

**Async Patterns:**
- `asyncio.Lock()` for critical sections
- `asyncio.Queue` for inter-task communication
- `threading.Event` for shutdown signaling
- Background tasks for callbacks

### Testing Strategy

- **Framework:** pytest with `pytest-asyncio` in auto mode
- **Coverage:** pytest-cov for coverage reporting
- **Test location:** `/tests/` directory
- **Patterns:** Mock/patch for isolation, numpy arrays for audio tests

**CI/CD Pipeline (GitHub Actions):**
1. `test` - Run pytest unit tests
2. `docker-smoke-test` - Build image, verify `/heartbeat` returns 200
3. `build-and-push` - Multi-arch build (amd64/arm64) to ghcr.io

### Git Workflow

- Branch naming: Feature branches (e.g., `nc_kyutai_live_transcriptions-experiments`)
- Commit messages: Imperative mood, concise descriptions
- CI required to pass before merge

## Domain Context

**Audio Processing Pipeline:**
- WebRTC delivers 48kHz stereo audio
- Resampled to 24kHz mono (Kyutai model expectation)
- Encoded as raw float32 PCM (32-bit LE, [-1.0, 1.0] range)
- Buffered in 2-second chunks (OPUS_CHUNK_MS = 2000)

**Nextcloud AppAPI Lifecycle:**
- `/heartbeat` - Health check (excluded from auth)
- `/init` - Called after deployment, signals completion via `set_init_status(100)`
- `/enabled` - Configuration check and enable/disable callbacks
- `/capabilities` - Declares `"live_transcription"` feature for Talk UI

**HPB Authentication:**
- HMAC-SHA256 signatures with `LT_INTERNAL_SECRET`
- Session resumption for network reconnections

## Important Constraints

**Memory Management:**
- Estimates 100MB per active transcriber
- Checks available system memory before accepting new requests
- Gracefully shuts down transcribers if memory constrained
- Detects container vs host environment for proper limit calculation

**Language Support:**
- Currently limited to English and French (Kyutai model limitation)
- Validated before transcription starts

**Modal Cold Start:**
- 120s timeout for Modal cold starts
- Exponential backoff for connection retries (base=2, max 5 attempts)

## External Dependencies

**Nextcloud Integration:**
- **AppAPI framework** - ExApp lifecycle management
- **HPB (High-Performance Backend)** - WebSocket signaling for room events
- **Talk app (spreed)** - Closed captions capability discovery
- **OCS API** - Settings retrieval (`/ocs/v2.php/apps/spreed/api/v3/signaling/settings`)

**Modal.com Integration:**
- **Workspace authentication** - Key + secret proxy auth
- **WebSocket endpoint:** `wss://{workspace}--kyutai-stt-rust-kyutaisttrustservice-serve.modal.run/v1/stream`
- **Protocol:** Binary WebSocket frames for audio, JSON for transcription results
- **Response format:** `{"text": "...", "final": bool, "vad_end": bool}`

**WebRTC/Janus:**
- STUN/TURN servers from HPB settings
- RTCPeerConnection for receiving audio tracks
- JSEP for offer/answer negotiation

**Environment Variables (Required):**
- `LT_HPB_URL` - HPB signaling server URL
- `LT_INTERNAL_SECRET` - HMAC secret for Nextcloud validation
- `MODAL_WORKSPACE` - Modal.com workspace name
- `MODAL_KEY` - Modal API key
- `MODAL_SECRET` - Modal API secret

**Environment Variables (Optional):**
- `SKIP_CERT_VERIFY` - For self-signed certs
- `APP_ID` - Hardcoded to `live_transcription`
