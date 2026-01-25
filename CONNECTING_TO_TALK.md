# Connecting to a public Nextcloud Talk room

This is a self-contained summary of how to join `https://cloud.codemyriad.io/call/erwcr27x`, send/receive media, and what is still blocking a headless round-trip test.

## Goal and current status
- Goal: headless script joins the public room, streams `../kyutai_modal/test_audio.wav`, receives the room mix, and forwards the received audio to Modal to verify intelligibility.
- Browser path (Playwright/Chrome, fake audio) works end-to-end. ICE succeeds via TURN.
- Headless path (aiortc in `tools/roundtrip_modal.py`) authenticates, joins the call, publishes audio, and reaches `iceConnectionState=completed`, but never receives a downstream offer. `requestoffer` is rejected with `{"code":"not_allowed","message":"Not allowed to request offer."}` so the listener peer connection never gets a remote description. No audio reaches Modal because no remote track arrives.
- TURN/STUN from `/signaling/settings` work (no 403/channel_bind). Example browser ICE pair: local relay `turn:cloud.codemyriad.io:3478` → `172.18.0.4:64521` (relay), remote host `172.18.0.4:54450`, RTT ~60 ms.

Relevant scripts:
- `tools/stream_audio_to_talk.py`: Playwright-based, publishes a WAV to the room (works).
- `tools/roundtrip_modal.py`: aiortc-based, tries to publish + listen + stream to Modal (blocked on missing downstream offer).

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

## Current blocker and why it matters

- After joining the call as two guests (publisher + listener), the publisher sends an offer to its own signaling session (MCU) and receives an answer plus ICE candidates; ICE completes and audio is being sent.
- The listener sends `{"type":"requestoffer","roomType":"video","sid":<sid>}` (and variants) to the publisher’s session id, but the server responds with `not_allowed` and never emits an `offer` toward the listener. As a result, the listener never gets a remote description/track and the Modal round-trip cannot be validated.
- The signaling channel shows only control/mute/nick messages even though `participants/update` reports all peers `inCall: 3`.

## Hypotheses for the `not_allowed` gate (based on Talk signaling research)

- It is application-layer, not a network block: TCP/WSS succeeds and `hello` is accepted (sessionid + features returned).
- Possible state/protocol mismatch:
  - The MCU may send offers automatically to subscribers without a `requestoffer`; if so, we need the exact trigger/sender session id used by the web client.
  - `requestoffer` may be limited to certain client types or require a different `sid`/`roomType` payload than we send.
  - If the server thinks the subscriber is not fully joined/in call, it rejects media actions (`not_allowed` is the generic enforcement).
- Less likely here (but common causes): Origin/Host mismatch in reverse proxy, signaling secret mismatch, or backend auth failure. These appear unlikely because `hello`, `room`, and `/call` all succeed and TURN is usable.
- Other known `not_allowed` causes from HPB research (keep in mind when comparing with server logs/config):
  - Reverse proxy origin/Host mismatch → CSWSH protection rejects the request.
  - Signaling secret mismatch between HPB and Nextcloud (`config.php` vs `server.conf`).
  - HPB failing to call back into Nextcloud because of `allow_local_remote_servers=false` or DNS/hairpin issues (`Host violates local access rules`).
  - Reusing an expired Janus handle/session id after restarts.

## What experts should capture next

1) Capture the working browser signaling frames on `wss://cloud.codemyriad.io/standalone-signaling/spreed`:
   - Sender session id(s) that deliver `offer` to new subscribers.
   - Exact payload (including `sid`, `roomType`, `clientType`, etc.) preceding the downstream offer. Confirm whether the browser sends `requestoffer` or relies on server-initiated offers.
2) Map MCU/session ids: is there a distinct MCU session to target, or do offers arrive from another participant session?
3) Identify the permission check that returns `not_allowed` for `requestoffer` in this deployment (likely in the HPB signaling server). Determine the allowed verb/payload for guest subscribers.

Until that is known, Playwright/browser automation is the reliable path for media; headless clients can publish but cannot receive because the subscriber offer never arrives.

