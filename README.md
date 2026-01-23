# Kyutai Live Transcription for Nextcloud Talk

Real-time live transcription for Nextcloud Talk video calls using [Kyutai's streaming speech-to-text model](https://github.com/kyutai-labs/moshi) deployed on [Modal.com](https://modal.com).

## Features

- **Low latency**: ~0.5 second first-token latency with streaming transcription
- **GPU-accelerated**: Runs on Modal.com's GPU infrastructure
- **Automatic scaling**: Scales from zero with Modal's serverless architecture
- **Cost-effective**: Pay only for what you use with per-second billing
- **Multi-language**: Supports English and French

## Important: App ID Requirement

This app **must** use the app ID `live_transcription` because Nextcloud Talk is hardcoded to look for an ExApp with exactly that ID when enabling the CC (closed captions) button. We would prefer to use a unique, non-conflicting name like `live_transcription`, but Talk's `LiveTranscriptionService` specifically queries for `getExApp('live_transcription')`.

**This means:**
- This app cannot be installed alongside Nextcloud's official [live_transcription](https://github.com/nextcloud/live_transcription) app
- You must choose one or the other as your live transcription provider
- If you have the official app installed, unregister it first before installing this one

## Prerequisites

1. **Nextcloud 30+** with the following:
   - Talk app (spreed) 18+
   - High-Performance Backend (HPB) configured
   - AppAPI app installed

2. **Modal.com account** with:
   - Kyutai STT service deployed (see [kyutai_modal](https://github.com/codemyriad/kyutai_modal))
   - Proxy authentication token created

## Installation

### Step 1: Deploy Kyutai STT on Modal

First, deploy the Kyutai STT service on Modal. See the [kyutai_modal repository](https://github.com/codemyriad/kyutai_modal) for instructions.

```bash
git clone https://github.com/codemyriad/kyutai_modal.git
cd kyutai_modal
uvx modal deploy src/stt/modal_app.py
```

### Step 2: Get Modal Credentials

1. Go to your [Modal dashboard](https://modal.com/settings)
2. Note your **workspace name** from the URL (e.g., `user-myworkspace`)
3. Go to **Settings** → **Proxy Auth Tokens** → **Create Token**
4. Save the generated **key** and **secret**

### Step 3: Register the ExApp in Nextcloud

#### For Nextcloud AIO (All-in-One)

Nextcloud AIO comes with a pre-configured Docker daemon called `docker_aio`. Register the ExApp from inside the Nextcloud container:

```bash
docker exec --user www-data -it nextcloud-aio-nextcloud php occ app_api:app:register \
    live_transcription docker_aio \
    --info-xml https://raw.githubusercontent.com/codemyriad/live_transcription/main/appinfo/info.xml \
    --env "LT_HPB_URL=wss://your-nextcloud-domain/standalone-signaling/spreed" \
    --env "LT_INTERNAL_SECRET=your-hpb-internal-secret" \
    --env "MODAL_WORKSPACE=your-modal-workspace" \
    --env "MODAL_KEY=your-modal-key" \
    --env "MODAL_SECRET=your-modal-secret" \
    --wait-finish
```

#### For Other Nextcloud Installations

First, check if you already have a Docker deploy daemon registered:

```bash
occ app_api:daemon:list
```

If you see a daemon with type `docker-install`, note its name and use it in the command below.

If no Docker daemon is configured, register one first:

```bash
occ app_api:daemon:register docker_local "Docker Local" \
    docker-install http /var/run/docker.sock http://localhost
```

Then register the ExApp (replace `docker_local` with your daemon name if different):

```bash
occ app_api:app:register live_transcription docker_local \
    --info-xml https://raw.githubusercontent.com/codemyriad/live_transcription/main/appinfo/info.xml \
    --env "LT_HPB_URL=wss://your-nextcloud-domain/standalone-signaling/spreed" \
    --env "LT_INTERNAL_SECRET=your-hpb-internal-secret" \
    --env "MODAL_WORKSPACE=your-modal-workspace" \
    --env "MODAL_KEY=your-modal-key" \
    --env "MODAL_SECRET=your-modal-secret" \
    --wait-finish
```

#### Installing via Nextcloud App Store (Future)

Once this app is published to the Nextcloud App Store, you'll be able to install it via **Settings → Apps → External Apps**. However, you'll still need to configure the environment variables (Modal credentials, HPB settings) via the AppAPI settings page after installation.

> **Note**: This app is not yet published to the Nextcloud App Store. For now, use the command-line installation above

## Configuration

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `LT_HPB_URL` | WebSocket URL to HPB signaling server | `wss://nextcloud.example.com/standalone-signaling/spreed` |
| `LT_INTERNAL_SECRET` | HPB internal secret for authentication | `your-24-char-secret` |
| `MODAL_WORKSPACE` | Your Modal workspace name | `user-myworkspace` |
| `MODAL_KEY` | Modal proxy authentication key | `key_...` |
| `MODAL_SECRET` | Modal proxy authentication secret | `secret_...` |

### Optional Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `APP_ID` | Application identifier (must be `live_transcription`) | `live_transcription` |
| `APP_VERSION` | Application version | `1.0.0` |
| `APP_PORT` | Port to listen on | `23000` |
| `SKIP_CERT_VERIFY` | Skip SSL certificate verification | `false` |

## Usage

Once installed, the transcription feature will be available in Nextcloud Talk:

1. Join a video call in Nextcloud Talk
2. Click on the **CC** (closed captions) button in the call controls
3. Select your preferred language
4. Transcriptions will appear in real-time as participants speak

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Nextcloud Talk UI                             │
│                  (Enable transcription)                          │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP API
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              Kyutai Transcription ExApp                          │
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │                  FastAPI Application                     │   │
│   │    /api/v1/call/transcribe    /api/v1/call/set-language │   │
│   └─────────────────────────────────────────────────────────┘   │
│                         │                                        │
│   ┌─────────────────────▼─────────────────────────────────────┐ │
│   │                   SpreedClient                             │ │
│   │    - Connects to HPB via WebSocket                        │ │
│   │    - Receives audio via WebRTC                            │ │
│   │    - Manages peer connections                             │ │
│   └─────────────────────┬─────────────────────────────────────┘ │
│                         │ Audio                                  │
│   ┌─────────────────────▼─────────────────────────────────────┐ │
│   │                ModalTranscriber                            │ │
│   │    - Resamples audio (48kHz → 24kHz)                      │ │
│   │    - Encodes to Opus                                      │ │
│   │    - Sends to Modal via WebSocket                         │ │
│   └─────────────────────┬─────────────────────────────────────┘ │
└─────────────────────────┼───────────────────────────────────────┘
                          │ wss://
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Modal.com (GPU Cloud)                           │
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │               Kyutai STT Service                         │   │
│   │    - Decodes Opus audio                                  │   │
│   │    - Runs Moshi streaming inference                      │   │
│   │    - Returns transcription tokens                        │   │
│   └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/enabled` | GET | Check if app is configured |
| `/capabilities` | GET | Get app capabilities |
| `/api/v1/languages` | GET | Get supported languages |
| `/api/v1/call/transcribe` | POST | Start/stop transcription |
| `/api/v1/call/set-language` | POST | Change transcription language |
| `/api/v1/call/leave` | POST | Leave a call |
| `/api/v1/status` | GET | Get service status |

## Development

### Local Development

1. Clone the repository:

```bash
git clone https://github.com/codemyriad/live_transcription.git
cd nc_kyutai_live_transcriptions
```

2. Create a virtual environment:

```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

3. Set environment variables:

```bash
export LT_HPB_URL=wss://your-hpb/spreed
export LT_INTERNAL_SECRET=your-secret
export MODAL_WORKSPACE=your-workspace
export MODAL_KEY=your-key
export MODAL_SECRET=your-secret
```

4. Run the application:

```bash
cd ex_app/lib
python -m uvicorn main:app --reload --port 23000
```

### Running Tests

Prefer using [uv](https://github.com/astral-sh/uv) to isolate deps quickly:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -v
```

If you already have a venv active:

```bash
pytest tests/ -v
```

### Building Docker Image

```bash
docker build -t live_transcription:dev .
```

## Troubleshooting

### No transcriptions appearing

1. Check that HPB is properly configured and accessible
2. Verify Modal credentials are correct
3. Check the container logs: `docker logs nc_app_live_transcription`
4. Verify the HPB internal secret matches

### "Failed to connect to HPB"

- Ensure `LT_HPB_URL` is correct (should end with `/spreed`)
- Check that `LT_INTERNAL_SECRET` matches the HPB configuration
- If using self-signed certificates, set `SKIP_CERT_VERIFY=true`

### "Modal not configured"

- Ensure all three Modal environment variables are set:
  - `MODAL_WORKSPACE`
  - `MODAL_KEY`
  - `MODAL_SECRET`
- Verify the Kyutai STT service is deployed on Modal

### High latency

- Check Modal GPU selection (A10G or A100 recommended)
- Ensure good network connectivity to Modal
- Monitor Modal logs for any issues

## License

AGPL-3.0-or-later

## Credits

- [Kyutai](https://kyutai.org/) for the Moshi streaming STT model
- [Modal](https://modal.com/) for the serverless GPU infrastructure
- [Nextcloud](https://nextcloud.com/) for the collaboration platform
