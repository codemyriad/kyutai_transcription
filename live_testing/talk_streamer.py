#!/usr/bin/env python3
"""
Stream a local audio file into a public Nextcloud Talk room.

This script:
1) Loads the public room page to obtain cookies + requesttoken (CSRF)
2) Joins the conversation to get the signaling sessionId
3) Fetches signaling settings (server URL, hello auth params, STUN/TURN)
4) Joins the call (audio-only flags)
5) Connects to the signaling WebSocket, answers remote offers, and sends the audio

Dependencies (install locally): pip install requests aiortc websockets
"""

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Dict, Optional

import requests
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer
from aiortc import sdp as aiortc_sdp
from aiortc.contrib.media import MediaPlayer
import websockets


def fetch_requesttoken(session: requests.Session, base_url: str, room: str) -> str:
    resp = session.get(f"{base_url}/call/{room}")
    resp.raise_for_status()
    m = re.search(r'data-requesttoken="([^"]+)"', resp.text)
    if not m:
        raise RuntimeError("requesttoken not found in room HTML")
    return m.group(1)


def join_conversation(session: requests.Session, base_url: str, room: str, requesttoken: str) -> dict:
    resp = session.post(
        f"{base_url}/ocs/v2.php/apps/spreed/api/v4/room/{room}/participants/active",
        headers={
            "OCS-APIREQUEST": "true",
            "requesttoken": requesttoken,
            "Content-Type": "application/json",
        },
        params={"format": "json"},
        json={"force": True},
    )
    resp.raise_for_status()
    return resp.json()["ocs"]["data"]


def join_call(session: requests.Session, base_url: str, room: str, requesttoken: str, flags: int = 3) -> None:
    resp = session.post(
        f"{base_url}/ocs/v2.php/apps/spreed/api/v4/call/{room}",
        headers={
            "OCS-APIREQUEST": "true",
            "requesttoken": requesttoken,
            "Content-Type": "application/json",
        },
        params={"format": "json"},
        json={
            "flags": flags,  # IN_CALL(1) | WITH_AUDIO(2) = 3
            "silent": False,
            "recordingConsent": False,
            "silentFor": [],
        },
    )
    resp.raise_for_status()


def fetch_signaling_settings(session: requests.Session, base_url: str, room: str) -> dict:
    resp = session.get(
        f"{base_url}/ocs/v2.php/apps/spreed/api/v3/signaling/settings",
        headers={"OCS-APIREQUEST": "true"},
        params={"format": "json", "token": room},
    )
    resp.raise_for_status()
    return resp.json()["ocs"]["data"]


def build_ice_servers(settings: dict) -> list:
    ice_servers = []
    for s in settings.get("stunservers", []):
        ice_servers.append(RTCIceServer(urls=s.get("urls", [])))
    for t in settings.get("turnservers", []):
        ice_servers.append(
            RTCIceServer(
                urls=t.get("urls", []),
                username=t.get("username"),
                credential=t.get("credential"),
            )
        )
    return ice_servers


