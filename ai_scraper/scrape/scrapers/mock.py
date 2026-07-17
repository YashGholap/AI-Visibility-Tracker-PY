"""A deterministic mock scraper for CLI testing.

Registered under the fake platform name 'mock'. Produces predictable
output that lets end-to-end tests assert the CLI + storage layers work
without needing browser or network.
"""
from __future__ import annotations

from playwright.async_api import BrowserContext

from ai_scraper.models import ScrapeResult


class MockScraper:
    name: str = "mock"

    async def scrape(self, context: BrowserContext, query: str) -> ScrapeResult:
        return ScrapeResult(
            query=query,
            source=self.name,
            internal_links=[
                f"https://example.com/{query.replace(' ', '-')}",
                "https://another.com/relevant",
                "https://client.com/landing",
            ],
            response_text=(
                f"Mock answer for query '{query}'. "
                "This is a stub response used for dev testing."
            ),
        )