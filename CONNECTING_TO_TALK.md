# Connecting to the public Talk room (`https://cloud.codemyriad.io/call/erwcr27x`)

Self-contained notes on how to join as a headless client, publish/receive audio, and the remaining gap for live captions.

## Current status
- Audio **works** with the aiortc client in `tools/roundtrip_modal.py` using the signaling **internal secret**. The publisher sends `../nc-modal-captions/test_audio_short.wav`, the listener receives the mix, and other room participants hear it.
- Captions now **work end-to-end** via the script by relaying Modal transcripts back into Talk signaling (`type:"transcript"` messages) to all known room sessions. Run the command below and you should see captions for the bot in the room UI.
- Target goal: make the headless client behave exactly like a browser guest (no internal secret); internal auth is only a fallback when the HPB rejects guest `requestoffer`. If you must use the fallback, pass `--internal-secret` explicitly.
- Command that works today:
  ```
  source .envrc                    # provides TALK_INTERNAL_SECRET + backend URL
  source ../kyutai_modal/.envrc    # provides MODAL_* for STT
  uv run python tools/roundtrip_modal.py \
    --room-url https://cloud.codemyriad.io/call/erwcr27x \
    --audio ../nc-modal-captions/test_audio_short.wav \
    --duration 20 \
    --enable-transcription
```
Notes: the script auto-loads Modal creds from the two `.envrc` files if not exported; `--internal-secret` triggers the HPB internal auth flow (HMAC(random, secret)).
- Signaling behaviour in the script:
  - Fetches cookies/requesttoken from the room page, then calls `/room/{token}/participants/active` to get the Nextcloud sessionId (roomsession id).
  - WebSocket hello uses internal auth to avoid “not in same call” checks; joins the room with the roomsession id.
  - Publisher offer goes to its own signaling session (MCU); listener filters incoming offers to the expected publisher signaling session.
  - Listener creates a data channel (for m=application) and only a recvonly audio transceiver; downstream answers/candidates are tied to the subscribe sid.
- Modal bytes-sent is non-zero and human users confirmed hearing the sample. Modal’s own transcripts print to stdout; Talk/HPB captions are still missing (see below).

## HTTP/OCS endpoints you need
- Page load (cookies + CSRF): GET `https://cloud.codemyriad.io/call/erwcr27x`, extract `data-requesttoken`.
- Active participant (roomsession id): POST `/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants/active?format=json` with body `{"force":true}` and headers `OCS-APIREQUEST:true`, `requesttoken:<token>`.
- Signaling settings (WS endpoint, TURN/STUN, hello auth): GET `/ocs/v2.php/apps/spreed/api/v3/signaling/settings?format=json&token={token}`.
- Enter call: POST `/ocs/v2.php/apps/spreed/api/v4/call/{token}?format=json` with `{"flags":3,"silent":false,"recordingConsent":false,"silentFor":[]}` (flags=3 means in-call + audio).

## WebSocket hello/auth
- Normal flow: `hello` v2 with `auth.url` = backend URL (`https://cloud.codemyriad.io/ocs/v2.php/apps/spreed/api/v3/signaling/backend`) and `auth.params` = `helloAuthParams["2.0"]` from settings. HPB verifies via Nextcloud.
- Internal flow (what we use): `hello.version="1.0"`, `auth.type="internal"`, params `{random:<48 hex>, token: HMAC(secret, random), backend:<room origin + trailing slash>}`. This skips backend pings that currently return “not in same call” for guests.
- After `hello` the client sends `{type:"room", room:{roomid:"erwcr27x", sessionid:<roomsession id>}}`.

## Live transcription (Talk side)
- API to toggle: POST `/ocs/v2.php/apps/spreed/api/v1/live-transcription/{token}?format=json` (no body). Requires the same cookies/requesttoken as other OCS calls.
- Set language (optional, default `en`): POST `/ocs/v2.php/apps/spreed/api/v1/live-transcription/{token}/language?format=json` with `{"languageId":"en"}`.
- Responses are `{}` (no error), so Nextcloud believes the ExApp call succeeded.
- What should happen: Nextcloud forwards the request to the ExApp (`live_transcription`), which connects to HPB with `LT_HPB_URL` + `LT_INTERNAL_SECRET`, subscribes to the room, and sends `type:"transcript"` messages over signaling. The browser renders these via `SimpleWebRTC`’s `transcript` handler.
- What we actually see: no `transcript` messages on the signaling channel, even after enabling via the endpoints above. Either the ExApp is not running/authorized, or HPB never adds our session as a transcription target. This is the remaining gap.

## What still needs investigation
- Why the ExApp/HPB never emits `type:"transcript"` to our sessions despite successful OCS responses. Candidates:
  - ExApp not deployed or missing `LT_*` env; AppAPI auth between Nextcloud and the ExApp may be failing silently.
  - ExApp connects to HPB but cannot map our Nextcloud session id to a signaling session (look for `nextcloudSessionId` in HPB events; the aiortc script logs those).
  - Permissions: the live-transcription endpoints require a participant; if moderation/lobby changes, the request may be ignored even with a 200 response.
- If ExApp is reachable, capture its `/api/v1/status` (needs AppAPI auth headers: `EX-APP-ID`, `EX-APP-VERSION`, `AUTHORIZATION-APP-API` = base64 `user:app_secret`) to confirm it is alive and knows about the room.
- If ExApp is running, watch HPB logs for `transcript` sends or add temporary logging in `ex_app/lib/spreed_client.py` (`send_transcript` / `add_target`) to confirm whether targets are being registered.

## Useful references
- `tools/roundtrip_modal.py`: working aiortc implementation (internal auth, offer filtering, Modal streaming). Adds a data channel plus recvonly audio; filters incoming offers to the expected publisher signaling session.
- `ex_app/lib/main.py`: FastAPI endpoints; guarded by `AppAPIAuthMiddleware`.
- `ex_app/lib/service.py` and `ex_app/lib/spreed_client.py`: how the ExApp connects to HPB and forwards transcripts.
- `research_on_talk_connection.md`: background on HPB/Nextcloud signalling, common `not_allowed` causes, and reverse-proxy pitfalls.
- Script-side captions: `tools/roundtrip_modal.py` now listens to Modal tokens, flushes on `vad_end`, and injects `type:"transcript"` signaling messages to all known remote sessions. This sidesteps the ExApp when it fails to transcribe the bot.