## Browser signaling capture (Chrome DevTools, public call reload)

Captured from the running Chrome (devtools WS `wss://cloud.codemyriad.io/standalone-signaling/spreed`, with a page reload to force a fresh handshake):
- `hello` v2 → server replies with `sessionid = onBdp_vjPu_rJYaZgY4sbHxYumKfP3wGgDDsNBQeTVh8PVVGMlZSWjVGV2FJUE5QbDh4LXFhSTFCOVd2dk5vczlyZVBRM19DalBVdEJpaV9qQWxZVVJLMGttTXMyfDg2MDI3Mzk2NzE=`.
- Sent `room` join with `roomid: erwcr27x` and `sessionid` equal to the HTTP `/participants/active` session (`k/3DCcYOH+5AEvCDRy8RC8Vww2Vvcdgp+2qhg2sXQlb9DESzh0Eri0O9ZTS/y28YDcEF34Y6QG2m6+rcMSg9GouOhirJCyzXVsAMxuxddDxlFlhCIYXAl0WU00WfgjIPxl55wKgGi3/g4Dpctd7kMmglZRbiZWcyqSVEQAFTT/fAyoWgysqCktKrWakZ7WmBxciRieJsy5yIBv1Nz0Ti95HX6Rdz9NXgYD/cDukkH24bkF2CPPy30BLc9Wh6q4K`).
- Participants present after join: self (sessionid above) and `Human tester` with signaling sessionid `bvWra8CGd9fH1oYOl4hS1imPxjuLg_rcjb0NNJfLT2t8PThCcFQ0MVo0NEFjRHpodTctSjRqNjF2QlZQVjN2MXJiLUhEVHdtYlJZakJNWmV0TjN4Z2M2YWNUY2YyfDQzMzE3Mzk2NzE=` and `inCall: 3`.
- Upstream publish: browser sends a burst of `message` frames to its *own* sessionid with `sid = 1769372072442` (roomType `video`) followed by a server answer/candidates. This matches the “offer to MCU” step.
- Downstream subscribe: browser sends `requestoffer` (roomType `video`, **no sid**) to the other participant sessionid (`bvWra8CGd9fH1oYOl4hS1...`). The server responds from that sessionid with WebRTC negotiation messages (offer/candidates not fully expanded in the truncated log), and the browser answers using a new `sid = 8443238136283536`. Control frames (mute/unmute/nickChanged) also use the same session addressing.

Implication: the working browser targets `requestoffer` at the remote participant sessionid (not at its own session/MFU). Replicating this pattern—including separate sids for upstream/downstream—may avoid the `not_allowed` rejection seen in the headless client.

## Script change in progress (aiortc)
- `tools/roundtrip_modal.py` now tracks separate sids for publish vs subscribe and keeps a list of remote participant session ids from the room join. The listener sends `requestoffer` (no sid) to each remote session id (mirrors browser) and accepts a downstream sid distinct from the publish sid. Candidates/answers for downstream offers use `subscribe_sid`.
- Validation (2025‑02‑25): even when sending `requestoffer` with no `sid` to every in-call session (including the “silviot” logged-in user and other guests) the signaling server responds `{"code":"not_allowed","message":"Not allowed to request offer."}`. Upstream publish path still works (offer → answer → ICE completed). Downstream offers never arrive, so Modal receives 0 bytes. The issue is not just targeting the wrong sessionid.
- Browser DevTools capture confirms the working `requestoffer` payload is minimal (no `sid`, just `roomType:"video"`) and downstream offers use their own `sid`. Our payload matches.
- HPB source (server/hub.go) shows `requestoffer` is rejected unless `isInSameCall` is true for sender+recipient (both in the same room with `inCall` flag set; `allowSubscribeAnyStream` default is false). The repeated `not_allowed` likely means the server does not consider our bot sessions “in call” despite POST `/call/...` with flags=3.
- Next step: ensure `inCall` is seen by HPB (e.g., wait for participants/update inCall events before requesting offers, or check backend → HPB “roomInCall” updates). Otherwise, inspect HPB logs/config to see why our sessions aren’t marked in call.
