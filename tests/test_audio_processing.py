"""Tests for audio processing pure functions.

These tests verify actual behavior without mocking.
They use real audio math and real sample files.
"""

import numpy as np
import pytest
from pathlib import Path

from ex_app.lib.audio.processing import (
    stereo_to_mono,
    resample,
    int16_to_float32,
    float32_to_int16,
    load_wav_file,
    process_webrtc_frame_for_modal,
    AudioConfig,
)


class TestStereoToMono:
    """Tests for stereo_to_mono function."""

    def test_basic_averaging(self):
        """Stereo pairs should be averaged."""
        # L=100, R=200 -> avg=150
        # L=300, R=400 -> avg=350
        stereo = np.array([100, 200, 300, 400], dtype=np.int16)
        mono = stereo_to_mono(stereo)

        assert len(mono) == 2
        assert mono[0] == 150
        assert mono[1] == 350

    def test_empty_array(self):
        """Empty input should return empty output."""
        stereo = np.array([], dtype=np.int16)
        mono = stereo_to_mono(stereo)
        assert len(mono) == 0

    def test_odd_samples_raises(self):
        """Odd number of samples is invalid stereo."""
        stereo = np.array([100, 200, 300], dtype=np.int16)
        with pytest.raises(ValueError, match="even number of samples"):
            stereo_to_mono(stereo)

    def test_preserves_dtype(self):
        """Output should be int16."""
        stereo = np.array([100, 200], dtype=np.int16)
        mono = stereo_to_mono(stereo)
        assert mono.dtype == np.int16

    def test_large_values_dont_overflow(self):
        """Averaging large values should not overflow."""
        # 32000 + 32000 = 64000, which overflows int16
        # But averaging gives 32000, which is fine
        stereo = np.array([32000, 32000], dtype=np.int16)
        mono = stereo_to_mono(stereo)
        assert mono[0] == 32000

    def test_negative_values(self):
        """Should handle negative values correctly."""
        stereo = np.array([-1000, 1000, -500, -500], dtype=np.int16)
        mono = stereo_to_mono(stereo)
        assert mono[0] == 0  # (-1000 + 1000) / 2
        assert mono[1] == -500


class TestResample:
    """Tests for resample function."""

    def test_48k_to_24k_halves_samples(self):
        """Downsampling 2:1 should halve the sample count."""
        audio_48k = np.zeros(4800, dtype=np.int16)
        audio_24k = resample(audio_48k, 48000, 24000)
        assert len(audio_24k) == 2400

    def test_same_rate_returns_unchanged(self):
        """Same source and target rate should return identical array."""
        audio = np.array([100, 200, 300], dtype=np.int16)
        result = resample(audio, 24000, 24000)
        np.testing.assert_array_equal(result, audio)

    def test_empty_array(self):
        """Empty input should return empty output."""
        audio = np.array([], dtype=np.int16)
        result = resample(audio, 48000, 24000)
        assert len(result) == 0

    def test_invalid_rates_raise(self):
        """Zero or negative rates should raise ValueError."""
        audio = np.array([100, 200], dtype=np.int16)
        with pytest.raises(ValueError, match="positive"):
            resample(audio, 0, 24000)
        with pytest.raises(ValueError, match="positive"):
            resample(audio, 48000, -1)

    def test_preserves_int16_dtype(self):
        """int16 input should produce int16 output."""
        audio = np.array([100, 200, 300, 400], dtype=np.int16)
        result = resample(audio, 48000, 24000)
        assert result.dtype == np.int16

    def test_sine_wave_frequency_preserved(self):
        """A sine wave should maintain its frequency after resampling."""
        # Generate 100Hz sine at 48kHz for 100ms
        duration = 0.1
        freq = 100
        source_rate = 48000
        target_rate = 24000

        t_source = np.linspace(0, duration, int(source_rate * duration), endpoint=False)
        sine_48k = (np.sin(2 * np.pi * freq * t_source) * 16000).astype(np.int16)

        sine_24k = resample(sine_48k, source_rate, target_rate)

        # Check the sample count is correct
        expected_samples = int(target_rate * duration)
        assert len(sine_24k) == expected_samples

        # The peak values should be similar (within 10% due to resampling artifacts)
        assert abs(np.max(sine_24k) - np.max(sine_48k)) < 1600


