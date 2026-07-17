"""Run one query across multiple platforms concurrently.

Mirrors Go's RunQuery in internal/service/runquery.go — same fan-out,
per-platform timeouts, per-platform failures don't abort the query.
"""
from __future__ import annotations

import asyncio
import logging

from ai_scraper.models import ScrapeResult
from ai_scraper.scrape.browser.pool import BrowserPool
from ai_scraper.scrape.scrapers.registry import get_scraper, resolve_platforms

log = logging.getLogger(__name__)


async def run_query(
    query: str,
    platforms: list[str] | None,
    pool: BrowserPool,
    per_platform_timeout_s: float = 180.0,
) -> list[ScrapeResult]:
    """Scrape one query across the requested platforms concurrently.

    Empty/None platforms → all registered. Unknown names raise ValueError
    before any browser work starts. Per-platform failures are logged and
    dropped from the returned list (matching Go behaviour).
    """
    resolved = resolve_platforms(platforms)

    async def _one(name: str) -> ScrapeResult | None:
        scraper = get_scraper(name)
        async with pool.context() as ctx:
            try:
                return await asyncio.wait_for(
                    scraper.scrape(ctx, query),
                    timeout=per_platform_timeout_s,
                )
            except asyncio.TimeoutError:
                log.warning("[%s] timed out after %.0fs", name, per_platform_timeout_s)
                return None
            except Exception:  # noqa: BLE001
                log.exception("[%s] scrape failed", name)
                return None

    results = await asyncio.gather(*(_one(name) for name in resolved))
    return [r for r in results if r is not None]