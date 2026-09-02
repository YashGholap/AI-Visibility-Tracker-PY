"""Playwright browser launch with stealth-oriented config.

Kept small for Phase 1. Deep anti-bot tuning per platform is Phase 2.
Here we just make the launched Chromium not scream 'I am a bot' via
its default flags.

Flags mirror the Go chromedp binary's launch config so the two produce
comparable fingerprints when hitting the same platforms.
"""
from __future__ import annotations

from patchright.async_api import Browser, Playwright

# Match the Go binary exactly. Google/others fingerprint UA versions;
# an outdated UA is itself a mild signal.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


async def launch_browser(playwright: Playwright, headless: bool = True) -> Browser:
    return await playwright.chromium.launch(
        headless=headless,
        args=[
            # anti-detection (matches Go)
            "--disable-blink-features=AutomationControlled",
            # stability (matches Go)
            "--disable-gpu",
            "--no-sandbox",
            # NOTE: --disable-dev-shm-usage is deliberately NOT set. It exists
            # to work around a tiny /dev/shm (Docker's 64 MiB default) by
            # pushing Chromium's shared memory into /tmp as regular files. On
            # the scrape host /dev/shm is 126 GiB and sits completely unused,
            # so the flag only bought us slower, disk-backed shared memory.
            # performance / noise reduction (matches Go)
            "--disable-extensions",
            "--disable-infobars",
            # start-maximized: Go binary sets this. Compatible with headless=new,
            # ignored in old headless mode. Kept for parity.
            "--start-maximized",
        ],
    )


DEFAULT_USER_AGENT = _UA
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}