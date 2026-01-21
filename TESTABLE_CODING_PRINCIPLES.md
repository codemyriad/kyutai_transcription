# Testable Coding Principles

*Lessons learned from debugging the Modal STT integration*

## Problems That Could Have Been Caught With Tests

### 1. Stereo vs Mono Audio
- **Issue**: WebRTC delivered stereo (2 channels), code assumed mono
- **Result**: Garbled audio, half-speed playback
- **Detection method**: Manual testing, listening to saved audio
- **Time to detect**: ~30 minutes of debugging
- **With tests**: Immediate failure

### 2. Raw Opus vs Ogg Opus Container
- **Issue**: `opuslib.Encoder.encode()` produces raw Opus frames, but Modal's `sphn.read_opus_bytes()` expects Ogg container format
- **Result**: Modal received valid Opus frames but couldn't decode them
- **Detection method**: No transcripts returned, only pings
- **Time to detect**: Multiple deploy cycles, ~20 minutes
- **With tests**: Would fail immediately when testing against `sphn`

### 3. Accumulating Buffer Duplicate Data
- **Issue**: Encoder returned ALL accumulated bytes each call, Modal accumulated all received bytes
- **Result**: Data like `[A, AB, ABC]` → `[A+AB+ABC]` = garbage
- **Detection method**: Reasoning about byte sizes in logs
- **Time to detect**: Would have been caught immediately with a test

### 4. PyAV API Change
- **Issue**: `stream.channels` became read-only in newer PyAV
- **Detection method**: Runtime error in production
- **Time to detect**: One deploy cycle
- **With tests**: CI would catch this on dependency update

## The Cost of Slow Feedback Loops

### Without Tests (What We Did)
```
Edit code locally
    → git commit
    → git push
    → SSH to server
    → git pull
    → docker build (~20s cached, ~60s uncached)
    → docker push (~10s)
    → occ app:unregister
    → occ app:register (~30s)
    → Open browser, start call, enable transcription
    → Wait for Modal cold start (~20s)
    → Speak, observe logs

Total: 2-5 minutes per iteration
```

### With Tests
```
Edit code → Run pytest → See failure → Fix → Run pytest → Pass

Total: 5-15 seconds per iteration
```

---

## Recommended Code Structure

### Bad: Entangled (Hard to Test)
```python
# transcriber.py - everything mixed together
class OggOpusEncoder:
    # PyAV details + buffer management + encoding logic

class ModalTranscriber:
    # WebSocket connection + audio processing + result parsing + debug logging
```

### Good: Separated Concerns (Easy to Test)
```
lib/
├── audio/
│   ├── formats.py        # Channel detection, format conversion (pure functions)
│   ├── resampler.py      # Resampling logic (pure functions)
│   └── encoder.py        # Encoder interface
│
├── codecs/
│   ├── ogg_opus.py       # OggOpusEncoder - just encoding, no I/O
│   └── raw_pcm.py        # Passthrough for testing
│
├── protocols/
│   ├── modal.py          # Modal message format, auth headers, response parsing
│   └── hpb.py            # HPB signaling protocol
│
├── transport/
│   └── ws_client.py      # Generic async WebSocket with reconnection
│
├── transcriber.py        # Thin orchestration layer
└── service.py            # HTTP endpoint handlers
```

---

## The Six Principles

### Principle 1: Separate I/O from Logic

**Bad**: Logic mixed with I/O
```python
class Transcriber:
    async def process(self):
        ws = await websockets.connect(url)  # I/O
        data = resample(audio)               # Logic
        encoded = encode(data)               # Logic
        await ws.send(encoded)               # I/O
        response = await ws.recv()           # I/O
        return parse(response)               # Logic
```

**Good**: Logic is pure, I/O is separate
```python
# Pure logic - easily testable
def resample(audio, from_rate, to_rate) -> np.ndarray: ...
def encode(audio) -> bytes: ...
def parse_response(raw: str) -> TranscriptResult: ...

# I/O layer - thin wrapper
class TranscriberClient:
    async def send_audio(self, encoded: bytes): ...
    async def receive(self) -> str: ...

# Orchestration - glues them together
class Transcriber:
    async def process(self, audio):
        resampled = resample(audio, 48000, 24000)
        encoded = self.encoder.encode(resampled)
        await self.client.send_audio(encoded)
        raw = await self.client.receive()
        return parse_response(raw)
```

### Principle 2: Test External Protocols Locally

When integrating with external services, create tests that verify your output matches what they expect:

