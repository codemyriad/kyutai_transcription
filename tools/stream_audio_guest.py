"""Join a public Nextcloud Talk room as a guest and stream a WAV file.

Default audio is ../kyutai_modal/test_audio.wav (relative to repo root).

Usage:
    uv run --with playwright playwright install chromium   # first time only
    uv run --with playwright python tools/stream_audio_guest.py \
        --url "$NEXTCLOUD_ROOM_URL" \
        --nickname "Bot 1" \
        --duration 60

Flags:
    --audio /path/to/file.wav   (defaults to ../kyutai_modal/test_audio.wav)
    --headful                   (show the browser, headless by default)
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Iterable

from playwright.async_api import Page, async_playwright


DEFAULT_AUDIO = Path(__file__).resolve().parent.parent / "kyutai_modal" / "test_audio.wav"
ALT_AUDIO = Path(__file__).resolve().parent.parent.parent / "kyutai_modal" / "test_audio.wav"


async def _fill_nickname(page: Page, nickname: str) -> None:
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
    raise RuntimeError("Could not find nickname input.")


async def _click_join(page: Page) -> None:
    buttons = (
        "Submit name and join",
        "Join",
        "Join call",
        "Join video call",
        "Join audio call",
        "Continue",
        "Enter",
        "Next",
        "Close",  # dialog may have only close/next; we prefer next if present
    )
    for label in buttons:
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue
    raise RuntimeError("Could not find join button.")


async def _start_call(page: Page) -> None:
    labels = (
        "Start call",
        "Start a call",
        "Join call",
        "Join video call",
        "Join audio call",
        "Call",
    )
    for label in labels:
        try:
            btn = page.get_by_role("button", name=label, exact=False)
            if await btn.is_visible():
                await btn.click()
                return
        except Exception:
            continue


async def _unmute(page: Page) -> None:
    try:
        unmute = page.get_by_role("button", name=lambda n: "unmute" in n.lower())
        if await unmute.is_visible():
            await unmute.click()
            return
    except Exception:
        pass
    try:
        mic = page.get_by_role("button", name=lambda n: "microphone" in n.lower())
        if await mic.is_visible():
            pressed = await mic.get_attribute("aria-pressed")
            if pressed and pressed.lower() == "true":
                await mic.click()
    except Exception:
        pass


async def stream_audio(url: str, nickname: str, audio_path: Path, duration: int, headless: bool) -> None:
    audio_path = audio_path.expanduser().resolve()
    if not audio_path.exists() and ALT_AUDIO.exists():
        audio_path = ALT_AUDIO.resolve()
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
        page = await context.new_page()

        print(f"[info] opening {url}")
        await page.goto(url, wait_until="domcontentloaded")

        await _fill_nickname(page, nickname)
        await _click_join(page)

        # Let the UI settle, then start call + unmute.
        await page.wait_for_timeout(1_000)
        await _start_call(page)
        await page.wait_for_timeout(500)
        await _unmute(page)

        print(f"[info] streaming {audio_path.name} as '{nickname}' for ~{duration}s")
        await page.wait_for_timeout(duration * 1000)
        print("[info] done, closing")

        await context.close()
        await browser.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream a WAV file into a public Nextcloud Talk room.")
    parser.add_argument("--url", required=True, help="Talk room URL (guest share link).")
    parser.add_argument("--nickname", default="Bot", help="Display name to join with.")
    parser.add_argument("--audio", default=str(DEFAULT_AUDIO), help="Path to PCM WAV audio file.")
    parser.add_argument("--duration", type=int, default=120, help="Seconds to stay in the call (default: 120).")
    parser.add_argument("--headful", action="store_true", help="Show the browser (headless by default).")
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
            )
        )
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
