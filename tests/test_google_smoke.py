"""End-to-end smoke test against real Google. Slow, network-dependent.

Run explicitly with:
    uv run pytest tests\\test_google_smoke.py -v -m slow -s
"""
from __future__ import annotations

import asyncio

import pytest

from ai_scraper.scrape.browser.pool import open_pool
from ai_scraper.scrape.scrapers.registry import get_scraper


@pytest.mark.slow
def test_google_scrape_returns_content_or_links():
    async def _run():
        # headless=False during dev — Google flags headless harder.
        # Flip back to True once we add stealth (Phase 2 anti-bot work).
        async with open_pool(headless=False) as pool:
            scraper = get_scraper("google_ai")
            async with pool.context() as ctx:
                return await scraper.scrape(ctx, "best crm software")

    result = asyncio.run(_run())
    assert result is not None
    print(f"\n---\nresponse_text len: {len(result.response_text)}")
    print(f"link count: {len(result.internal_links)}")
    for link in result.internal_links[:5]:
        print(f"  {link}")

    assert result.source == "google_ai"
    assert result.query == "best crm software"
    assert result.response_text or result.internal_links, "extracted nothing"