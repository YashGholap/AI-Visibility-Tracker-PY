"""BrowserPool: one long-lived Chromium process, per-scrape BrowserContext.

Design (from earlier discussion):
  - Chromium process launch is ~1-3s. New BrowserContext is ~50ms.
  - So we launch Chromium once per worker lifetime and lease one context
    per scrape, disposing on the way out. That gives cookie/storage
    isolation without the launch tax.

Resilience:
  A weekly cron run drives this pool (patchright's bundled Chromium) for 8+
  hours, and the browser does not reliably survive that. On 2026-09-02 it died
  4h30m in, at query 116/299, with 188 GB RAM free and no OOM kill. The host's
  fd soft limit was 1024 (since raised in run.sh) — the leading suspect, but
  unproven. What made it catastrophic was not the death but the handling:
  every subsequent new_context() raised TargetClosedError against the stale
  handle, draining the remaining 184 queries in ten seconds.

  So the pool checks liveness before every lease and relaunches on death —
  cause-agnostic by design, since whatever kills the browser produces the same
  dead handle. It also recycles the process every N contexts, bounding
  whatever resource might be leaking without needing to name it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from patchright.async_api import Browser, BrowserContext, Playwright
from patchright.async_api import Error as PlaywrightError

from ai_scraper.scrape.browser.launcher import DEFAULT_VIEWPORT, launch_browser

log = logging.getLogger(__name__)

_BLOCKED_RESOURCES = {"image", "font", "media"}

# Replace the Chromium process after this many leased contexts. Bounds how
# much cache/state a single process can accumulate over a long run.
DEFAULT_RECYCLE_AFTER = 150


class BrowserPool:
    def __init__(
        self,
        playwright: Playwright,
        headless: bool = True,
        *,
        block_media: bool = False,
        recycle_after: int = DEFAULT_RECYCLE_AFTER,
    ) -> None:
        self._playwright = playwright
        self._headless = headless
        self._block_media = block_media
        self._recycle_after = recycle_after
        self._browser: Browser | None = None
        # Serialises launch/relaunch. run_query fans out one coroutine per
        # platform, so without this several of them would race into launching
        # a Chromium each the moment one dies.
        self._lock = asyncio.Lock()
        self._live_contexts = 0
        self._contexts_served = 0

    async def start(self) -> None:
        await self._ensure_browser()

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._discard_browser()
                log.info("browser pool: closed Chromium")

    # --- internals -------------------------------------------------------

    def _alive(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def _discard_browser(self) -> None:
        """Close and forget the current browser. Caller must hold _lock."""
        if self._browser is None:
            return
        try:
            await self._browser.close()
        except Exception:  # noqa: BLE001
            # Expected when the process is already gone.
            log.debug("browser pool: close() on an already-dead browser")
        self._browser = None
        self._contexts_served = 0

    async def _ensure_browser(self) -> Browser:
        """Return a live browser, launching or relaunching as needed."""
        async with self._lock:
            if self._alive():
                recycle_due = (
                    self._recycle_after > 0
                    and self._contexts_served >= self._recycle_after
                    and self._live_contexts == 0
                )
                if not recycle_due:
                    assert self._browser is not None
                    return self._browser
                log.warning(
                    "browser pool: recycling Chromium proactively after %d contexts",
                    self._contexts_served,
                )
            elif self._browser is not None:
                log.warning(
                    "browser pool: Chromium died after %d contexts, relaunching",
                    self._contexts_served,
                )

            await self._discard_browser()
            self._browser = await launch_browser(
                self._playwright, headless=self._headless
            )
            log.info(
                "browser pool: launched Chromium (headless=%s)", self._headless
            )
            return self._browser

    async def _new_context(self, ctx_kwargs: dict) -> BrowserContext:
        """new_context(), surviving one browser death.

        patchright.async_api exports Error but not TargetClosedError, so we
        catch the public base class and let is_connected() tell us whether the
        process is actually gone — more robust than matching on message text.
        """
        last_error: PlaywrightError | None = None
        for attempt in (1, 2):
            browser = await self._ensure_browser()
            try:
                return await browser.new_context(**ctx_kwargs)
            except PlaywrightError as e:
                if browser.is_connected():
                    # Browser is healthy; the failure is something else and
                    # relaunching would not help.
                    raise
                last_error = e
                log.warning(
                    "browser pool: new_context failed on a dead browser "
                    "(attempt %d/2): %s",
                    attempt, e,
                )
        assert last_error is not None
        raise last_error

    # --- public lease ----------------------------------------------------

    @asynccontextmanager
    async def context(
        self,
        *,
        block_media: bool | None = None,
        user_agent: str | None = None,
        viewport: dict[str, int] | None = None
    ) -> AsyncIterator[BrowserContext]:
        """Yield a fresh BrowserContext; dispose on exit.

        block_media routes image/font/media requests to abort() — cuts
        bandwidth 60%+, and LLM answer streams don't need any of those. It is
        off by default: blocking images is a known bot signal and this host is
        already captcha'd heavily by Google, which costs more than the
        bandwidth is worth. Defaults to the pool's setting; pass explicitly to
        override for one lease.
        """
        ctx_kwargs: dict = {
            "viewport": viewport or DEFAULT_VIEWPORT,
            "locale": "en-US",
        }
        # Only override UA if caller explicitly asked. Patchright works best
        # when the UA matches the browser it's actually driving — an override
        # to a mismatched UA is itself a detection signal.
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent

        ctx = await self._new_context(ctx_kwargs)
        self._live_contexts += 1
        self._contexts_served += 1

        try:
            if self._block_media if block_media is None else block_media:
                async def _route(route):
                    if route.request.resource_type in _BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.continue_()
                await ctx.route("**/*", _route)

            yield ctx
        finally:
            self._live_contexts -= 1
            try:
                await ctx.close()
            except Exception:   # noqa: BLE001
                log.exception("failed to close browser context")


@asynccontextmanager
async def open_pool(
    headless: bool = True,
    *,
    block_media: bool = False,
    recycle_after: int = DEFAULT_RECYCLE_AFTER,
) -> AsyncIterator[BrowserPool]:
    """Convenience: start Playwright + pool, tear both down together."""
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        pool = BrowserPool(
            pw,
            headless=headless,
            block_media=block_media,
            recycle_after=recycle_after,
        )
        await pool.start()
        try:
            yield pool
        finally:
            await pool.close()
