#!/usr/bin/env python3
"""
Minimal helper to exercise the Nextcloud Talk public room APIs for manual testing.

It covers:
1) Grab public page -> cookies + CSRF requesttoken
2) Join conversation as guest (participants/active) to obtain sessionId
3) Fetch recent chat messages and optionally send a new message
4) Fetch signaling settings (server URL, hello auth params, STUN/TURN)
5) Optional: perform signaling hello over WebSocket (no media, just handshake)

Dependencies: python3, requests; websocket-client is optional for the WS hello.
    pip install requests websocket-client
"""

import argparse
import json
import re
import sys
from typing import Optional

import requests

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    websocket = None


def fetch_csrf_and_cookies(session: requests.Session, base_url: str, room_token: str) -> str:
    """Load the public room page and extract data-requesttoken."""
    url = f"{base_url}/call/{room_token}"
    resp = session.get(url)
    resp.raise_for_status()
    m = re.search(r'data-requesttoken="([^"]+)"', resp.text)
    if not m:
        raise RuntimeError("requesttoken not found in room HTML")
    return m.group(1)


def join_conversation(session: requests.Session, base_url: str, room_token: str, requesttoken: str) -> dict:
    """POST /participants/active to join; returns the JSON data payload."""
    url = f"{base_url}/ocs/v2.php/apps/spreed/api/v4/room/{room_token}/participants/active"
    resp = session.post(
        url,
        headers={
            "OCS-APIREQUEST": "true",
            "requesttoken": requesttoken,
            "Content-Type": "application/json",
        },
        params={"format": "json"},
        json={"force": True},
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload["ocs"]["data"]


def fetch_messages(session: requests.Session, base_url: str, room_token: str) -> list[dict]:
    """Fetch recent chat messages (oldest first in this limited page)."""
    url = f"{base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}"
    resp = session.get(
        url,
        headers={"OCS-APIREQUEST": "true"},
        params={
            "format": "json",
            "lookIntoFuture": 0,
            "lastKnownMessageId": 0,
            "limit": 10,
            "setReadMarker": 0,
            "includeLastKnown": 0,
        },
    )
    resp.raise_for_status()
    return resp.json()["ocs"]["data"]


def send_message(session: requests.Session, base_url: str, room_token: str, requesttoken: str, text: str, display_name: Optional[str]) -> dict:
    url = f"{base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{room_token}"
    resp = session.post(
        url,
        headers={
            "OCS-APIREQUEST": "true",
            "requesttoken": requesttoken,
            "Content-Type": "application/json",
        },
        params={"format": "json"},
        json={
            "message": text,
            "actorDisplayName": display_name or "CLI Bot",
        },
    )
    resp.raise_for_status()
    return resp.json()["ocs"]["data"]


def fetch_signaling_settings(session: requests.Session, base_url: str, room_token: str) -> dict:
    url = f"{base_url}/ocs/v2.php/apps/spreed/api/v3/signaling/settings"
    resp = session.get(
        url,
        headers={"OCS-APIREQUEST": "true"},
        params={"format": "json", "token": room_token},
    )
    resp.raise_for_status()
    return resp.json()["ocs"]["data"]


def ws_hello(server_url: str, hello_auth_params: dict, session_id: str, room_token: str) -> None:
    """Optional: perform signaling hello and room join over WebSocket."""
    if websocket is None:
        print("websocket-client not installed; skipping WS hello", file=sys.stderr)
        return

    ws_url = server_url.rstrip("/") + "/spreed"
    print(f"Connecting to signaling WebSocket {ws_url}")
    ws = websocket.create_connection(ws_url)

    # If the server sends a welcome, read it; otherwise continue.
    ws.settimeout(2)
    try:
        raw = ws.recv()
        if raw:
            msg = json.loads(raw)
            if msg.get("type") == "welcome":
                print("Received welcome:", msg)
    except Exception:
        pass

    hello_version = "2.0" if "2.0" in hello_auth_params else "1.0"
    auth_url = hello_auth_params.get("url") or f"{server_url.rstrip('/')}/../ocs/v2.php/apps/spreed/api/v3/signaling/backend"
    hello_msg = {
        "type": "hello",
        "hello": {
            "version": hello_version,
            "auth": {
                "url": auth_url,
                "params": hello_auth_params[hello_version],
            },
        },
    }
    ws.send(json.dumps(hello_msg))
    resp = json.loads(ws.recv())
    print("Hello response:", resp)

    join_msg = {
        "type": "room",
        "room": {
            "roomid": room_token,
            "sessionid": session_id,
        },
    }
    ws.send(json.dumps(join_msg))
    print("Room join sent")
    ws.close()


def main(args: argparse.Namespace) -> None:
    session = requests.Session()

    token = fetch_csrf_and_cookies(session, args.base_url, args.room)
    print(f"requesttoken: {token}")

    room_data = join_conversation(session, args.base_url, args.room, token)
    print("Joined conversation, sessionId:", room_data.get("sessionId"))

    msgs = fetch_messages(session, args.base_url, args.room)
    if msgs:
        latest = msgs[0]
        print("Latest message:", latest.get("actorDisplayName"), latest.get("message"))

    if args.message:
        sent = send_message(session, args.base_url, args.room, token, args.message, args.name)
        print("Sent message id:", sent.get("id"))

    signaling = fetch_signaling_settings(session, args.base_url, args.room)
    print("Signaling server:", signaling.get("server"))
    print("TURN servers:", signaling.get("turnservers"))

    if args.ws_hello:
        ws_hello(signaling["server"], signaling["helloAuthParams"], room_data.get("sessionId"), args.room)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe a public Nextcloud Talk room")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("NEXTCLOUD_URL", "https://example.com"),
        help="Nextcloud base URL (defaults to NEXTCLOUD_URL env var)",
    )
    parser.add_argument("--room", default="erwcr27x", help="Room token")
    parser.add_argument("--name", default="CLI Bot", help="Display name when sending a message")
    parser.add_argument("--message", help="Optional message to send")
    parser.add_argument("--ws-hello", action="store_true", help="Perform signaling WebSocket hello + room join (no media)")
    args = parser.parse_args()
    main(args)
