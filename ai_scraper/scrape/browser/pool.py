"""BrowserPool: one long-lived Chromium process, per-scrape BrowserContext.

Design (from earlier discussion):
  - Chromium process launch is ~1-3s. New BrowserContext is ~50ms.
  - So we launch Chromium once per worker lifetime and lease one context
    per scrape, disposing on the way out. That gives cookie/storage
    isolation without the launch tax.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, BrowserContext, Playwright

from ai_scraper.scrape.browser.launcher import (
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
    launch_browser
)

log = logging.getLogger(__name__)

_BLOCKED_RESOURCES = {"image", "font", "media"}

class BrowserPool:
    def __init__(self, playwright: Playwright, headless: bool = True) -> None:
        self._playwright = playwright
        self._headless = headless
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._browser is None:
            self._browser = await launch_browser(
                self._playwright, headless=self._headless
            )
            log.info(
                "browser pool: launched Chromium (headless=%s)", self._headless
            )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
            log.info("browser pool: closed Chromium")


    @asynccontextmanager
    async def context(
        self,
        *,
        block_media: bool = True,
        user_agent: str | None = None,
        viewport: dict[str, int] | None = None
    ) -> AsyncIterator[BrowserContext]:
        """Yield a fresh BrowserContext; dispose on exit.

        block_media=True routes image/font/media requests to abort() —
        cuts bandwidth 60%+ and speeds up navigation. LLM answer streams
        don't need any of those.
        """
        if self._browser is None:
            raise RuntimeError('BrowserPool.start() must be called first')
        
        ctx = await self._browser.new_context(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            viewport= viewport or DEFAULT_VIEWPORT,
            locale="en-US",
        )

        if block_media:
            async def _route(route):
                if route.request.resource_type in _BLOCKED_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", _route)

        try:
            yield ctx
        finally:
            try:
                await ctx.close()
            except Exception:   # noqa: BLE001
                log.exception("failed to close browser context")


@asynccontextmanager
async def open_pool(headless: bool = True) -> AsyncIterator[BrowserPool]:
    """Convenience: start Playwright + pool, tear both down together."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        pool = BrowserPool(pw, headless=headless)
        await pool.start()
        try:
            yield pool
        finally:
            await pool.close()