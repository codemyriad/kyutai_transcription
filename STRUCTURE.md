# Project Structure

This document explains how the Kyutai Transcription ExApp is built and how it interacts with Nextcloud Talk.

## Directory Layout

```
kyutai_transcription/
├── appinfo/
│   └── info.xml              # ExApp metadata for Nextcloud AppAPI
├── ex_app/
│   └── lib/
│       ├── main.py           # FastAPI application & endpoints
│       ├── service.py        # Application service layer
│       ├── spreed_client.py  # HPB/Janus WebRTC client
│       ├── transcriber.py    # Modal STT integration
│       ├── audio_stream.py   # Audio processing utilities
│       ├── constants.py      # App configuration constants
│       ├── models.py         # Language models
│       ├── livetypes.py      # Pydantic models for API
│       └── utils.py          # Helper functions
├── tests/                    # Unit tests
├── .github/workflows/        # CI/CD (Docker build & publish)
├── Dockerfile                # Container definition
├── pyproject.toml            # Python package configuration
└── README.md                 # User documentation
```

## How It Works

### 1. ExApp Registration with Nextcloud

The app is a **Nextcloud External App (ExApp)** managed by the AppAPI framework:

1. **info.xml** declares the app metadata, Docker image location, and required environment variables
2. AppAPI pulls the Docker image and starts the container
3. AppAPI calls lifecycle endpoints: `/heartbeat`, `/init`, `/enabled`
4. The app registers its **capabilities** so Talk knows it provides live transcription

### 2. AppAPI Integration (main.py)

The FastAPI app implements these required endpoints for AppAPI:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/heartbeat` | GET | Health check (returns `{"status": "ok"}`) |
| `/init` | POST | Called after deployment, signals completion via `set_init_status(100)` |
| `/enabled` | GET | Returns whether app is properly configured |
| `/enabled` | PUT | Called when app is enabled/disabled |
| `/capabilities` | GET | **Critical**: Must include `"live_transcription"` in features for Talk to show the CC button |

### 3. Talk Integration

For Talk to show the transcription button, `/capabilities` must return:

```python
{
    "kyutai_transcription": {
        "version": "1.0.0",
        "features": ["live_transcription"],  # Required!
        "live_transcription": {
            "supported_languages": {...}
        }
    }
}
```

### 4. Transcription Flow

```
User enables CC in Talk
         │
         ▼
    POST /api/v1/call/transcribe
         │
         ▼
┌─────────────────────────────────┐
│        service.py               │
│   Application.transcript_req()  │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│      spreed_client.py           │
│  - Connect to HPB via WebSocket │
│  - Authenticate with INTERNAL   │
│    SECRET                       │
│  - Subscribe to audio via Janus │
│    WebRTC                       │
└────────────┬────────────────────┘
             │ Audio frames
             ▼
┌─────────────────────────────────┐
│      transcriber.py             │
│  - Resample 48kHz → 24kHz       │
│  - Encode to Opus               │
│  - Send to Modal via WebSocket  │
│  - Receive transcription tokens │
└────────────┬────────────────────┘
             │ Transcription text
             ▼
┌─────────────────────────────────┐
│      spreed_client.py           │
│  - Send transcript message to   │
│    HPB signaling                │
│  - HPB broadcasts to Talk UI    │
└─────────────────────────────────┘
```

## Key Components

### AppAPIAuthMiddleware

The `nc_py_api` library provides middleware that validates requests from Nextcloud:

```python
app.add_middleware(AppAPIAuthMiddleware, disable_for=["heartbeat", "enabled"])
```

Endpoints in `disable_for` can be called without authentication (needed during setup).

### HPB Connection (spreed_client.py)

The High-Performance Backend (HPB) provides:
- **Signaling server**: WebSocket connection for room events
- **Janus gateway**: WebRTC SFU for audio/video streams

Authentication uses `LT_INTERNAL_SECRET` with HMAC-SHA256 signatures.

### Modal Transcription (transcriber.py)

Audio is sent to Modal.com where the Kyutai Moshi model runs:
- WebSocket connection to `wss://{workspace}--kyutai-stt-*.modal.run/ws`
- Opus-encoded audio chunks sent in real-time
- Transcription tokens returned as they're generated

## Environment Variables

| Variable | Used By | Purpose |
|----------|---------|---------|
| `APP_ID` | main.py | App identifier for capabilities |
| `APP_VERSION` | main.py | Version string |
| `APP_PORT` | Dockerfile | Port to listen on |
| `LT_HPB_URL` | spreed_client.py | HPB signaling WebSocket URL |
| `LT_INTERNAL_SECRET` | spreed_client.py | HPB authentication |
| `MODAL_WORKSPACE` | transcriber.py | Modal workspace name |
| `MODAL_KEY` | transcriber.py | Modal proxy auth key |
| `MODAL_SECRET` | transcriber.py | Modal proxy auth secret |

## CI/CD Pipeline

The GitHub Actions workflow (`.github/workflows/docker-publish.yml`):

1. **test**: Runs pytest on unit tests
2. **docker-smoke-test**: Builds image, starts container, verifies `/heartbeat` returns 200
3. **build-and-push**: Builds multi-arch image (amd64/arm64), pushes to ghcr.io

## Common Issues

### "Heartbeat check failed" in Nextcloud UI
- `/heartbeat` must return `{"status": "ok"}` (not empty string)

### "0% initializing" stuck
- `/init` must call `nc.set_init_status(100)` in a background task

### No CC button in Talk
- `/capabilities` must include `"live_transcription"` in features array

### Environment variables not passed
- `info.xml` must use `<environment-variables>` (hyphen) not `<environment>` (underscore)
