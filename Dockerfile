# Kyutai Live Transcription ExApp for Nextcloud
# This container connects to HPB and forwards audio to Modal for transcription

FROM python:3.12-slim

# Labels for GitHub Container Registry
LABEL org.opencontainers.image.source="https://github.com/codemyriad/kyutai_transcription"
LABEL org.opencontainers.image.description="Live transcription for Nextcloud Talk using Kyutai STT on Modal"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies for aiortc and audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Audio/video processing
    libavcodec-dev \
    libavformat-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libavfilter-dev \
    # For aiortc/WebRTC
    libvpx-dev \
    libopus-dev \
    libsrtp2-dev \
    # Build tools
    pkg-config \
    gcc \
    # Networking
    curl \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ex_app/ /app/ex_app/
COPY appinfo/ /app/appinfo/

# Set working directory to lib for running the app
WORKDIR /app/ex_app/lib

# Default environment variables
ENV APP_ID="kyutai_transcription" \
    APP_VERSION="1.0.0" \
    APP_PORT="23000" \
    APP_HOST="0.0.0.0"

# Expose the application port
EXPOSE 23000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:23000/health || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "23000"]
