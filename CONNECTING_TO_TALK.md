# Connecting to a public Nextcloud Talk room

This documents the HTTP and signaling calls needed to join a public Talk room, post/read chat messages, and connect to the WebRTC call. All flows were validated against `https://cloud.codemyriad.io/call/erwcr27x` where the chat currently contains the message “I am human!” from **Human tester**.

## Prerequisites: cookies and CSRF token

1) Load the public room page to obtain the session cookies and CSRF request token:
```
curl -c /tmp/talk.cookies https://cloud.codemyriad.io/call/erwcr27x -o /tmp/call.html
TOKEN=$(python3 - <<'PY'
import re
html=open('/tmp/call.html').read()
m=re.search(r'data-requesttoken="([^"]+)"', html)
print(m.group(1))
PY
)
```
2) All subsequent OCS requests must include:
- Cookie jar from `/tmp/talk.cookies`
- Header `OCS-APIREQUEST: true`
- Header `requesttoken: $TOKEN`
- (Recommended) `?format=json` query parameter for JSON responses

## Join the conversation and get the signaling session id

Join (or rejoin) the room; the response contains `sessionId` which is required when subscribing to signaling:
```
curl -b /tmp/talk.cookies \
  -H "OCS-APIREQUEST: true" \
  -H "requesttoken: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"force":true}' \
  "https://cloud.codemyriad.io/ocs/v2.php/apps/spreed/api/v4/room/erwcr27x/participants/active?format=json"
```
Relevant response fields:
- `sessionId`: Nextcloud session identifier to present on the signaling channel
- `actorType/actorId`: who you are for chat/call purposes
- `callFlag`: whether a call is already running

Setting a guest display name would normally use `POST /ocs/v2.php/apps/spreed/api/v1/guest/{token}/name` with `{"displayName": "Your Name"}`; this endpoint returned 404 on this instance, but chat messages accept an `actorDisplayName` field (see below) so you can still show a custom name.

## Chat: post and read messages

### Send a message
```
curl -b /tmp/talk.cookies \
  -H "OCS-APIREQUEST: true" \
  -H "requesttoken: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello from CLI Bot","actorDisplayName":"CLI Bot"}' \
  "https://cloud.codemyriad.io/ocs/v2.php/apps/spreed/api/v1/chat/erwcr27x?format=json"
```
Returns `201` with the created message (`id`, `timestamp`, `threadId`, etc.).

### Fetch existing messages
```
curl -b /tmp/talk.cookies \
  -H "OCS-APIREQUEST: true" \
  "https://cloud.codemyriad.io/ocs/v2.php/apps/spreed/api/v1/chat/erwcr27x?format=json&lookIntoFuture=0&lastKnownMessageId=0&limit=20&setReadMarker=0&includeLastKnown=0"
```
This returned (among others) message `id: 6348`, `actorDisplayName: "Human tester"`, `message: "I am human!"`.

### Long-poll for new messages
Use the same endpoint with `lookIntoFuture=1` and `timeout=<ms>`; pass your last seen message id in `lastKnownMessageId`.

## Signaling and WebRTC

1) Fetch signaling settings (contains WebSocket endpoint, auth, STUN/TURN):
```
curl -b /tmp/talk.cookies \
  -H "OCS-APIREQUEST: true" \
  "https://cloud.codemyriad.io/ocs/v2.php/apps/spreed/api/v3/signaling/settings?format=json&token=erwcr27x"
```
Key fields observed:
- `server`: `https://cloud.codemyriad.io/standalone-signaling/`
- `helloAuthParams`: authentication for the signaling hello (v2 JWT present)
- `ticket`: legacy v1 ticket
- `stunservers` / `turnservers`: e.g. `stun:cloud.codemyriad.io:3478`, `turn:cloud.codemyriad.io:3478?transport=udp/tcp` with username/credential

2) Open a WebSocket to `{server}/spreed` (wss).

3) Hello handshake:
   - Wait for an optional `welcome` message (hello-v2 feature). If none arrives within a few seconds, send `hello` immediately.
   - Send:
```
{
  "type": "hello",
  "hello": {
    "version": "2.0",            // use "1.0" if only the ticket is available
    "auth": {
      "url": "https://cloud.codemyriad.io/ocs/v2.php/apps/spreed/api/v3/signaling/backend",
      "params": { "token": "<helloAuthParams[\"2.0\"].token>" } // or { "userid": null, "ticket": "<ticket>" } for v1
    }
  }
}
```
   - The server replies with `type: "hello"` containing `sessionid` (signaling session), `resumeid`, and server features.

