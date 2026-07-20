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
from ai_scraper.scrape.scrapers.base import NeedsFreshContextError

log = logging.getLogger(__name__)

# Per-platform concurrency limits. Platforms with aggressive rate-limiting
# get tighter bounds regardless of the global query concurrency setting.
# These are semaphores shared across ALL concurrent queries.
_PLATFORM_SEMAPHORES: dict[str, asyncio.Semaphore] = {}

_PLATFORM_MAX_CONCURRENT: dict[str, int] = {
    "chatgpt": 1,      # very aggressive rate-limiting, must be serial
    "perplexity": 2,   # moderate rate-limiting
    "google_ai": 3,    # patchright handles antibot well, 3 is safe
    "gemini": 2,       # google-owned, moderate limit
    "mock": 10,        # no limit for testing
}

_DEFAULT_PLATFORM_MAX = 2


def _get_platform_semaphore(name: str) -> asyncio.Semaphore:
    """Get (or create) the per-platform semaphore.

    Semaphores are created lazily on first use. Because asyncio.Semaphore
    must be created inside a running event loop, we can't create them at
    module load time.
    """
    if name not in _PLATFORM_SEMAPHORES:
        limit = _PLATFORM_MAX_CONCURRENT.get(name, _DEFAULT_PLATFORM_MAX)
        _PLATFORM_SEMAPHORES[name] = asyncio.Semaphore(limit)
    return _PLATFORM_SEMAPHORES[name]


_MAX_RETRIES = 3
_GRACE_PERIOD_S = 5.0


async def run_query(
    query: str,
    platforms: list[str] | None,
    pool: BrowserPool,
    per_platform_timeout_s: float = 180.0,
) -> list[ScrapeResult]:
    """Scrape one query across the requested platforms concurrently."""
    resolved = resolve_platforms(platforms)

    async def _one(name: str) -> ScrapeResult | None:
        scraper = get_scraper(name)
        last_error: Exception | None = None
        platform_sem = _get_platform_semaphore(name)

        for attempt in range(_MAX_RETRIES):
            # Acquire per-platform semaphore BEFORE opening a context so we
            # don't waste a browser context waiting for the semaphore.
            async with platform_sem:
                async with pool.context() as ctx:
                    task = asyncio.create_task(scraper.scrape(ctx, query))
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(task),
                            timeout=per_platform_timeout_s,
                        )
                    except asyncio.TimeoutError:
                        log.warning(
                            "[%s] timed out after %.0fs, cancelling",
                            name, per_platform_timeout_s,
                        )
                        task.cancel()
                        try:
                            await asyncio.wait_for(task, timeout=_GRACE_PERIOD_S)
                        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
                        return None
                    except NeedsFreshContextError as e:
                        log.warning(
                            "[%s] gated on attempt %d/%d, retrying with fresh context",
                            name, attempt + 1, _MAX_RETRIES,
                        )
                        last_error = e
                    except Exception:  # noqa: BLE001
                        log.exception("[%s] scrape failed", name)
                        return None

        log.warning(
            "[%s] exhausted %d attempts, giving up: %s",
            name, _MAX_RETRIES, last_error,
        )
        return None

    results = await asyncio.gather(*(_one(name) for name in resolved))
    return [r for r in results if r is not None]