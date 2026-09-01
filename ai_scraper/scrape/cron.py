"""Cron scrape flow: read queries → scrape → write to ai_visibility.

Ports Go's runCron from cmd/server/main.go plus its per-platform dedupe.
"""
from __future__ import annotations

import logging

from sqlalchemy import Engine
import asyncio

from ai_scraper.config import Config
from ai_scraper.models import ScrapeResult
from ai_scraper.scrape.browser.pool import open_pool
from ai_scraper.scrape.orchestrator import run_query
from ai_scraper.scrape.scrapers.registry import resolve_platforms
from ai_scraper.storage.migrations import ensure_ai_visibility
from ai_scraper.storage.visibility import (
    get_visibility_queries,
    insert_results,
    is_already_scraped,
)

log = logging.getLogger(__name__)


def _parse_platforms(spec: str) -> list[str] | None:
    """Parse comma-separated platforms config into a list, or None if empty."""
    spec = spec.strip()
    if not spec:
        return None
    return [p.strip() for p in spec.split(",") if p.strip()]


async def run_cron(engine: Engine, config: Config) -> int:
    """Run the cron scrape flow.

    Returns count of rows inserted into ai_visibility.
    """
    ensure_ai_visibility(engine)

    project_filter = config.project_id.strip()
    platforms = _parse_platforms(config.platforms)

    try:
        resolved_platforms = resolve_platforms(platforms)
    except ValueError as e:
        log.error("platform validation failed: %s", e)
        raise

    log.info(
        "cron: starting  project_filter=%s  platforms=%s",
        project_filter or "all", resolved_platforms,
    )

    queries = get_visibility_queries(engine, project_id=project_filter, limit=config.query_limit)
    log.info("cron: loaded %d queries", len(queries))

    if not queries:
        return 0

    concurrency = max(1, config.query_concurrency)
    log.info("Cron: query concurrency=%d", concurrency)

    async with open_pool(headless=config.browser_headless) as pool:
        semaphore = asyncio.Semaphore(concurrency)
        total = len(queries)
        counter = {"started": 0, "done": 0, "inserted": 0}

        async def _process(q) -> None:
            async with semaphore:
                counter["started"] += 1
                idx = counter["started"]
                log.info(
                    "cron: [%d/%d] START project=%s query=%r",
                    idx, total, q.project_id, q.query,
                )

                pending = [
                    p for p in resolved_platforms
                    if not is_already_scraped(engine, q.project_id, q.query, p)
                ]
                if not pending:
                    log.info(
                        "cron: [%d/%d] all platforms already scraped, skipping",
                        idx, total,
                    )
                    counter["done"] = 1
                    return
                try:
                    results = await run_query(
                        q.query,
                        pending,
                        pool,
                        per_platform_timeout_s=config.per_platform_timeout_seconds
                    )
                except Exception: # noqa: BLE001
                    log.exception(
                        "cron: [%d/%d] run_query crashed  project=%s query=%r",
                        idx, total, q.project_id, q.query,
                    )
                    counter["done"] += 1
                    return
                
                enriched: list[ScrapeResult] = []
                for r in results:
                    if not r.internal_links and not r.response_text:
                        log.warning(
                            "cron: [%d/%d] dropping empty result  source=%s query=%r",
                            idx, total, r.source, r.query,
                        )
                        continue
                    if not r.internal_links:
                        log.warning(
                            "cron: [%d/%d] no links extracted (keeping row)  "
                            "source=%s query=%r links=0",
                            idx, total, r.source, r.query,
                        )
                    r.project_id = q.project_id
                    r.category = q.category
                    r.intent = q.intent
                    r.search_volume = q.search_volume
                    enriched.append(r)
                
                if enriched:
                    try:
                        insert_results(engine, enriched)
                        counter["inserted"] += len(enriched)
                        log.info(
                            "cron: [%d/%d] DONE inserted=%d",
                            idx, total, len(enriched),
                        )
                    except Exception:   # noqa: BLE001
                        log.exception(
                            "cron: [%d/%d] insert failed  project=%s query=%r",
                            idx, total, q.project_id, q.query,
                        )
                counter["done"] += 1

        await asyncio.gather(*(_process(q) for q in queries))
        inserted = counter["inserted"]

    log.info("cron: done  inserted=%d", inserted)
    return inserted