4) Join the room on the signaling channel:
```
{
  "type": "room",
  "room": {
    "roomid": "erwcr27x",
    "sessionid": "<sessionId from participants/active>"
  }
}
```
After this, signaling messages (`type: "message"` / `"control"`) will carry WebRTC offers/answers and ICE candidates to/from other peers. Payloads are of the form:
```
// offer/answer/candidate relay
{
  "to": "<peer signaling session id>",
  "sid": "<call id>",
  "roomType": "<call/screen share>",
  "type": "offer" | "answer" | "candidate",
  "payload": { ...SDP or ICE... }
}
```
Use the STUN/TURN servers from step 1 when constructing your `RTCPeerConnection`. For audio-only media, create offers with `offerToReceiveAudio: 1` and `offerToReceiveVideo: 0`.

## Entering the call (audio-only example)

After joining the conversation and the signaling room:
```
curl -b /tmp/talk.cookies \
  -H "OCS-APIREQUEST: true" \
  -H "requesttoken: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"flags":3,"silent":false,"recordingConsent":false,"silentFor":[]}' \
  "https://cloud.codemyriad.io/ocs/v2.php/apps/spreed/api/v4/call/erwcr27x?format=json"
```
`flags` is a bitmask (`IN_CALL=1`, `WITH_AUDIO=2`, `WITH_VIDEO=4`, `WITH_PHONE=8`). `3` means “in call + audio only”. The signaling channel will now deliver/expect WebRTC offers/answers/candidates; once media is negotiated, start sending your audio track.

To update call media flags without leaving, `PUT /ocs/v2.php/apps/spreed/api/v4/call/{token}` with a new `flags` value. To leave, `DELETE /ocs/v2.php/apps/spreed/api/v4/call/{token}`.

## Quick recap of the minimal flow

1) GET public room page → cookies + `requesttoken`.
2) POST `/room/{token}/participants/active` → `sessionId` for signaling.
3) GET `/signaling/settings?token={token}` → signaling server + auth + STUN/TURN.
4) WebSocket to `{server}/spreed` → `hello` (auth) → receive `hello`.
5) Send `room` message with `roomid` + `sessionid` to subscribe.
6) POST `/call/{token}` with flags (e.g., `3` for audio) to join the call.
7) Exchange WebRTC offer/answer/ICE over the signaling WebSocket and stream audio.

## Current state and blocker

- Browser path (Playwright, Chrome fake audio) works end-to-end on `https://cloud.codemyriad.io/call/erwcr27x` with no internal secret.
- Headless path (aiortc/wrtc) now gets past TURN:
  - HTTP bootstrap: fetch room page → cookies + `requesttoken`; `POST /room/{token}/participants/active` → `sessionId`; `GET /signaling/settings?token=erwcr27x` → signaling URL/auth + STUN/TURN; `POST /call/{token}` flags=3.
  - Signaling: `hello` v2 → `sessionid` + features; send offer (with candidates) to own signaling session (MCU). MCU replies with answer + candidates; `iceConnectionState=completed` on the publish PC. TURN creds from settings work (no 403/channel_bind). Example browser ICE: local relay `turn:cloud.codemyriad.io:3478` → `172.18.0.4:64521` (relay), remote host `172.18.0.4:54450`, RTT ~60 ms, state `succeeded`.
- Blocker: no downstream offers to subscribers. Signaling rejects `requestoffer` with `{"code":"not_allowed","message":"Not allowed to request offer."}`. The signaling WS never emits `offer` messages toward the listener session, even though `participants/update` shows all peers `inCall: 3`. Only control/mute/unmute/nick events arrive, so the listener PC never gets a remote description/track and Modal sees 0 bytes.

## What’s missing (for experts to investigate)

1) Capture the working browser’s signaling WS (`wss://cloud.codemyriad.io/standalone-signaling/spreed`) to see:
   - Which sender session id issues downstream `offer` messages to a subscriber, and the `sid` values used.
   - Whether the MCU sends offers unprompted or after a different trigger (not `requestoffer`).
2) Identify the MCU session id (if any) and the exact message shape required for guests to receive offers.
3) Explain/adjust the permission gate returning `not_allowed` on `requestoffer`; if another verb (e.g. `sendoffer`, `recipient-call`, or other control) is needed, document it.

Until those details are known, the reliable route is browser-driven (Playwright) for publish/listen; headless clients can publish but cannot receive because downstream offers are missing.***
