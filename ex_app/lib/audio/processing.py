"""Pure audio processing functions.

All functions in this module are pure (no side effects, no I/O).
They can be tested in isolation without mocking anything.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import numpy as np


@dataclass(frozen=True)
class AudioConfig:
    """Immutable audio configuration."""

    sample_rate: int
    channels: int

    # Common configurations
    WEBRTC_STEREO = None  # Forward reference, set below
    KYUTAI_MONO = None


# Set class attributes after class definition
AudioConfig.WEBRTC_STEREO = AudioConfig(sample_rate=48000, channels=2)
AudioConfig.KYUTAI_MONO = AudioConfig(sample_rate=24000, channels=1)


def stereo_to_mono(audio: np.ndarray) -> np.ndarray:
    """Convert stereo interleaved audio to mono by averaging channels.

    Args:
        audio: Interleaved stereo audio as int16 array [L, R, L, R, ...]

    Returns:
        Mono audio as int16 array

    Raises:
        ValueError: If audio has odd number of samples (not valid stereo)

    Example:
        >>> stereo = np.array([100, 200, 300, 400], dtype=np.int16)  # L=100,R=200, L=300,R=400
        >>> mono = stereo_to_mono(stereo)
        >>> mono  # [150, 350] - averaged
    """
    if len(audio) == 0:
        return audio

    if len(audio) % 2 != 0:
        raise ValueError(
            f"Stereo audio must have even number of samples, got {len(audio)}"
        )

    # Reshape to (N, 2) and average channels
    stereo_pairs = audio.reshape(-1, 2)
    mono = stereo_pairs.mean(axis=1).astype(np.int16)
    return mono


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample audio from source rate to target rate.

    Uses scipy.signal.resample for high-quality resampling.

    Args:
        audio: Input audio as numpy array (int16 or float32)
        source_rate: Source sample rate in Hz
        target_rate: Target sample rate in Hz

    Returns:
        Resampled audio (same dtype as input)

    Raises:
        ValueError: If rates are invalid

    Example:
        >>> audio_48k = np.zeros(4800, dtype=np.int16)  # 100ms at 48kHz
        >>> audio_24k = resample(audio_48k, 48000, 24000)
        >>> len(audio_24k)  # 2400 samples = 100ms at 24kHz
    """
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError(f"Sample rates must be positive, got {source_rate}, {target_rate}")

    if source_rate == target_rate:
        return audio

    if len(audio) == 0:
        return audio

    from scipy import signal

    original_dtype = audio.dtype
    num_samples = int(len(audio) * target_rate / source_rate)
    resampled = signal.resample(audio, num_samples)

    # Preserve original dtype
    if original_dtype == np.int16:
        return np.clip(resampled, -32768, 32767).astype(np.int16)
    return resampled.astype(original_dtype)


def int16_to_float32(audio: np.ndarray) -> np.ndarray:
    """Convert int16 PCM to float32 normalized to [-1.0, 1.0].

    This is the format Modal's Kyutai STT expects.

    Args:
        audio: Audio data as int16 array (range -32768 to 32767)

    Returns:
        Audio data as float32 array (range -1.0 to 1.0)

    Example:
        >>> audio_int16 = np.array([0, 16384, -16384, 32767], dtype=np.int16)
        >>> audio_float = int16_to_float32(audio_int16)
        >>> audio_float  # approximately [0.0, 0.5, -0.5, 1.0]
    """
    return audio.astype(np.float32) / 32768.0


def float32_to_int16(audio: np.ndarray) -> np.ndarray:
    """Convert float32 normalized audio back to int16.

    Inverse of int16_to_float32.

    Args:
        audio: Audio data as float32 array (range -1.0 to 1.0)

    Returns:
        Audio data as int16 array (range -32768 to 32767)

    Example:
        >>> audio_float = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
        >>> audio_int16 = float32_to_int16(audio_float)
        >>> audio_int16  # [0, 16384, -16384, 32767]
    """
    scaled = audio * 32768.0
    clipped = np.clip(scaled, -32768, 32767)
    return clipped.astype(np.int16)


def load_wav_file(path: Path | str) -> Tuple[np.ndarray, int]:
    """Load a WAV file and return audio data with sample rate.

    Args:
        path: Path to WAV file

    Returns:
        Tuple of (audio_data as int16 or float32, sample_rate)

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file is not a valid WAV
    """
    import wave

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"WAV file not found: {path}")

    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        n_channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        n_frames = wav.getnframes()

        raw_data = wav.readframes(n_frames)

    # Convert to numpy array based on sample width
    if sample_width == 2:  # 16-bit
        audio = np.frombuffer(raw_data, dtype=np.int16)
    elif sample_width == 4:  # 32-bit float
        audio = np.frombuffer(raw_data, dtype=np.float32)
    else:
        raise ValueError(f"Unsupported sample width: {sample_width} bytes")

    # If stereo, convert to mono
    if n_channels == 2:
        audio = stereo_to_mono(audio)

    return audio, sample_rate


def process_webrtc_frame_for_modal(
    frame_data: bytes,
    source_rate: int = 48000,
    target_rate: int = 24000,
    is_stereo: bool = True,
) -> bytes:
    """Process a WebRTC audio frame for sending to Modal.

    Complete pipeline: bytes -> int16 -> mono -> resample -> float32 -> bytes

    Args:
        frame_data: Raw PCM bytes (int16)
        source_rate: Source sample rate (WebRTC default: 48000)
        target_rate: Target sample rate (Kyutai: 24000)
        is_stereo: Whether input is stereo

    Returns:
        Float32 PCM bytes ready for Modal
    """
    # bytes -> int16
    audio = np.frombuffer(frame_data, dtype=np.int16)

    # stereo -> mono
    if is_stereo and len(audio) % 2 == 0:
        audio = stereo_to_mono(audio)

    # resample
    if source_rate != target_rate:
        audio = resample(audio, source_rate, target_rate)

    # int16 -> float32
    audio_float = int16_to_float32(audio)

    return audio_float.tobytes()
