"""
Round-trip audio test: publish a WAV file to a Nextcloud Talk room, receive it
as a separate participant, and stream the received audio to the Modal STT
endpoint to verify intelligibility.

Requirements:
  - Modal credentials: MODAL_WORKSPACE, MODAL_KEY, MODAL_SECRET
  - Dependencies in .venv (uv pip install -r requirements.txt)

Usage:
  uv run python tools/roundtrip_modal.py \\
      --room-url https://cloud.codemyriad.io/call/erwcr27x \\
      --audio ../kyutai_modal/test_audio.wav \\
      --duration 30
"""

import argparse
import asyncio
import json
import re
import sys
import uuid
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import textwrap

import aiohttp
import av
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer
import os


def _parse_room_url(room_url: str) -> tuple[str, str]:
    parsed = urlparse(room_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid room URL: {room_url}")
    parts = [p for p in parsed.path.split("/") if p]
    token = parts[-1]
    return f"{parsed.scheme}://{parsed.netloc}", token


async def _fetch_requesttoken(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as resp:
        html = await resp.text()
    m = re.search(r'data-requesttoken="([^"]+)"', html)
    if not m:
        raise RuntimeError("requesttoken not found on room page")
    return m.group(1)


async def _ocs_post(session: aiohttp.ClientSession, base: str, path: str, body: dict, token: str) -> dict:
    async with session.post(
        f"{base}{path}",
        json=body,
        headers={
            "OCS-APIREQUEST": "true",
            "requesttoken": token,
        },
    ) as resp:
        data = await resp.json()
    return data["ocs"]["data"] or {}


async def _ocs_get(session: aiohttp.ClientSession, base: str, path: str, params: dict, token: str) -> dict:
    async with session.get(
        f"{base}{path}",
        params={"format": "json", **params},
        headers={
            "OCS-APIREQUEST": "true",
            "requesttoken": token,
        },
    ) as resp:
        return await resp.json()


def _build_ice_servers(settings: dict, overrides: dict | None = None) -> list[dict]:
    if overrides:
        servers: list[RTCIceServer] = []
        if overrides.get("stun"):
            servers.append(RTCIceServer(urls=overrides["stun"]))
        if overrides.get("turn"):
            servers.append(
                RTCIceServer(
                    urls=overrides["turn"],
                    username=overrides.get("turn_username"),
                    credential=overrides.get("turn_credential"),
                )
            )
        return servers
    servers: list[RTCIceServer] = []
    for stun in settings.get("stunservers", []) or []:
        # Prefer TCP/STUN if available, but include all.
        servers.append(RTCIceServer(urls=[u for u in stun["urls"] if "transport=tcp" in u] or stun["urls"]))
    for turn in settings.get("turnservers", []) or []:
        tcp_urls = [u for u in turn["urls"] if "transport=tcp" in u]
        servers.append(RTCIceServer(urls=tcp_urls or turn["urls"], username=turn["username"], credential=turn["credential"]))
    return servers


@dataclass
class ParticipantContext:
    label: str
    session: aiohttp.ClientSession
    requesttoken: str
    participant: dict
    signaling_session: Optional[str]
    features: dict
    settings: dict
    call_join: dict
    ws: websockets.WebSocketClientProtocol
    pc: RTCPeerConnection
    publish_sid: str
    subscribe_sid: Optional[str] = None
    remote_sessions: set[str] = field(default_factory=set)


class ModalStreamer:
    """Send received audio frames to Modal and print transcripts."""

    def __init__(self, workspace: str, key: str, secret: str) -> None:
        self.workspace = workspace
        self.key = key
        self.secret = secret
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.url = f"wss://{workspace}--kyutai-stt-rust-kyutaisttrustservice-serve.modal.run/v1/stream"
        self.bytes_sent = 0

    async def connect(self) -> None:
        headers = {"Modal-Key": self.key, "Modal-Secret": self.secret}
        print(f"[modal] connecting to {self.url}")
        self.ws = await websockets.connect(
            self.url,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=20,
            open_timeout=60,
            max_size=None,
        )
        print("[modal] connected")
        asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        assert self.ws
        async for message in self.ws:
            try:
                data = json.loads(message)
            except Exception:
                continue
            if data.get("type") == "token":
                print(data.get("text", ""), end="", flush=True)
            elif data.get("type") == "vad_end":
                print()
            elif data.get("type") == "error":
                print(f"[modal] error: {data.get('message')}")

    async def send_audio(self, pcm_f32_mono: bytes) -> None:
        if not self.ws:
            return
        await self.ws.send(pcm_f32_mono)
        self.bytes_sent += len(pcm_f32_mono)

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            self.ws = None


async def wait_ice_complete(pc: RTCPeerConnection) -> None:
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.get_event_loop().create_future()

    @pc.on("icegatheringstatechange")
    async def _on_ice():
        if pc.iceGatheringState == "complete" and not done.done():
            done.set_result(True)

    await done


def log_sdp_candidates(label: str, sdp: str) -> None:
    lines = [ln for ln in sdp.splitlines() if ln.startswith("a=candidate:")]
    types: dict[str, int] = {}
    for ln in lines:
        m = ln.split()
        if len(m) >= 8:
            typ = m[7]
            types[typ] = types.get(typ, 0) + 1
    print(f"[{label}] candidates: {len(lines)} (types: {types})")
    for ln in lines[:4]:
        print(f"    {ln}")
    if len(lines) > 4:
        print(f"    ... ({len(lines)-4} more)")


async def create_participant(label: str, room_url: str) -> ParticipantContext:
    base_url, room_token = _parse_room_url(room_url)
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    requesttoken = await _fetch_requesttoken(session, room_url)
    participant = await _ocs_post(
        session,
        base_url,
        f"/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants/active?format=json",
        {"force": True},
        requesttoken,
    )
    settings = (
        await _ocs_get(
            session,
            base_url,
            "/ocs/v2.php/apps/spreed/api/v3/signaling/settings",
            {"token": room_token},
            requesttoken,
        )
    )["ocs"]["data"]
    if settings.get("turnservers"):
        print(f"[{label}] turn servers available: {len(settings['turnservers'])}")
        print(f"[{label}] first turn server: {settings['turnservers'][0]}")
    call_join = await _ocs_post(
        session,
        base_url,
        f"/ocs/v2.php/apps/spreed/api/v4/call/{room_token}?format=json",
        {"flags": 3, "silent": False, "recordingConsent": False, "silentFor": []},
        requesttoken,
    )

    ws_url = settings["server"].replace("http", "ws").rstrip("/") + "/spreed"
    ws = await websockets.connect(ws_url, ping_interval=20, max_size=None)

    pc = RTCPeerConnection(RTCConfiguration(iceServers=_build_ice_servers(settings)))
    sid = uuid.uuid4().hex

    return ParticipantContext(
        label=label,
        session=session,
        requesttoken=requesttoken,
        participant=participant,
        signaling_session=None,
        features={},
        settings=settings,
        call_join=call_join,
        ws=ws,
        pc=pc,
        publish_sid=sid,
    )


async def signaling_hello(ctx: ParticipantContext, base_url: str, room_token: str) -> None:
    hello_version = "2.0" if ctx.settings["helloAuthParams"].get("2.0") else "1.0"
    features = ["chat-relay", "encryption"]
    # Use base backend URL to avoid double-appending PathToOcsSignalingBackend on the server.
    raw_auth_url = ctx.settings["helloAuthParams"][hello_version].get("url") or f"{base_url}/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
    from urllib.parse import urlparse
    parsed_auth = urlparse(raw_auth_url)
    # Ensure a trailing "/" so server-side path concatenation stays correct.
    base_backend = f"{parsed_auth.scheme}://{parsed_auth.netloc}/"
    msg = {
        "type": "hello",
        "hello": {
            "version": hello_version,
            "auth": {
                "url": base_backend,
                "params": ctx.settings["helloAuthParams"][hello_version],
            },
            "features": features,
        },
    }
    await ctx.ws.send(json.dumps(msg))
    async for raw in ctx.ws:
        data = json.loads(raw)
        if data.get("type") == "welcome":
            continue
        if data.get("type") == "hello":
            hello = data.get("hello", {})
            ctx.signaling_session = hello.get("sessionid")
            server_features = hello.get("server", {}).get("features", []) or []
            ctx.features = {feat: True for feat in server_features}
            await ctx.ws.send(json.dumps({"type": "room", "room": {"roomid": room_token, "sessionid": ctx.participant["sessionId"]}}))
            print(f"[{ctx.label}] signaling session={ctx.signaling_session} features={server_features}")
            return


def _message(recipient_session: str, data: dict) -> dict:
    return {
        "type": "message",
        "message": {
            "recipient": {"type": "session", "sessionid": recipient_session},
            "data": data,
        },
    }


async def send_offer(source: ParticipantContext, recipient_session: str, offer_sdp: str, sid: Optional[str] = None) -> None:
    payload = {
        "to": recipient_session,
        "sid": sid or source.publish_sid,
        "roomType": "video",
        "type": "offer",
        "payload": {"type": "offer", "sdp": offer_sdp, "nick": source.label},
    }
    await source.ws.send(json.dumps(_message(recipient_session, payload)))


async def send_answer(source: ParticipantContext, recipient_session: str, answer_sdp: str, sid: Optional[str] = None) -> None:
    payload = {
        "to": recipient_session,
        "sid": sid or source.subscribe_sid or source.publish_sid,
        "roomType": "video",
        "type": "answer",
        "payload": {"type": "answer", "sdp": answer_sdp},
    }
    await source.ws.send(json.dumps(_message(recipient_session, payload)))


async def send_candidate(source: ParticipantContext, recipient_session: str, candidate, sid: Optional[str] = None) -> None:
    cand_str = candidate["candidate"] if isinstance(candidate, dict) else candidate.candidate
    sdp_mid = candidate.get("sdpMid") if isinstance(candidate, dict) else candidate.sdpMid
    sdp_mline = candidate.get("sdpMLineIndex") if isinstance(candidate, dict) else candidate.sdpMLineIndex
    payload = {
        "candidate": {
            "candidate": cand_str,
            "sdpMid": sdp_mid,
            "sdpMLineIndex": sdp_mline,
        },
    }
    data = {
        "to": recipient_session,
        "sid": sid or source.subscribe_sid or source.publish_sid,
        "roomType": "video",
        "type": "candidate",
        "payload": payload,
    }
    await source.ws.send(json.dumps(_message(recipient_session, data)))


async def send_request_offer(source: ParticipantContext, recipient_session: str, sid: Optional[str] = None) -> None:
    if not recipient_session:
        return
    payload = {"type": "requestoffer", "roomType": "video"}
    await source.ws.send(json.dumps(_message(recipient_session, payload)))
    print(f"[{source.label}] requested offer from {recipient_session}")


async def roundtrip(room_url: str, audio_path: Path, duration: int, modal_workspace: str, modal_key: str, modal_secret: str, ice_overrides: dict | None = None) -> None:
    base_url, room_token = _parse_room_url(room_url)
    sender = await create_participant("publisher", room_url)
    receiver = await create_participant("listener", room_url)
    modal = ModalStreamer(modal_workspace, modal_key, modal_secret)

    try:
        print(f"[publisher] sessionId={sender.participant['sessionId']}")
        print(f"[listener] sessionId={receiver.participant['sessionId']}")
        # Hello + room join
        await signaling_hello(sender, base_url, room_token)
        await signaling_hello(receiver, base_url, room_token)
        print("[signaling] both participants joined room")
        receiver.remote_sessions.add(sender.signaling_session)
        sender.remote_sessions.add(receiver.signaling_session)

        # Attach publisher media
        player = MediaPlayer(audio_path.as_posix(), loop=True)
        sender.pc.addTrack(player.audio)
        receiver.pc.addTransceiver("audio", direction="recvonly")
        # Override ICE servers if provided
        if ice_overrides:
            override_cfg = {"iceServers": _build_ice_servers(sender.settings, ice_overrides)}
            sender.pc.iceServers = override_cfg["iceServers"]
            receiver.pc.iceServers = override_cfg["iceServers"]

        # Handle ICE
        @sender.pc.on("icecandidate")
        async def _send_candidate_sender(event):
            if event.candidate:
                print("[publisher] sending candidate")
                await send_candidate(sender, sender.signaling_session, event.candidate)

        @receiver.pc.on("icecandidate")
        async def _send_candidate_receiver(event):
            if event.candidate:
                print("[listener] sending candidate")
                target_sid = receiver.subscribe_sid or receiver.publish_sid
                await send_candidate(receiver, receiver.signaling_session, event.candidate, sid=target_sid)

        @sender.pc.on("connectionstatechange")
        async def _pub_state():
            print(f"[publisher] pc state={sender.pc.connectionState}")

        @receiver.pc.on("connectionstatechange")
        async def _sub_state():
            print(f"[listener] pc state={receiver.pc.connectionState}")

        @sender.pc.on("iceconnectionstatechange")
        async def _pub_ice_state():
            print(f"[publisher] iceConnectionState={sender.pc.iceConnectionState}")

        @receiver.pc.on("iceconnectionstatechange")
        async def _sub_ice_state():
            print(f"[listener] iceConnectionState={receiver.pc.iceConnectionState}")

        @sender.pc.on("icegatheringstatechange")
        async def _pub_ice():
            print(f"[publisher] iceGatheringState -> {sender.pc.iceGatheringState}")

        @receiver.pc.on("icegatheringstatechange")
        async def _sub_ice():
            print(f"[listener] iceGatheringState -> {receiver.pc.iceGatheringState}")

        # Modal hook
        await modal.connect()
        resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=24000)
        resampler_pub = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=24000)

        @receiver.pc.on("track")
        async def _on_track(track):
            print(f"[listener] track received: {track.kind}")
            if track.kind != "audio":
                return
            while True:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    pcm = resampled.to_ndarray().tobytes()
                    await modal.send_audio(pcm)

        @sender.pc.on("track")
        async def _on_track_pub(track):
            print(f"[publisher] track received: {track.kind}")
            if track.kind != "audio":
                return
            while True:
                frame = await track.recv()
                for resampled in resampler_pub.resample(frame):
                    pcm = resampled.to_ndarray().tobytes()
                    await modal.send_audio(pcm)

        async def make_offer(ctx: ParticipantContext, label: str, recipient: Optional[str] = None, sid: Optional[str] = None) -> None:
            offer = await ctx.pc.createOffer()
            await ctx.pc.setLocalDescription(offer)
            await wait_ice_complete(ctx.pc)
            print(f"[{label}] iceGatheringState={ctx.pc.iceGatheringState}")
            log_sdp_candidates(f"{label}-offer", ctx.pc.localDescription.sdp)
            target = recipient or ctx.signaling_session
            await send_offer(ctx, target, ctx.pc.localDescription.sdp, sid=sid)
            print(f"[{label}] offer (with candidates) sent to {target}")

        async def message_loop(ctx: ParticipantContext, label: str) -> None:
            async for raw in ctx.ws:
                data = json.loads(raw)
                if label == "listener":
                    print(f"[listener][ws] {data}")
                if data.get("type") == "event":
                    # Track remote sessions from join/update events so we can target requestoffer correctly.
                    evt = data.get("event", {})
                    if evt.get("type") == "join":
                        for entry in evt.get("join", []) or []:
                            sid = entry.get("sessionid")
                            if sid and sid != ctx.signaling_session:
                                ctx.remote_sessions.add(sid)
                    if evt.get("type") == "update":
                        users = evt.get("update", {}).get("users") or []
                        for user in users:
                            sid = user.get("sessionId") or user.get("sessionid")
                            if sid and sid != ctx.signaling_session:
                                ctx.remote_sessions.add(sid)
                    continue
                if data.get("type") != "message":
                    continue
                d = data["message"].get("data", {})
                sender_id = data["message"].get("sender", {}).get("sessionid", "unknown") if data.get("message") else "unknown"
                mtype = d.get("type")
                msg_sid = d.get("sid")
                if mtype == "answer":
                    if msg_sid and msg_sid not in {ctx.subscribe_sid, ctx.publish_sid}:
                        continue
                    await ctx.pc.setRemoteDescription(RTCSessionDescription(sdp=d["payload"]["sdp"], type="answer"))
                    print(f"[{label}] answer applied from {sender_id}")
                elif mtype == "offer":
                    # Offers arriving here are downstream (listener) offers; track separate sid.
                    if msg_sid and msg_sid != ctx.subscribe_sid:
                        ctx.subscribe_sid = msg_sid
                    sdp = d["payload"]["sdp"]
                    await ctx.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                    answer = await ctx.pc.createAnswer()
                    await ctx.pc.setLocalDescription(answer)
                    await wait_ice_complete(ctx.pc)
                    log_sdp_candidates(f"{label}-answer", ctx.pc.localDescription.sdp)
                    await send_answer(ctx, sender_id, ctx.pc.localDescription.sdp, msg_sid or ctx.subscribe_sid)
                    print(f"[{label}] sent answer to {sender_id}")
                elif mtype == "candidate":
                    if msg_sid and msg_sid not in {ctx.subscribe_sid, ctx.publish_sid}:
                        continue
                    cand = d.get("payload", {}).get("candidate")
                    if cand and cand.get("candidate"):
                        ice_cand = candidate_from_sdp(cand["candidate"])
                        ice_cand.sdpMid = cand.get("sdpMid")
                        ice_cand.sdpMLineIndex = cand.get("sdpMLineIndex")
                        await ctx.pc.addIceCandidate(ice_cand)
                        print(f"[{label}] added remote candidate from {sender_id}")
                elif mtype == "requestoffer":
                    await make_offer(ctx, label, recipient=sender_id, sid=msg_sid or ctx.sid)
                else:
                    print(f"[{label}] unhandled message type {mtype} sid={msg_sid} from {sender_id}")

        recv_task = asyncio.create_task(message_loop(receiver, "listener"))
        send_task = asyncio.create_task(message_loop(sender, "publisher"))
        await make_offer(sender, "publisher")
        if receiver.features.get("mcu"):
            # Mirror browser: request offer from remote participant sessions (no sid).
            # Try all known remotes every few seconds until a subscribe sid is set.
            async def _request_loop():
                while not receiver.subscribe_sid:
                    remotes = list(receiver.remote_sessions)
                    if remotes:
                        print(f"[listener] requesting offers from remotes: {remotes}")
                    for remote in remotes:
                        if remote != receiver.signaling_session:
                            await send_request_offer(receiver, remote)
                    await asyncio.sleep(5)
            asyncio.create_task(_request_loop())
        await asyncio.sleep(duration)
        recv_task.cancel()
        send_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recv_task
            await send_task
    finally:
        await modal.close()
        await sender.pc.close()
        await receiver.pc.close()
        await sender.ws.close()
        await receiver.ws.close()
        await sender.session.close()
        await receiver.session.close()
        print(f"[stats] sent {modal.bytes_sent} bytes to Modal")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Round-trip Talk audio and feed received audio to Modal STT.")
    parser.add_argument("--room-url", required=True, help="Talk room URL (guest/public).")
    parser.add_argument("--audio", required=True, help="WAV/PCM file to publish.")
    parser.add_argument("--duration", type=int, default=30, help="Seconds to run the round-trip (default: 30).")
    parser.add_argument("--stun", action="append", default=[], help="Override STUN URL (repeatable).")
    parser.add_argument("--turn-url", action="append", default=[], help="Override TURN URL (repeatable).")
    parser.add_argument("--turn-username", help="Override TURN username.")
    parser.add_argument("--turn-credential", help="Override TURN credential.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    ice_overrides = None
    if args.turn_url or args.stun or args.turn_username or args.turn_credential:
        ice_overrides = {
            "stun": args.stun,
            "turn": args.turn_url,
            "turn_username": args.turn_username,
            "turn_credential": args.turn_credential,
        }
    workspace = os.environ.get("MODAL_WORKSPACE")
    key = os.environ.get("MODAL_KEY")
    secret = os.environ.get("MODAL_SECRET")
    if not (workspace and key and secret):
        # Fallback: load from common .envrc locations if env not exported
        for env_file in ("../kyutai_modal/.envrc", ".envrc"):
            env_path = Path(env_file).resolve()
            if not env_path.exists():
                continue
            for line in env_path.read_text().splitlines():
                if "MODAL_WORKSPACE" in line and "=" in line:
                    workspace = workspace or line.split("=", 1)[1].strip().strip('"')
                elif "MODAL_KEY" in line and "=" in line:
                    key = key or line.split("=", 1)[1].strip().strip('"')
                elif "MODAL_SECRET" in line and "=" in line:
                    secret = secret or line.split("=", 1)[1].strip().strip('"')
    if not (workspace and key and secret):
        print("Missing Modal credentials (MODAL_WORKSPACE, MODAL_KEY, MODAL_SECRET)", file=sys.stderr)
        return 1
    try:
        asyncio.run(
            roundtrip(
                room_url=args.room_url,
                audio_path=Path(args.audio),
                duration=args.duration,
                modal_workspace=workspace,
                modal_key=key,
                modal_secret=secret,
                ice_overrides=ice_overrides,
            )
        )
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
