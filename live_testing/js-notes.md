Yes. If you switch to **Node.js**, you can use `simple-peer` combined with `wrtc` (Node-WebRTC) to achieve the same result.

However, since Node.js has no microphone or "browser," you cannot use `getUserMedia`. Instead, you must manually decode an audio file into raw PCM data and feed it into the WebRTC connection using the `nonstandard` APIs provided by `wrtc`.

### Prerequisites

You must have **FFmpeg** installed on your system (to decode the audio file).
Then install these packages:

```bash
npm install simple-peer wrtc fluent-ffmpeg websocket

```

### Node.js Code (Audio Streamer)

This script connects to a WebSocket (like Nextcloud Talk), creates a "virtual microphone" from an audio file, and streams it.

```javascript
const SimplePeer = require('simple-peer');
const wrtc = require('wrtc'); // Provides WebRTC for Node
const ffmpeg = require('fluent-ffmpeg');
const WebSocket = require('websocket').client;
const { RTCAudioSource } = wrtc.nonstandard;

// --- CONFIGURATION ---
const AUDIO_FILE = 'audio.mp3'; // Ensure this file exists
const SIGNALING_URL = 'wss://your-nextcloud-instance/signaling/url'; 
const ROOM_TOKEN = 'your_token';

// 1. Setup the "Virtual Microphone"
// We create a source that accepts raw audio data (PCM)
const audioSource = new RTCAudioSource();
const audioTrack = audioSource.createTrack();

// 2. Start Decoding the Audio File
// We use FFmpeg to convert the file to raw PCM (Signed 16-bit, 48kHz, Mono)
// This format is required for the WebRTC audio source to understand it.
const ffmpegCommand = ffmpeg(AUDIO_FILE)
    .format('s16le')       // Raw PCM, signed 16-bit little-endian
    .audioBitrate('16k')   // Low bitrate is fine for voice
    .audioChannels(1)      // Mono
    .audioFrequency(48000) // 48kHz standard for WebRTC
    .on('start', () => {
        console.log('Started reading audio file...');
    })
    .on('error', (err) => {
        console.error('FFmpeg error:', err);
    });

const audioStream = ffmpegCommand.pipe();

// 3. Feed the Audio Loop
// WebRTC expects audio in 10ms chunks. 
// 48000 Hz * 1 channel * 16-bit (2 bytes) = 96000 bytes/sec
// 10ms chunk = 960 bytes.
const CHUNK_SIZE = 960; 

audioStream.on('data', (buffer) => {
    // We must break the FFmpeg stream into 10ms chunks for WebRTC
    // Note: In a production app, you need a precise timer or buffer handling
    // to avoid "drifting". This is a simplified "firehose" approach.
    const samples = new Int16Array(buffer.buffer, buffer.byteOffset, buffer.length / 2);
    
    const data = {
        samples: samples,
        sampleRate: 48000
    };
    
    try {
        // Push data to the WebRTC track
        audioSource.onData(data);
    } catch (e) {
        // Track might be closed
    }
});

// 4. Setup WebSocket & Signaling
const client = new WebSocket();

client.on('connectFailed', function(error) {
    console.log('Connect Error: ' + error.toString());
});

client.on('connect', function(connection) {
    console.log('WebSocket Client Connected');

    // Create the WebRTC Peer
    // 'initiator: true' if we are calling them, 'false' if we join a room
    // For Nextcloud Talk, usually we join, so we wait for an offer (initiator: false) 
    // OR we send a "Hello" and they send an offer.
    const peer = new SimplePeer({
        initiator: false, 
        wrtc: wrtc,
        stream: new wrtc.MediaStream([audioTrack]), // <--- Inject our fake audio
        trickle: false 
    });

    // Handle Signaling Data
    peer.on('signal', data => {
        // When SimplePeer generates a signal (Answer/Candidate), send it to Server
        const message = {
            type: 'answer', // or candidate
            payload: JSON.stringify(data)
        };
        connection.sendUTF(JSON.stringify(message));
    });

    peer.on('connect', () => {
        console.log('WebRTC Connection ESTABLISHED!');
        console.log('Streaming audio...');
    });

    peer.on('error', err => console.error('Peer error:', err));

    // WebSocket Receive Loop
    connection.on('message', function(message) {
        if (message.type === 'utf8') {
            const msg = JSON.parse(message.utf8Data);
            
            // Adapt this to match Nextcloud's specific JSON structure
            if (msg.type === 'offer') {
                console.log("Received Offer, generating Answer...");
                peer.signal(msg.sdp); // Feed the offer to SimplePeer
            } else if (msg.type === 'candidate') {
                peer.signal(msg.candidate);
            }
        }
    });

    // Send initial Join/Hello message to trigger the server to send us an Offer
    const joinMsg = {
        type: 'join',
        token: ROOM_TOKEN
    };
    connection.sendUTF(JSON.stringify(joinMsg));
});

client.connect(SIGNALING_URL);

```

### Key Differences from Python

1. **`wrtc.nonstandard`**: In Python (`aiortc`), you use a `MediaPlayer` class. In Node.js, you have to manually use `RTCAudioSource` and push raw bits into it.
2. **Timing**: The example above pipes data as fast as FFmpeg reads it. For high-quality audio, you often need a `setTimeout` loop to ensure you send exactly 10ms of audio every 10ms, otherwise, you might flood the buffer or cause "jitter."
3. **Dependencies**: `wrtc` connects to native C++ WebRTC bindings. It can be difficult to install on some Linux distros (requires python, make, g++).
