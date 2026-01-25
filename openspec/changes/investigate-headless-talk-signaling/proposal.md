# Change: investigate headless Talk signaling to match browser behavior

## Why
- Headless Python/aiortc and Node attempts get `not_allowed` on `requestoffer` and never receive downstream offers, while the browser succeeds.
- We need a repeatable client-side recipe (no server changes) to join a Talk room and send/receive audio so automation works reliably.

## What Changes
- Reverse-engineer the browser signaling flow and credentials, including any derived secrets, to reproduce it from scripts.
- Capture and document the exact signaling/API sequence (hello/join/call/requestoffer/ICE) and any required headers/cookies/tokens.
- Update the local docs and tooling (scripts/tests) to perform a successful headless round trip.

## Impact
- Affected specs: none yet (investigation), may touch Talk client automation specs when implementation is known.
- Affected code/docs: `tools/*` headless clients, `CONNECTING_TO_TALK.md`, any new helper utilities for credential extraction.