class TalkStreamer:
    def __init__(self, base_url: str, room: str, session_id: str, settings: dict, audio_file: Path):
        self.base_url = base_url
        self.room = room
        self.session_id = session_id
        self.settings = settings
        self.audio_file = audio_file
        self.ws = None
        self.peer_connections: Dict[str, RTCPeerConnection] = {}
        self.player = MediaPlayer(str(audio_file), loop=True)
        self.ice_servers = build_ice_servers(settings)
        self.requested_peers = set()
        self.publish_pc: Optional[RTCPeerConnection] = None
        self.publish_sid: Optional[str] = None
        self.status_dc = None

    async def connect_ws(self):
        server = self.settings["server"].rstrip("/")
        if server.startswith("https://"):
            ws_url = "wss://" + server[len("https://"):] + "/spreed"
        elif server.startswith("http://"):
            ws_url = "ws://" + server[len("http://"):] + "/spreed"
        else:
            ws_url = server + "/spreed"
        self.ws = await websockets.connect(ws_url)

        # Optionally read a welcome; ignore timeout
        try:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=2)
            parsed = json.loads(msg)
            if parsed.get("type") == "welcome":
                print("Welcome received")
        except Exception:
            pass

        # Hello
        hello_version = "2.0" if "2.0" in self.settings["helloAuthParams"] else "1.0"
        auth_params = self.settings["helloAuthParams"][hello_version]
        auth_url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v3/signaling/backend"
        hello_msg = {
            "type": "hello",
            "hello": {
                "version": hello_version,
                "auth": {
                    "url": auth_url,
                    "params": auth_params,
                },
            },
        }
        await self.ws.send(json.dumps(hello_msg))
        hello_resp = json.loads(await self.ws.recv())
        print("Hello response:", hello_resp)

        # Join room
        join_msg = {
            "type": "room",
            "room": {
                "roomid": self.room,
                "sessionid": self.session_id,
            },
        }
        await self.ws.send(json.dumps(join_msg))
        print("Joined signaling room")

        # Start publishing immediately (MCU expects us to offer)
        await self.start_publish()

    async def start_publish(self):
        pc = RTCPeerConnection(RTCConfiguration(iceServers=self.ice_servers))
        self.publish_pc = pc
        self.publish_sid = self.publish_sid or str(asyncio.get_event_loop().time()).replace('.', '')

        if self.player.audio:
            pc.addTransceiver(self.player.audio, direction="sendonly")
            # Status datachannel for MCU to pick up media state
            self.status_dc = pc.createDataChannel("status")

            @self.status_dc.on("open")
            def on_open():
                try:
                    self._send_status({"type": "audioOn"})
                    self._send_status({"type": "unmute", "payload": {"name": "audio"}})
                    self._send_status({"type": "speaking"})
                except Exception:
                    pass

        @pc.on("icecandidate")
        async def on_pub_icecandidate(event):
            if event.candidate is None:
                return
            payload = {
                "candidate": event.candidate.to_sdp(),
                "sdpMid": event.candidate.sdpMid,
                "sdpMLineIndex": event.candidate.sdpMLineIndex,
            }
            await self.send_call_message(
                to=self.session_id,
                sid=self.publish_sid,
                room_type="video",
                mtype="candidate",
                payload=payload,
            )

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        offer_payload = {"type": "offer", "sdp": pc.localDescription.sdp}
        # Send to MCU via own session id
        await self.send_call_message(to=self.session_id, sid=self.publish_sid, room_type="video", mtype="offer", payload=offer_payload)
        # Also broadcast to room (some setups expect this)
        await self.send_call_message(to=None, sid=self.publish_sid, room_type="video", mtype="offer", payload=offer_payload)
        print("Sent publish offer (to session and room)")

        @pc.on("connectionstatechange")
        async def on_state_change():
            print("Publish PC state:", pc.connectionState)

        # Signal current media state (unmute audio) to peers/MCU
        await self.send_media_state(audio=True, video=False)

    async def run(self):
        await self.connect_ws()

        async def listener():
            async for raw in self.ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "message":
                    data = msg["message"]["data"]
                    sender = msg["message"]["sender"]["sessionid"]
                    data["from"] = sender
                    await self.handle_signaling(data)
                elif mtype == "event":
                    await self.handle_event(msg.get("event", {}))
                else:
                    print("Unhandled message type:", mtype, msg)

        await listener()

    async def handle_event(self, event: dict):
        if event.get("type") != "update":
            return
        # In MCU mode, offers are pushed; nothing to request.
        return

    async def handle_signaling(self, data: dict):
        mtype = data.get("type")
        if mtype == "offer":
            await self.handle_offer(data)
        elif mtype == "candidate":
            print("Candidate from", data.get("from"))
            await self.handle_candidate(data)
        elif mtype == "answer":
            print("Answer from", data.get("from"))
            await self.handle_answer(data)
        else:
            print("Unhandled signaling data:", data)
        # Keep media state advertised
        await self.send_media_state(audio=True, video=False)

    def _pc_key(self, data: dict) -> str:
        return f"{data.get('from')}::{data.get('roomType','call')}::{data.get('sid','')}"

    async def handle_offer(self, data: dict):
        key = self._pc_key(data)
        print(f"Received offer from {data.get('from')}, roomType={data.get('roomType')}, sid={data.get('sid')}")

        pc = RTCPeerConnection(RTCConfiguration(iceServers=self.ice_servers))
        self.peer_connections[key] = pc

        # Media: send audio only
        if self.player.audio:
            pc.addTrack(self.player.audio)
            # Status channel to mimic client media messages
            self.status_dc = pc.createDataChannel("status")
            @self.status_dc.on("open")
            def on_status_open():
                self._send_status({"type": "audioOn"})
                self._send_status({"type": "speaking"})

        @pc.on("icecandidate")
        async def on_icecandidate(event):
            if event.candidate is None:
                return
            payload = {
                "candidate": event.candidate.to_sdp(),
                "sdpMid": event.candidate.sdpMid,
                "sdpMLineIndex": event.candidate.sdpMLineIndex,
            }
            await self.send_call_message(
                to=data["from"],
                sid=data.get("sid"),
                room_type=data.get("roomType"),
                mtype="candidate",
                payload=payload,
            )

        await pc.setRemoteDescription(RTCSessionDescription(sdp=data["payload"]["sdp"], type="offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        await self.send_call_message(
            to=data["from"],
            sid=data.get("sid"),
            room_type=data.get("roomType"),
            mtype="answer",
            payload={"type": "answer", "sdp": pc.localDescription.sdp},
        )
        print("Sent answer")

    async def handle_candidate(self, data: dict):
        if data.get("sid") == self.publish_sid and self.publish_pc:
            pc = self.publish_pc
        else:
            key = self._pc_key(data)
            pc = self.peer_connections.get(key)
            if not pc:
                print("Candidate for unknown peer, ignoring")
                return
        cand_payload = data.get("payload") or data
        candidate_sdp = cand_payload.get("candidate")
        # Some servers wrap the actual candidate dict in "candidate"
        if isinstance(candidate_sdp, dict) and "candidate" in candidate_sdp:
            cand_payload = candidate_sdp
            candidate_sdp = candidate_sdp.get("candidate")

        if candidate_sdp:
            try:
                ice = aiortc_sdp.candidate_from_sdp(candidate_sdp)
                ice.sdpMid = cand_payload.get("sdpMid") or cand_payload.get("sdpMid".lower())
                ice.sdpMLineIndex = cand_payload.get("sdpMLineIndex")
                await pc.addIceCandidate(ice)
            except Exception as exc:
                print("Failed to apply candidate, ignoring:", exc, "payload:", cand_payload)

    async def handle_answer(self, data: dict):
        if data.get("sid") != self.publish_sid or not self.publish_pc:
            return
        payload = data.get("payload") or data
        sdp = payload.get("sdp")
        if not sdp:
            return
        await self.publish_pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        print("Publish answer applied")

    async def send_call_message(self, to: str, sid: Optional[str], room_type: Optional[str], mtype: str, payload: dict):
        if not self.ws:
            return
        recipient = {"type": "session", "sessionid": to} if to else {"type": "room"}
        msg = {
            "type": "message",
            "message": {
                "recipient": recipient,
                "data": {
                    "to": to or "",
                    "sid": sid,
                    "roomType": room_type or "call",
                    "type": mtype,
                    "payload": payload,
                },
            },
        }
        await self.ws.send(json.dumps(msg))

    async def send_media_state(self, audio: bool, video: bool):
        media_state = {
            "audio": audio,
            "video": video,
            "screensharing": False,
            "virtualBackground": False,
        }
        await self.send_call_message(
            to=self.session_id,
            sid=self.publish_sid,
            room_type="video",
            mtype="media",
            payload=media_state,
        )
        # Also update status datachannel if present
        self._send_status({"type": "audioOn" if audio else "audioOff"})
        self._send_status({"type": "speaking" if audio else "stoppedSpeaking"})

    def _send_status(self, obj: dict):
        if self.status_dc and self.status_dc.readyState == "open":
            try:
                self.status_dc.send(json.dumps(obj))
            except Exception:
                pass


async def main(args):
    session = requests.Session()
    requesttoken = fetch_requesttoken(session, args.base_url, args.room)
    info = join_conversation(session, args.base_url, args.room, requesttoken)
    print("Joined conversation; sessionId:", info["sessionId"])

    settings = fetch_signaling_settings(session, args.base_url, args.room)
    print("Signaling server:", settings.get("server"))

    # Join call audio-only
    join_call(session, args.base_url, args.room, requesttoken, flags=3)
    print("Joined call (flags=3)")

    streamer = TalkStreamer(args.base_url, args.room, info["sessionId"], settings, Path(args.audio))
    try:
        await asyncio.wait_for(streamer.run(), timeout=args.duration)
    except asyncio.TimeoutError:
        print(f"Duration {args.duration}s reached, closing")
    finally:
        # Cleanup
        if streamer.publish_pc:
            await streamer.publish_pc.close()
        for pc in streamer.peer_connections.values():
            await pc.close()
        if streamer.ws:
            await streamer.ws.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stream audio file into a Nextcloud Talk room")
    parser.add_argument("--base-url", default="https://cloud.codemyriad.io", help="Nextcloud base URL")
    parser.add_argument("--room", default="erwcr27x", help="Room token")
    parser.add_argument("--audio", required=True, help="Path to WAV/Opus/etc. audio file")
    parser.add_argument("--duration", type=int, default=60, help="Seconds to stay connected (audio loops)")
    args = parser.parse_args()
    asyncio.run(main(args))