```python
# If Modal uses sphn.read_opus_bytes(), install sphn and test against it
def test_our_encoder_compatible_with_modal():
    our_output = our_encoder.encode(test_audio)

    # Use the SAME decoder Modal uses
    decoded = sphn.read_opus_bytes(our_output)
    assert decoded is not None
```

This catches format mismatches **before** deploying.

### Principle 3: Make State Changes Visible

**Bad**: Hidden internal state
```python
class Encoder:
    def encode(self, data):
        self._buffer.write(data)
        self._buffer.seek(0)
        return self._buffer.read()  # Returns everything each time!
```

**Good**: Explicit about what's returned
```python
class Encoder:
    def encode(self, data) -> bytes:
        """Returns only NEW bytes since last call."""
        # ... implementation ...
        new_bytes = self._output[self._last_position:]
        self._last_position = len(self._output)
        return new_bytes
```

Test the contract:
```python
def test_returns_only_new_bytes():
    e = Encoder()
    b1 = e.encode(chunk1)
    b2 = e.encode(chunk2)

    # b2 must not overlap with b1
    assert b1 + b2 == e.get_all_output()
```

### Principle 4: Build Bottom-Up with Tests

1. **Start with the leaf components** (no dependencies):
   ```python
   # Day 1: Build and test the encoder
   def test_encoder_basic(): ...
   def test_encoder_with_silence(): ...
   def test_encoder_format_compatibility(): ...
   ```

2. **Add the next layer**:
   ```python
   # Day 2: Build and test the protocol parser
   def test_parse_token(): ...
   def test_parse_error(): ...
   ```

3. **Integrate with mocks**:
   ```python
   # Day 3: Test transcriber with mock WebSocket
   def test_transcriber_sends_encoded_audio():
       mock_ws = Mock()
       transcriber = Transcriber(encoder, mock_ws)

       transcriber.process(test_audio)

       mock_ws.send.assert_called_once()
       sent_data = mock_ws.send.call_args[0][0]
       assert is_valid_ogg_opus(sent_data)
   ```

4. **Integration test last**:
   ```python
   # Day 4: End-to-end test (can be slow, run less often)
   @pytest.mark.integration
   def test_full_transcription_flow():
       # Actually connect to Modal and verify transcription
   ```

### Principle 5: Fail Fast, Fail Locally

Every assumption should be testable locally:

| Assumption | Local Test |
|------------|------------|
| Audio is stereo | `assert frame.channels == 2` |
| Encoder produces valid Ogg | Decode with `sphn` |
| Modal auth format correct | Unit test header generation |
| Message parsing works | Test all message types |

### Principle 6: Design for Testability

Ask yourself:
- Can I test this function without network access?
- Can I test this class without creating the whole system?
- Can I verify the output format without the real receiver?

If "no" to any, refactor until "yes".

---

## Example Tests That Would Have Caught Our Bugs

### Stereo Detection
```python
def test_audio_stream_detects_stereo():
    mock_frame = create_mock_frame(channels=2, sample_rate=48000)
    stream = AudioStream(mock_track)
    # ... process frame
    assert stream.channels == 2
```

### Opus Format Compatibility
```python
def test_encoder_produces_valid_ogg_opus():
    encoder = OggOpusEncoder(24000, 1)
    pcm = np.zeros(4800, dtype=np.int16)
    encoded = encoder.encode(pcm)

    # This is what Modal does - use the same decoder
    import sphn
    decoded, sr = sphn.read_opus_bytes(encoded)
    assert decoded is not None
    assert sr == 24000
```

### No Duplicate Data
```python
def test_encoder_sends_only_new_bytes():
    encoder = OggOpusEncoder(24000, 1)

    chunk1 = encoder.encode(np.zeros(960, dtype=np.int16))
    chunk2 = encoder.encode(np.zeros(960, dtype=np.int16))

    # Simulate what Modal does
    accumulated = chunk1 + chunk2
    decoded, sr = sphn.read_opus_bytes(accumulated)

    # Should decode successfully
    assert len(decoded) > 0
```

---

## Summary

The problems in this session stemmed from:
1. **Tight coupling** - Encoding, transport, and parsing intertwined
2. **No local verification** - Couldn't test Ogg format without deploying
3. **Hidden state** - Buffer accumulation wasn't visible in the API
4. **No contract tests** - Never verified compatibility with Modal's decoder

The fix is structural:
- **Separate concerns** into testable modules
- **Test external protocols** with the same libraries the receiver uses
- **Make state explicit** in function signatures and docstrings
- **Build bottom-up** with tests at each layer
