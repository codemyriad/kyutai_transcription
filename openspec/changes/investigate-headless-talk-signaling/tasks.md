## 1. Investigation
- [ ] 1.1 Capture a fresh browser signaling trace (hello, room, call, requestoffer, offers/candidates) with session ids and payloads.
- [ ] 1.2 Capture the headless script trace for the same room and compare field-by-field (auth, features, session ids, requestoffer target, sids).
- [ ] 1.3 Identify required credentials/secrets/cookies the browser uses (e.g. hello token, requesttoken, cookies), and how to obtain them programmatically.
- [ ] 1.4 Test a modified headless client that mirrors the browser payloads to confirm downstream offers are delivered; record the minimal working sequence.

## 2. Documentation & Handoff
- [ ] 2.1 Update `CONNECTING_TO_TALK.md` (and any relevant README) with the verified end-to-end headless recipe and credential acquisition steps.
- [ ] 2.2 Note any remaining risks or server-side dependencies for future work.
