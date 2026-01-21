# Building a Custom Live Caption ExApp for Nextcloud Talk

A comprehensive guide for developers who want to create their own transcription/captioning service that integrates with Nextcloud Talk.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Architecture](#architecture)
4. [Setting Up the Development Environment](#setting-up-the-development-environment)
5. [Building the ExApp](#building-the-exapp)
6. [Connecting to HPB](#connecting-to-hpb)
7. [Receiving Audio Streams](#receiving-audio-streams)
8. [Sending Captions Back](#sending-captions-back)
9. [Integrating with Talk Frontend](#integrating-with-talk-frontend)
10. [Deployment](#deployment)
11. [Testing](#testing)
12. [Reference Implementation](#reference-implementation)

---

## Overview

### What You're Building

A Nextcloud External App (ExApp) that:
1. Connects to the Talk High-Performance Backend (HPB) as an internal service
2. Subscribes to audio streams from call participants via WebRTC
3. Transcribes audio using your speech-to-text engine of choice
4. Sends transcription text back to participants as real-time captions

### Why an ExApp?

Nextcloud's AppAPI framework allows apps to run as external Docker containers while integrating with Nextcloud's authentication, capabilities, and UI systems. For live transcription, this approach:

- Keeps heavy ML workloads outside the PHP process
- Allows use of Python/Go/etc. for the transcription engine
- Enables GPU acceleration if needed
- Integrates with Talk's caption UI automatically

---

## Prerequisites

### Required Knowledge

- Python (or your preferred language)
- WebSocket protocol basics
- WebRTC concepts (streams, tracks, SDP)
- Docker containerization
- Basic understanding of Nextcloud app structure

### Required Infrastructure

| Component | Purpose | Required Version |
|-----------|---------|------------------|
| Nextcloud Server | Base platform | 28+ recommended |
| Talk (spreed) app | Video calling | 18+ (with live transcription support) |
| High-Performance Backend | Signaling + WebRTC | Latest (Sept 2025+) |
| AppAPI app | ExApp management | Latest |
| Docker | Container runtime | 20+ |

### Development Tools

```bash
# Python development
python >= 3.10
pip install fastapi uvicorn websockets aiortc numpy

# Or use the official requirements
pip install nc_py_api  # Nextcloud Python API client
```

---

## Architecture

### Component Interaction

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Nextcloud Server                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────┐ │
│  │ Talk App    │───▶│ AppAPI      │───▶│ Your ExApp (Docker)     │ │
│  │ (PHP)       │    │ Framework   │    │                         │ │
│  └─────────────┘    └─────────────┘    │  ┌───────────────────┐  │ │
│        │                               │  │ FastAPI Server    │  │ │
│        │ capabilities                  │  │ - /transcribeCall │  │ │
│        ▼                               │  │ - /stopTranscribe │  │ │
│  ┌─────────────┐                       │  │ - /languages      │  │ │
│  │ Talk        │                       │  └─────────┬─────────┘  │ │
│  │ Frontend    │                       │            │            │ │
│  │ (Vue.js)    │                       │  ┌─────────▼─────────┐  │ │
│  └──────┬──────┘                       │  │ HPB Client        │  │ │
│         │                              │  │ (WebSocket)       │  │ │
│         │ WebSocket                    │  └─────────┬─────────┘  │ │
└─────────┼──────────────────────────────┼────────────┼────────────┘ │
          │                              │            │              │
          ▼                              │            ▼              │
┌─────────────────────────────────────┐  │  ┌─────────────────────┐ │
│     High-Performance Backend        │◀─┼──│ WebRTC/Janus Client │ │
│  ┌─────────────┐  ┌──────────────┐  │  │  │ (audio subscriber)  │ │
│  │ Signaling   │  │ Janus WebRTC │  │  │  └─────────────────────┘ │
│  │ Server (Go) │  │ Gateway      │  │  │            │             │
│  └─────────────┘  └──────────────┘  │  │            ▼             │
└─────────────────────────────────────┘  │  ┌─────────────────────┐ │
                                         │  │ Transcription       │ │
                                         │  │ Engine (Vosk/       │ │
                                         │  │ Whisper/etc.)       │ │
                                         │  └─────────────────────┘ │
                                         └─────────────────────────┘
```

### Message Flow

```
1. User joins call, enables captions
   Talk Frontend ──▶ Nextcloud Backend ──▶ Your ExApp /transcribeCall

2. Your ExApp connects to HPB
   ExApp ──WebSocket──▶ HPB Signaling Server (authenticate with INTERNAL_SECRET)

3. Your ExApp subscribes to audio
   ExApp ──▶ Janus Gateway ──▶ Receives WebRTC audio streams

4. Transcription happens
   Audio ──▶ Your STT Engine ──▶ Text

5. Captions sent back
   ExApp ──signaling message──▶ HPB ──▶ Talk Frontend (renders captions)
```

---

## Setting Up the Development Environment

### 1. Set Up Nextcloud with Talk + HPB

Use Docker Compose for a complete dev environment:

```yaml
# docker-compose.yml
version: '3.8'

services:
  # Nextcloud with Talk
  nextcloud:
    image: nextcloud:latest
    ports:
      - "8080:80"
    volumes:
      - nextcloud_data:/var/www/html
    environment:
      - NEXTCLOUD_ADMIN_USER=admin
      - NEXTCLOUD_ADMIN_PASSWORD=admin

  # High-Performance Backend (includes Janus, NATS, signaling)
  hpb:
    image: ghcr.io/nextcloud-releases/aio-talk:latest
    ports:
      - "3478:3478/tcp"
      - "3478:3478/udp"
      - "8081:8081/tcp"
    environment:
      - NC_DOMAIN=localhost:8080
      - TALK_PORT=3478
      - TURN_SECRET=turn-secret-min-24-chars-long
      - SIGNALING_SECRET=signaling-secret-24-chars
      - INTERNAL_SECRET=internal-secret-24-chars

  # Your ExApp (development)
  transcription:
    build: ./your-exapp
    ports:
      - "23000:23000"
    environment:
      - APP_ID=your_transcription_app
      - APP_SECRET=your-app-secret
      - APP_HOST=0.0.0.0
      - APP_PORT=23000
      - LT_HPB_URL=ws://hpb:8081/spreed
      - LT_INTERNAL_SECRET=internal-secret-24-chars
    volumes:
      - ./your-exapp:/app

volumes:
  nextcloud_data:
```

### 2. Configure Talk to Use HPB

In Nextcloud admin settings (Settings → Talk):

- **STUN server:** `stun:localhost:3478`
- **TURN server:** `turn:localhost:3478` with secret `turn-secret-min-24-chars-long`
- **High-performance backend:** `http://localhost:8081/` with secret `signaling-secret-24-chars`

### 3. Install AppAPI

```bash
docker exec -u www-data nextcloud php occ app:install app_api

# Register a Docker deploy daemon (adjust for your setup)
docker exec -u www-data nextcloud php occ app_api:daemon:register \
    docker_dev "Docker Dev" docker-install http host.docker.internal http://localhost:8080
```

---

## Building the ExApp

### Project Structure

```
your-transcription-app/
├── appinfo/
│   └── info.xml              # ExApp metadata
├── ex_app/
│   └── lib/
│       ├── main.py           # FastAPI application entry point
│       ├── hpb_client.py     # HPB WebSocket client
│       ├── webrtc_client.py  # Janus/WebRTC audio subscriber
│       ├── transcriber.py    # Your STT engine wrapper
│       └── signaling.py      # Signaling message handlers
├── Dockerfile
├── requirements.txt
└── README.md
```

### appinfo/info.xml

```xml
<?xml version="1.0"?>
<info xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:noNamespaceSchemaLocation="https://apps.nextcloud.com/schema/apps/info.xsd">
    <id>your_transcription_app</id>
    <name>Your Transcription App</name>
    <summary>Live transcription for Nextcloud Talk</summary>
    <description><![CDATA[
Provides live transcription/captions in Nextcloud Talk calls.
    ]]></description>
    <version>1.0.0</version>
    <licence>agpl</licence>
    <author>Your Name</author>
    <namespace>YourTranscriptionApp</namespace>
    <category>multimedia</category>
    <dependencies>
        <nextcloud min-version="28" max-version="32"/>
    </dependencies>
    
    <external-app>
        <docker-install>
            <registry>ghcr.io</registry>
            <image>your-org/your-transcription-app</image>
            <image-tag>latest</image-tag>
        </docker-install>
        <protocol>http</protocol>
        <system>false</system>
        <routes>
            <route>
                <url>.*</url>
                <verb>GET,POST,PUT,DELETE</verb>
                <access_level>USER</access_level>
                <headers_to_exclude>[]</headers_to_exclude>
            </route>
        </routes>
        <scopes>
            <value>TALK</value>
            <value>TALK_BOT</value>
        </scopes>
        <environment>
            <variable>
                <name>LT_HPB_URL</name>
                <display_name>HPB WebSocket URL</display_name>
                <description>WebSocket URL to the signaling server (wss://...)</description>
            </variable>
            <variable>
                <name>LT_INTERNAL_SECRET</name>
                <display_name>HPB Internal Secret</display_name>
                <description>INTERNAL_SECRET from HPB configuration</description>
            </variable>
        </environment>
    </external-app>
</info>
```

### requirements.txt

```
# Nextcloud integration
nc_py_api>=0.17.0

# Web framework
fastapi>=0.100.0
uvicorn>=0.23.0

# WebSocket & async
websockets>=11.0
aiohttp>=3.8.0

# WebRTC (for receiving audio)
aiortc>=1.6.0

# Audio processing
numpy>=1.24.0
scipy>=1.10.0

# Speech-to-text (choose your engine)
vosk>=0.3.45
# or: openai-whisper>=20230918
# or: faster-whisper>=0.10.0

# Utilities
python-dotenv>=1.0.0
pydantic>=2.0.0
```

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for audio processing
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Vosk model (or your preferred STT model)
RUN python -c "import vosk; vosk.Model(lang='en-us')" || true

# Copy application code
COPY ex_app/ /app/ex_app/

# Expose port
EXPOSE 23000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:23000/health || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "ex_app.lib.main:app", "--host", "0.0.0.0", "--port", "23000"]
```

---

## Connecting to HPB

### HPB Authentication

The HPB signaling server authenticates internal services using the `INTERNAL_SECRET`. Your ExApp must:

1. Connect via WebSocket to the HPB signaling endpoint
2. Send a `hello` message with internal authentication
3. Maintain the connection for sending/receiving messages

### ex_app/lib/hpb_client.py

```python
import asyncio
import json
import hashlib
import hmac
import time
from typing import Callable, Optional
import websockets
from websockets.client import WebSocketClientProtocol


class HPBClient:
    """Client for connecting to Nextcloud Talk High-Performance Backend."""
    
    def __init__(
        self,
        hpb_url: str,
        internal_secret: str,
        on_message: Optional[Callable] = None
    ):
        self.hpb_url = hpb_url
        self.internal_secret = internal_secret
        self.on_message = on_message
        self.ws: Optional[WebSocketClientProtocol] = None
        self.session_id: Optional[str] = None
        self._message_id = 0
        
    def _generate_auth(self, backend_url: str) -> dict:
        """Generate authentication params for HPB internal auth."""
        # Internal authentication uses HMAC-SHA256
        timestamp = str(int(time.time()))
        random_data = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:16]
        
        # Create signature
        check_data = f"{backend_url}:{timestamp}:{random_data}"
        signature = hmac.new(
            self.internal_secret.encode(),
            check_data.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return {
            "type": "internal",
            "params": {
                "backend": backend_url,
                "timestamp": timestamp,
                "random": random_data,
                "token": signature
            }
        }
    
    def _next_message_id(self) -> str:
        self._message_id += 1
        return f"msg-{self._message_id}"
    
    async def connect(self, backend_url: str = "https://nextcloud.local"):
        """Connect to HPB and authenticate."""
        self.ws = await websockets.connect(
            self.hpb_url,
            ping_interval=30,
            ping_timeout=10
        )
        
        # Send hello message with internal auth
        hello = {
            "id": self._next_message_id(),
            "type": "hello",
            "hello": {
                "version": "1.0",
                "auth": self._generate_auth(backend_url),
                "features": ["audio-video-permissions", "mcu", "simulcast"]
            }
        }
        
        await self.ws.send(json.dumps(hello))
        
        # Wait for hello response
        response = await self.ws.recv()
        data = json.loads(response)
        
        if data.get("type") == "error":
            raise Exception(f"HPB auth failed: {data.get('error', {}).get('message')}")
        
        if data.get("type") == "hello":
            self.session_id = data.get("hello", {}).get("sessionid")
            print(f"Connected to HPB with session: {self.session_id}")
        
        return self.session_id
    
    async def join_room(self, room_id: str, session_id: str):
        """Join a Talk room to receive events."""
        message = {
            "id": self._next_message_id(),
            "type": "room",
            "room": {
                "roomid": room_id,
                "sessionid": session_id
            }
        }
        await self.ws.send(json.dumps(message))
    
    async def send_transcription(
        self,
        room_id: str,
        recipient_session_id: str,
        speaker_session_id: str,
        text: str,
        is_final: bool = True
    ):
        """Send a transcription message to a participant."""
        message = {
            "id": self._next_message_id(),
            "type": "message",
            "message": {
                "recipient": {
                    "type": "session",
                    "sessionid": recipient_session_id
                },
                "data": {
                    "type": "transcription",
                    "transcription": {
                        "sessionId": speaker_session_id,
                        "text": text,
                        "final": is_final
                    }
                }
            }
        }
        await self.ws.send(json.dumps(message))
    
    async def listen(self):
        """Listen for incoming messages."""
        async for message in self.ws:
            data = json.loads(message)
            if self.on_message:
                await self.on_message(data)
    
    async def close(self):
        """Close the connection."""
        if self.ws:
            await self.ws.close()
```

---

## Receiving Audio Streams

### WebRTC via Janus

The HPB uses Janus as a Selective Forwarding Unit (SFU). To receive audio:

1. Subscribe to a participant's audio stream via Janus
2. Receive RTP packets containing Opus-encoded audio
3. Decode Opus to PCM for transcription

### ex_app/lib/webrtc_client.py

```python
import asyncio
from typing import Callable, Optional
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRecorder, MediaBlackhole
import numpy as np


class AudioSubscriber:
    """Subscribe to a participant's audio stream via Janus."""
    
    def __init__(self, on_audio: Callable[[np.ndarray, int], None]):
        """
        Args:
            on_audio: Callback receiving (audio_data, sample_rate)
        """
        self.on_audio = on_audio
        self.pc: Optional[RTCPeerConnection] = None
        self._audio_buffer = []
        
    async def subscribe(
        self,
        janus_url: str,
        room_id: str,
        publisher_id: str,
        session_id: str
    ):
        """Subscribe to a publisher's audio stream."""
        self.pc = RTCPeerConnection()
        
        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                asyncio.create_task(self._process_audio(track))
        
        # Create Janus subscriber session
        # Note: This is simplified - real implementation needs
        # Janus API calls to attach to videoroom and subscribe
        
        # For actual implementation, see:
        # https://janus.conf.meetecho.com/docs/videoroom.html
        
    async def _process_audio(self, track):
        """Process incoming audio frames."""
        while True:
            try:
                frame = await track.recv()
                
                # Convert to numpy array (16-bit PCM)
                audio_data = np.frombuffer(frame.to_ndarray(), dtype=np.int16)
                sample_rate = frame.sample_rate
                
                # Call the transcription callback
                if self.on_audio:
                    self.on_audio(audio_data, sample_rate)
                    
            except Exception as e:
                print(f"Audio processing error: {e}")
                break
    
    async def close(self):
        if self.pc:
            await self.pc.close()


class JanusClient:
    """Client for Janus WebRTC Gateway."""
    
    def __init__(self, janus_url: str):
        self.janus_url = janus_url
        self.session_id = None
        self.handle_id = None
        
    async def create_session(self):
        """Create a Janus session."""
        # POST to {janus_url}/janus
        # Returns session_id
        pass
    
    async def attach_plugin(self, plugin: str = "janus.plugin.videoroom"):
        """Attach to a Janus plugin."""
        # POST to {janus_url}/janus/{session_id}
        # Returns handle_id
        pass
    
    async def join_as_subscriber(self, room_id: int, feed_id: int):
        """Join a videoroom as a subscriber to receive streams."""
        # Send join request to Janus videoroom
        message = {
            "janus": "message",
            "body": {
                "request": "join",
                "room": room_id,
                "ptype": "subscriber",
                "feed": feed_id,
                "audio": True,
                "video": False  # We only need audio for transcription
            }
        }
        # Send via Janus API
        pass
    
    async def send_answer(self, sdp: str):
        """Send SDP answer to complete WebRTC handshake."""
        pass
```

### Simplified Audio Capture (Alternative)

If direct Janus integration is complex, you can use the HPB's built-in audio forwarding (if available) or request audio via the signaling protocol:

```python
async def request_audio_subscription(self, hpb_client: HPBClient, participant_session: str):
    """Request to receive audio from a participant via HPB."""
    message = {
        "id": self._next_message_id(),
        "type": "control",
        "control": {
            "recipient": {
                "type": "session", 
                "sessionid": participant_session
            },
            "data": {
                "type": "requestAudio",
                "requestAudio": {
                    "subscribe": True
                }
            }
        }
    }
    await hpb_client.ws.send(json.dumps(message))
```

---

## Sending Captions Back

### Transcription Message Format

Based on the Talk frontend expectations, transcription messages should follow this format:

```python
async def send_caption(
    self,
    hpb_client: HPBClient,
    room_id: str,
    target_sessions: list[str],  # Sessions that want captions
    speaker_session: str,         # Who is speaking
    text: str,
    is_partial: bool = False      # Intermediate vs final transcription
):
    """Send caption to participants via HPB signaling."""
    
    for session_id in target_sessions:
        message = {
            "id": self._next_message_id(),
            "type": "message",
            "message": {
                "recipient": {
                    "type": "session",
                    "sessionid": session_id
                },
                "data": {
                    "type": "transcript",
                    "transcript": {
                        "sessionId": speaker_session,
                        "text": text,
                        "partial": is_partial,
                        "timestamp": int(time.time() * 1000)
                    }
                }
            }
        }
        await hpb_client.ws.send(json.dumps(message))
```

### Handling Partial vs Final Transcriptions

Most STT engines provide partial (interim) results while processing, then a final result:

```python
class TranscriptionBuffer:
    """Buffer and manage transcription results."""
    
    def __init__(self, on_result: Callable):
        self.on_result = on_result
        self.partial_text = ""
        
    def add_partial(self, text: str, speaker_session: str):
        """Handle partial transcription result."""
        self.partial_text = text
        # Optionally send partial updates for real-time feel
        self.on_result(text, speaker_session, is_partial=True)
    
    def add_final(self, text: str, speaker_session: str):
        """Handle final transcription result."""
        self.partial_text = ""
        self.on_result(text, speaker_session, is_partial=False)
```

---

## Integrating with Talk Frontend

### Required API Endpoints

Your ExApp must implement these endpoints that Talk expects:

```python
# ex_app/lib/main.py

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import run_app, AppAPIAuthMiddleware

app = FastAPI()

# Add Nextcloud AppAPI authentication middleware
app.add_middleware(AppAPIAuthMiddleware)


class TranscribeCallRequest(BaseModel):
    roomToken: str
    sessionId: str
    language: str = "en"


class StopTranscribeRequest(BaseModel):
    roomToken: str
    sessionId: str


# Store active transcription sessions
active_sessions: dict[str, dict] = {}


@app.get("/languages")
async def get_languages():
    """Return available transcription languages."""
    return {
        "languages": [
            {"code": "en", "name": "English"},
            {"code": "de", "name": "German"},
            {"code": "es", "name": "Spanish"},
            {"code": "fr", "name": "French"},
            # Add languages your STT engine supports
        ]
    }


@app.post("/transcribeCall")
async def start_transcription(request: TranscribeCallRequest):
    """Start transcription for a participant in a call."""
    session_key = f"{request.roomToken}:{request.sessionId}"
    
    if session_key in active_sessions:
        return {"status": "already_active"}
    
    # Start transcription worker
    try:
        worker = await start_transcription_worker(
            room_token=request.roomToken,
            session_id=request.sessionId,
            language=request.language
        )
        active_sessions[session_key] = worker
        return {"status": "started"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stopTranscribe")
async def stop_transcription(request: StopTranscribeRequest):
    """Stop transcription for a participant."""
    session_key = f"{request.roomToken}:{request.sessionId}"
    
    if session_key not in active_sessions:
        return {"status": "not_found"}
    
    worker = active_sessions.pop(session_key)
    await worker.stop()
    
    return {"status": "stopped"}


@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration."""
    return {"status": "ok"}


# Run with Nextcloud ExApp framework
if __name__ == "__main__":
    run_app(app, log_level="info")
```

### Registering Capabilities

Your ExApp should register its capabilities so Talk knows it's available. This happens automatically through AppAPI when your app is installed, but you need the correct `info.xml` configuration.

---

## Deployment

### 1. Build and Push Docker Image

```bash
# Build
docker build -t ghcr.io/your-org/your-transcription-app:latest .

# Push to registry
docker push ghcr.io/your-org/your-transcription-app:latest
```

### 2. Register ExApp in Nextcloud

```bash
# If using Docker deploy daemon
occ app_api:app:register your_transcription_app docker_daemon \
    --info-xml /path/to/your/appinfo/info.xml \
    --env LT_HPB_URL=wss://your-hpb-domain/standalone-signaling/spreed \
    --env LT_INTERNAL_SECRET=your-internal-secret \
    --wait-finish

# Or for manual/development deployment
occ app_api:daemon:register manual_install "Manual Install" manual-install http localhost http://localhost

occ app_api:app:register your_transcription_app manual_install \
    --json-info '{
        "id": "your_transcription_app",
        "name": "Your Transcription App", 
        "daemon_config_name": "manual_install",
        "version": "1.0.0",
        "secret": "your-app-secret",
        "port": 23000,
        "scopes": ["TALK", "TALK_BOT"]
    }' \
    --wait-finish
```

### 3. Verify Installation

```bash
# Check ExApp status
occ app_api:app:list

# Check Talk capabilities
curl -u admin:password \
    "https://your-nextcloud/ocs/v2.php/cloud/capabilities?format=json" \
    | jq '.ocs.data.capabilities.spreed'
```

---

## Testing

### Unit Testing the Transcription Engine

```python
# tests/test_transcriber.py
import pytest
import numpy as np
from ex_app.lib.transcriber import Transcriber


@pytest.fixture
def transcriber():
    return Transcriber(model="vosk-model-small-en-us-0.15")


def test_transcribe_audio(transcriber):
    # Generate test audio (sine wave saying nothing)
    sample_rate = 16000
    duration = 1.0
    t = np.linspace(0, duration, int(sample_rate * duration))
    audio = (np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    
    result = transcriber.transcribe(audio, sample_rate)
    assert isinstance(result, str)
```

### Integration Testing with HPB

```python
# tests/test_hpb_integration.py
import pytest
import asyncio
from ex_app.lib.hpb_client import HPBClient


@pytest.mark.asyncio
async def test_hpb_connection():
    client = HPBClient(
        hpb_url="ws://localhost:8081/spreed",
        internal_secret="test-internal-secret"
    )
    
    try:
        session_id = await client.connect("https://localhost")
        assert session_id is not None
    finally:
        await client.close()
```

### End-to-End Testing

1. Start your ExApp locally
2. Join a Talk call in the browser
3. Enable captions
4. Speak into microphone
5. Verify captions appear

```bash
# Watch ExApp logs during testing
docker logs -f your-transcription-app
```

---

## Reference Implementation

The official `live_transcription` ExApp is the best reference:

**Repository:** https://github.com/nextcloud/live_transcription

**Key files to study:**

| File | Purpose |
|------|---------|
| `ex_app/lib/main.py` | FastAPI entry point, API endpoints |
| `ex_app/lib/vosk_server.py` | Vosk STT server implementation |
| `appinfo/info.xml` | ExApp metadata and configuration |
| `Dockerfile` | Container build configuration |

**Clone and explore:**

```bash
git clone https://github.com/nextcloud/live_transcription.git
cd live_transcription

# Study the structure
find . -name "*.py" -exec head -50 {} \;

# Look at the API endpoints
grep -r "@app\." ex_app/
```

---

## Troubleshooting

### Common Issues

| Problem | Cause | Solution |
|---------|-------|----------|
| "Failed to enable live transcription" | ExApp not registered correctly | Check `occ app_api:app:list` |
| No captions appearing | Signaling message format wrong | Check HPB logs, verify message schema |
| Audio not received | Janus subscription failed | Check Janus logs, verify WebRTC setup |
| Auth errors on HPB | Wrong INTERNAL_SECRET | Verify secret matches HPB config |
| ExApp not starting | Docker/port issues | Check `docker logs`, verify port mapping |

### Debug Logging

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Log all WebSocket messages
async def on_message(data):
    logger.debug(f"HPB message: {json.dumps(data, indent=2)}")
```

### Checking HPB Logs

```bash
# If using Docker
docker logs nc-talk 2>&1 | grep -i transcription

# If using systemd
journalctl -u nextcloud-spreed-signaling -f
```

---

## Next Steps

1. **Start simple:** Get HPB connection working first
2. **Add audio:** Implement Janus subscription
3. **Add STT:** Integrate your transcription engine
4. **Send captions:** Implement signaling message sending
5. **Polish:** Handle edge cases, add language selection
6. **Deploy:** Package as proper ExApp, publish to app store

Good luck building your transcription app! 🎤➡️📝