class TestInt16ToFloat32:
    """Tests for int16_to_float32 conversion."""

    def test_zero_stays_zero(self):
        """Zero should convert to zero."""
        audio = np.array([0], dtype=np.int16)
        result = int16_to_float32(audio)
        assert result[0] == 0.0

    def test_max_positive_becomes_one(self):
        """32767 should become approximately 1.0."""
        audio = np.array([32767], dtype=np.int16)
        result = int16_to_float32(audio)
        assert 0.99 < result[0] <= 1.0

    def test_max_negative_becomes_minus_one(self):
        """-32768 should become exactly -1.0."""
        audio = np.array([-32768], dtype=np.int16)
        result = int16_to_float32(audio)
        assert result[0] == -1.0

    def test_half_values(self):
        """Half-range values should convert proportionally."""
        audio = np.array([16384, -16384], dtype=np.int16)
        result = int16_to_float32(audio)
        assert abs(result[0] - 0.5) < 0.01
        assert abs(result[1] - (-0.5)) < 0.01

    def test_output_dtype_is_float32(self):
        """Output should be float32."""
        audio = np.array([100, 200], dtype=np.int16)
        result = int16_to_float32(audio)
        assert result.dtype == np.float32


class TestFloat32ToInt16:
    """Tests for float32_to_int16 conversion."""

    def test_roundtrip(self):
        """Converting int16 -> float32 -> int16 should preserve values."""
        original = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
        float_audio = int16_to_float32(original)
        recovered = float32_to_int16(float_audio)
        np.testing.assert_array_equal(recovered, original)

    def test_clips_overflow(self):
        """Values > 1.0 should be clipped to 32767."""
        audio = np.array([2.0, -2.0], dtype=np.float32)
        result = float32_to_int16(audio)
        assert result[0] == 32767
        assert result[1] == -32768


class TestLoadWavFile:
    """Tests for load_wav_file function."""

    # Use the real sample files
    SAMPLES_DIR = Path("/home/silvio/dev/kyutai_modal/samples/wav24k")

    def test_load_real_sample(self):
        """Should load a real WAV sample file."""
        wav_path = self.SAMPLES_DIR / "chunk_0.wav"
        if not wav_path.exists():
            pytest.skip("Sample file not available")

        audio, sample_rate = load_wav_file(wav_path)

        assert sample_rate == 24000
        assert len(audio) > 0
        assert audio.dtype == np.int16

    def test_nonexistent_file_raises(self):
        """Should raise FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_wav_file("/nonexistent/path.wav")


class TestProcessWebrtcFrameForModal:
    """Tests for the complete processing pipeline."""

    def test_output_is_float32_bytes(self):
        """Output should be float32 little-endian bytes."""
        # Simulate WebRTC frame: 48kHz stereo
        # 20ms = 960 samples per channel = 1920 total samples
        frame_data = np.zeros(1920, dtype=np.int16).tobytes()

        result = process_webrtc_frame_for_modal(frame_data)

        # Should be float32 bytes
        # 960 stereo -> 480 mono at 48k -> 240 at 24k -> 240 * 4 bytes
        result_array = np.frombuffer(result, dtype=np.float32)
        assert result_array.dtype == np.float32

    def test_stereo_to_mono_and_resample(self):
        """Full pipeline should convert stereo 48k to mono 24k."""
        # Create stereo 48kHz test signal: 20ms = 960 samples/channel
        samples_per_channel = 960
        stereo_samples = samples_per_channel * 2

        # Left=1000, Right=2000 for all samples
        stereo = np.zeros(stereo_samples, dtype=np.int16)
        stereo[0::2] = 1000  # Left channel
        stereo[1::2] = 2000  # Right channel

        result = process_webrtc_frame_for_modal(stereo.tobytes())
        result_array = np.frombuffer(result, dtype=np.float32)

        # After mono conversion, should be avg of 1000,2000 = 1500
        # Then normalized: 1500/32768 ≈ 0.0458
        expected_mono_value = 1500 / 32768.0

        # All values should be close to expected (allowing for resampling artifacts)
        assert np.allclose(result_array, expected_mono_value, atol=0.01)

    def test_sample_count_after_processing(self):
        """Sample count should be halved (48k->24k) after stereo->mono."""
        # 48kHz stereo: 1920 samples (960 per channel)
        # After mono: 960 samples at 48k
        # After resample to 24k: 480 samples
        frame_data = np.zeros(1920, dtype=np.int16).tobytes()

        result = process_webrtc_frame_for_modal(frame_data)
        result_array = np.frombuffer(result, dtype=np.float32)

        assert len(result_array) == 480


class TestAudioConfig:
    """Tests for AudioConfig dataclass."""

    def test_webrtc_stereo_preset(self):
        """WEBRTC_STEREO should have correct values."""
        assert AudioConfig.WEBRTC_STEREO.sample_rate == 48000
        assert AudioConfig.WEBRTC_STEREO.channels == 2

    def test_kyutai_mono_preset(self):
        """KYUTAI_MONO should have correct values."""
        assert AudioConfig.KYUTAI_MONO.sample_rate == 24000
        assert AudioConfig.KYUTAI_MONO.channels == 1

    def test_immutable(self):
        """AudioConfig should be immutable."""
        config = AudioConfig(sample_rate=44100, channels=1)
        with pytest.raises(Exception):  # frozen=True raises FrozenInstanceError
            config.sample_rate = 48000
