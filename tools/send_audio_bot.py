"""
Join a public Nextcloud Talk room via signaling (no browser) and publish a WAV file.

Usage:
  uv run python tools/send_audio_bot.py \\
      --room-url https://cloud.codemyriad.io/call/erwcr27x \\
      --audio /path/to/audio.wav \\
      --nickname "Bot" \\
      --duration 90
"""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import aiohttp
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from websockets import WebSocketClientProtocol


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
    return data["ocs"]["data"]


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


def _build_ice_servers(settings: dict) -> list[dict]:
    servers: list[dict] = []
    for stun in settings.get("stunservers", []) or []:
        servers.append({"urls": stun["urls"]})
    for turn in settings.get("turnservers", []) or []:
        servers.append({"urls": turn["urls"], "username": turn["username"], "credential": turn["credential"]})
    return servers


@dataclass
class Connection:
    pc: RTCPeerConnection
    sid: str
    target_session: Optional[str]
    player: MediaPlayer


class TalkStreamer:
    def __init__(self, room_url: str, audio_path: Path, nickname: str, duration: int) -> None:
        self.room_url = room_url
        self.audio_path = audio_path
        self.nickname = nickname
        self.duration = duration

        self.base_url, self.room_token = _parse_room_url(room_url)
        self.cookie_session: Optional[aiohttp.ClientSession] = None
        self.requesttoken: Optional[str] = None
        self.participant: Optional[dict] = None
        self.settings: Optional[dict] = None

        self.ws: Optional[WebSocketClientProtocol] = None
        self.connections: Dict[str, Connection] = {}
        self.publish_sid = f"{asyncio.get_event_loop().time():.0f}"
        self.hello_sent = False

    async def _bootstrap_http(self) -> None:
        self.cookie_session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        assert self.cookie_session

        self.requesttoken = await _fetch_requesttoken(self.cookie_session, self.room_url)
        self.participant = await _ocs_post(
            self.cookie_session,
            self.base_url,
            f"/ocs/v2.php/apps/spreed/api/v4/room/{self.room_token}/participants/active?format=json",
            {"force": True},
            self.requesttoken,
        )
        settings = await _ocs_get(
            self.cookie_session,
            self.base_url,
            "/ocs/v2.php/apps/spreed/api/v3/signaling/settings",
            {"token": self.room_token},
            self.requesttoken,
        )
        self.settings = settings["ocs"]["data"]
        await _ocs_post(
            self.cookie_session,
            self.base_url,
            f"/ocs/v2.php/apps/spreed/api/v4/call/{self.room_token}?format=json",
            {"flags": 3, "silent": False, "recordingConsent": False, "silentFor": []},
            self.requesttoken,
        )

    async def _send(self, msg: dict) -> None:
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        await self.ws.send(json.dumps(msg))

    async def _send_hello(self) -> None:
        if self.hello_sent:
            return
        assert self.settings
        hello_version = "2.0" if self.settings["helloAuthParams"].get("2.0") else "1.0"
        self.hello_sent = True
        await self._send(
            {
                "type": "hello",
                "hello": {
                    "version": hello_version,
                    "auth": {
                        "url": f"{self.base_url}/ocs/v2.php/apps/spreed/api/v3/signaling/backend",
                        "params": self.settings["helloAuthParams"][hello_version],
                    },
                },
            }
        )

    async def _send_room_join(self) -> None:
        assert self.participant
        await self._send(
            {
                "type": "room",
                "room": {"roomid": self.room_token, "sessionid": self.participant["sessionId"]},
            }
        )

    def _send_signal(self, conn: Connection, msg_type: str, payload: dict) -> None:
        recipient = {"type": "session", "sessionid": conn.target_session} if conn.target_session else {"type": "room"}
        data = {
            "to": conn.target_session or "",
            "sid": conn.sid,
            "roomType": "video",
            "type": msg_type,
            "payload": payload,
        }
        asyncio.create_task(self._send({"type": "message", "message": {"recipient": recipient, "data": data}}))

    async def _create_connection(self, sid: str, target_session: Optional[str]) -> Connection:
        if sid in self.connections:
            conn = self.connections[sid]
            conn.target_session = conn.target_session or target_session
            return conn

        assert self.settings
        player = MediaPlayer(self.audio_path.as_posix(), loop=True)
        pc = RTCPeerConnection({"iceServers": _build_ice_servers(self.settings)})
        transceiver = pc.addTransceiver(player.audio, direction="sendonly")
        transceiver.sender.replaceTrack(player.audio)

        conn = Connection(pc=pc, sid=sid, target_session=target_session, player=player)
        self.connections[sid] = conn

        @pc.on("icecandidate")
        async def _on_ice(event):
            if event.candidate:
                self._send_signal(
                    conn,
                    "candidate",
                    {
                        "candidate": {
                            "candidate": event.candidate.to_sdp().strip(),
                            "sdpMid": event.candidate.sdpMid,
                            "sdpMLineIndex": event.candidate.sdpMLineIndex,
                        }
                    },
                )

        return conn

    async def _send_offer(self, conn: Connection) -> None:
        offer = await conn.pc.createOffer()
        await conn.pc.setLocalDescription(offer)
        self._send_signal(conn, "offer", {"type": "offer", "sdp": conn.pc.localDescription.sdp, "nick": self.nickname})
        await self._send(
            {
                "type": "message",
                "message": {"recipient": {"type": "room"}, "data": {"type": "nickChanged", "payload": {"name": self.nickname}}},
            }
        )

    async def _handle_offer(self, from_session: str, data: dict) -> None:
        sid = data.get("sid") or f"sid-{int(asyncio.get_event_loop().time())}"
        conn = await self._create_connection(sid, from_session)
        sdp = data.get("payload", {}).get("sdp") or data.get("payload")
        await conn.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        answer = await conn.pc.createAnswer()
        await conn.pc.setLocalDescription(answer)
        self._send_signal(conn, "answer", {"type": "answer", "sdp": conn.pc.localDescription.sdp, "nick": self.nickname})

    async def _handle_answer(self, from_session: str, data: dict) -> None:
        sid = data.get("sid") or self.publish_sid
        conn = self.connections.get(sid)
        if not conn:
            print(f"[warn] answer for unknown sid {sid}")
            return
        conn.target_session = conn.target_session or from_session
        sdp = data.get("payload", {}).get("sdp") or data.get("payload")
        await conn.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
        print(f"[info] answer applied for sid={sid} from={from_session}")

    async def _handle_candidate(self, from_session: str, data: dict) -> None:
        sid = data.get("sid") or self.publish_sid
        conn = self.connections.get(sid)
        if not conn:
            print(f"[warn] candidate for unknown sid {sid}")
            return
        conn.target_session = conn.target_session or from_session
        cand = data.get("payload", {}).get("candidate") or {}
        if not cand.get("candidate"):
            return
        await conn.pc.addIceCandidate(
            {
                "candidate": cand["candidate"],
                "sdpMid": cand.get("sdpMid"),
                "sdpMLineIndex": cand.get("sdpMLineIndex"),
            }
        )

    async def run(self) -> None:
        audio_path = self.audio_path.expanduser().resolve()
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)

        await self._bootstrap_http()
        assert self.settings

        ws_url = self.settings["server"].replace("http", "ws").rstrip("/") + "/spreed"
        async with websockets.connect(ws_url, ping_interval=20, max_size=None) as ws:
            self.ws = ws

            hello_task = asyncio.create_task(self._send_hello())
            await hello_task

            # Run for duration seconds or until SIGINT/bye.
            done_event = asyncio.Event()

            async def _receiver():
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "welcome":
                        await self._send_hello()
                    elif msg.get("type") == "hello":
                        await self._send_room_join()
                    elif msg.get("type") == "room":
                        conn = await self._create_connection(self.publish_sid, None)
                        await self._send_offer(conn)
                    elif msg.get("type") == "message":
                        sender = msg["message"]["sender"]["sessionid"]
                        data = msg["message"]["data"]
                        if data["type"] == "offer":
                            await self._handle_offer(sender, data)
                        elif data["type"] == "answer":
                            await self._handle_answer(sender, data)
                        elif data["type"] == "candidate":
                            await self._handle_candidate(sender, data)
                    elif msg.get("type") == "bye":
                        done_event.set()
                        break

            recv_task = asyncio.create_task(_receiver())
            try:
                await asyncio.wait_for(done_event.wait(), timeout=self.duration)
            except asyncio.TimeoutError:
                pass
            finally:
                recv_task.cancel()

        await self._cleanup()

    async def _cleanup(self) -> None:
        for conn in self.connections.values():
            await conn.pc.close()
            conn.player.stop()
        if self.ws:
            try:
                await self._send({"type": "bye", "bye": {}})
            except Exception:
                pass
            await self.ws.close()
        if self.cookie_session:
            await self.cookie_session.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Headless Talk publisher bot (audio only, no browser).")
    parser.add_argument("--room-url", required=True, help="Talk room URL (guest/public share link).")
    parser.add_argument("--audio", required=True, help="Path to WAV/PCM file to send.")
    parser.add_argument("--nickname", default="Bot", help="Display name (sent in signaling payloads).")
    parser.add_argument("--duration", type=int, default=120, help="Seconds to stay connected (default: 120).")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    bot = TalkStreamer(room_url=args.room_url, audio_path=Path(args.audio), nickname=args.nickname, duration=args.duration)
    try:
        asyncio.run(bot.run())
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
