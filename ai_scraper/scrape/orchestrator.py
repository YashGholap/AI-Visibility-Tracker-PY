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


# Fresh-context retries on a gate. 0 = give up on the platform for the current
# query and move on. An anonymous/IP gate (Perplexity quota, ChatGPT anon limit)
# is keyed on the IP, not cookies, so retrying with a fresh context on the same
# IP just re-hits the gate — on 2026-09-02 all 3 attempts gated identically and
# a single ChatGPT gate burned 3×90s. Raise this only on a residential IP where
# the gate is genuinely cookie-based.
_DEFAULT_GATE_RETRIES = 0
_GRACE_PERIOD_S = 5.0


async def run_query(
    query: str,
    platforms: list[str] | None,
    pool: BrowserPool,
    per_platform_timeout_s: float = 180.0,
    gate_retries: int = _DEFAULT_GATE_RETRIES,
) -> list[ScrapeResult]:
    """Scrape one query across the requested platforms concurrently."""
    resolved = resolve_platforms(platforms)
    max_attempts = max(1, gate_retries + 1)

    async def _one(name: str) -> ScrapeResult | None:
        scraper = get_scraper(name)
        last_error: Exception | None = None
        platform_sem = _get_platform_semaphore(name)

        for attempt in range(1, max_attempts + 1):
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
                        last_error = e
                        # Gated. A fresh context won't clear an IP-level gate,
                        # so unless retries are explicitly enabled we stop here
                        # and let the run move to the next query.
                        if attempt >= max_attempts:
                            break
                        log.warning(
                            "[%s] gated on attempt %d/%d, retrying with fresh context",
                            name, attempt, max_attempts,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("[%s] scrape failed", name)
                        return None

        log.warning(
            "[%s] gated, skipping to next query: %s", name, last_error,
        )
        return None

    results = await asyncio.gather(*(_one(name) for name in resolved))
    return [r for r in results if r is not None]