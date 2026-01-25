"""Stream a local audio file into a Nextcloud Talk room as a guest.

Requirements:
    uv run --with playwright playwright install chromium

Usage:
    uv run --with playwright stream_audio_to_talk.py \
        --url https://cloud.codemyriad.io/call/erwcr27x \
        --nickname "Bot 1" \
        --audio /path/to/audio.wav \
        --duration 120

Notes:
    - The audio file must be a PCM WAV Chrome can open (16-bit, 48kHz recommended).
    - Chrome will loop the file; add trailing silence if you want a one-shot play-out.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page


async def _fill_nickname(page: Page, nickname: str) -> None:
    """Fill the nickname input using a few likely selectors."""
    selectors: Iterable[str] = (
        "input[name='displayName']",
        "input#displayName",
        "input[placeholder*='name' i]",
        "input[type='text']",
    )
    for selector in selectors:
        try:
            el = await page.wait_for_selector(selector, timeout=5_000)
            await el.fill(nickname)
            return
        except Exception:
            continue
    raise RuntimeError("Could not find a nickname input on the join screen.")


async def _click_join(page: Page) -> None:
    """Click the join/start button by accessible label."""
    candidate_names = ("Join", "Start", "Continue", "Enter")
    for name in candidate_names:
        try:
            await page.get_by_role("button", name=name, exact=False).click()
            return
        except Exception:
            continue
    raise RuntimeError("Could not find the join button.")


async def _ensure_mic_unmuted(page: Page) -> None:
    """Attempt to unmute microphone if the UI exposes a control."""
    try:
        # If there's an explicit "Unmute" button, click it.
        unmute = await page.get_by_role("button", name=lambda n: "unmute" in n.lower()).first
        if await unmute.is_visible():
            await unmute.click()
            return
    except Exception:
        pass

    try:
        mic_button = await page.get_by_role("button", name=lambda n: "microphone" in n.lower()).first
        if await mic_button.is_visible():
            pressed = await mic_button.get_attribute("aria-pressed")
            if pressed and pressed.lower() == "true":
                await mic_button.click()  # toggle to unmuted
    except Exception:
        # Best effort only; many UIs auto-unmute fake devices.
        return


async def _start_call_if_idle(page: Page) -> None:
    """If the room is idle, click the call start/join button."""
    button_labels = (
        "Start call",
        "Start a call",
        "Join call",
        "Enter call",
        "Join video call",
        "Join audio call",
        "Call",
    )
    for label in button_labels:
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue


def _parse_cookies(cookie_args: list[str], url: str) -> list[dict]:
    """Convert name=value strings into Playwright cookie dicts."""
    if not cookie_args:
        return []
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    cookies = []
    for raw in cookie_args:
        if "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        name = name.strip()
        value = value.strip()
        cookies.append(
            {
                "name": name,
                "value": value,
                "url": base_url,
                "secure": True,
            }
        )
    return cookies


async def stream_audio(
    url: str,
    nickname: str,
    audio_path: Path,
    duration: int,
    headless: bool,
    cookies: list[str],
) -> None:
    audio_path = audio_path.expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    launch_args = [
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={audio_path}",
        "--allow-file-access-from-files",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
        )
        if cookies:
            await context.add_cookies(_parse_cookies(cookies, url))
        page = await context.new_page()

        print(f"Opening {url}")
        await page.goto(url, wait_until="domcontentloaded")

        # Fill nickname and join
        await _fill_nickname(page, nickname)
        await _click_join(page)

        # Wait for call UI to load
        try:
            await page.wait_for_timeout(1000)
            await _start_call_if_idle(page)
            await _ensure_mic_unmuted(page)
        except Exception:
            pass

        print(f"Streaming {audio_path.name} as '{nickname}' for ~{duration}s ...")
        await page.wait_for_timeout(duration * 1000)
        print("Done. Closing browser.")

        await context.close()
        await browser.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream a WAV file into a Nextcloud Talk room.")
    parser.add_argument("--url", required=True, help="Talk room URL (guest share link).")
    parser.add_argument("--nickname", required=True, help="Display name to join with.")
    parser.add_argument("--audio", required=True, help="Path to PCM WAV audio file.")
    parser.add_argument("--duration", type=int, default=120, help="Seconds to stay in the call (default: 120).")
    parser.add_argument("--headful", action="store_true", help="Show the browser (headless by default).")
    parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        help="Optional cookie in name=value form (repeatable) for authenticated rooms.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        asyncio.run(
            stream_audio(
                url=args.url,
                nickname=args.nickname,
                audio_path=Path(args.audio),
                duration=args.duration,
                headless=not args.headful,
                cookies=args.cookie,
            )
        )
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
