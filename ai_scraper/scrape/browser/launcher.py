
"""Playwright browser launch with stealth-oriented config.

Kept small for Phase 1. Deep anti-bot tuning per platform is Phase 2.
Here we just make the launched Chromium not scream 'I am a bot' via
its default flags.
"""
from __future__ import annotations

from playwright.async_api import Browser, Playwright

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

async def launch_browser(playwright: Playwright, headless: bool = True) -> Browser:
    return await playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]
    )

DEFAULT_USER_AGENT = _UA
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}

