"""BrowserPool recovery. Slow — launches a real Chromium, no network.

Regression cover for the 2026-09-02 weekly run: Chromium died 4h30m in, at
query 116/299, and because the pool kept the stale handle every remaining
query failed instantly with TargetClosedError. 184 queries drained in 10s.

Run explicitly with:
    xvfb-run -a uv run pytest tests/test_browser_pool_recovery.py -v -m slow -s -p no:asyncio
"""
from __future__ import annotations

import asyncio

import pytest

from ai_scraper.scrape.browser.pool import open_pool


@pytest.mark.slow
def test_pool_relaunches_after_browser_death():
    async def _run():
        async with open_pool(headless=False) as pool:
            async with pool.context() as ctx:
                page = await ctx.new_page()
                await page.goto("about:blank")

            first = pool._browser
            assert first is not None
            assert first.is_connected()

            # Simulate the failure: the Chromium process goes away while the
            # pool still holds the handle.
            await first.close()
            assert not first.is_connected()

            # Before the fix this raised TargetClosedError — and kept raising
            # for every remaining query in the run.
            async with pool.context() as ctx:
                page = await ctx.new_page()
                await page.goto("about:blank")
                assert await page.evaluate("1 + 1") == 2

            second = pool._browser
            assert second is not None
            assert second.is_connected()
            assert second is not first

    asyncio.run(_run())


@pytest.mark.slow
def test_pool_recycles_after_threshold():
    async def _run():
        async with open_pool(headless=False, recycle_after=2) as pool:
            async with pool.context():
                pass
            first = pool._browser
            async with pool.context():
                pass
            # Two contexts served and none live, so this lease should land on
            # a freshly launched process.
            async with pool.context():
                pass
            assert pool._browser is not first
            assert pool._browser is not None
            assert pool._browser.is_connected()

    asyncio.run(_run())


@pytest.mark.slow
def test_pool_does_not_recycle_while_a_context_is_live():
    async def _run():
        async with open_pool(headless=False, recycle_after=1) as pool:
            async with pool.context():
                first = pool._browser
                # Threshold is met, but a context is still checked out —
                # recycling here would kill an in-flight scrape.
                async with pool.context():
                    assert pool._browser is first

    asyncio.run(_run())
