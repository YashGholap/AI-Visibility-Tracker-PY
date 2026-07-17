"""Cron scrape flow: read queries → scrape → write to ai_visibility.

Ports Go's runCron from cmd/server/main.go plus its per-platform dedupe.
"""
from __future__ import annotations

import logging

from sqlalchemy import Engine

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

    inserted = 0
    async with open_pool(headless=config.browser_headless) as pool:
        for i, q in enumerate(queries, start=1):
            log.info(
                "cron: [%d/%d] project=%s query=%r",
                i, len(queries), q.project_id, q.query,
            )

            pending = [
                p for p in resolved_platforms
                if not is_already_scraped(engine, q.project_id, q.query, p)
            ]
            if not pending:
                log.info("cron: all platforms already scraped today, skipping")
                continue

            results = await run_query(
                q.query,
                pending,
                pool,
                per_platform_timeout_s=config.per_platform_timeout_seconds,
            )

            enriched: list[ScrapeResult] = []
            for r in results:
                # Drop only if BOTH content and links are empty — a scrape
                # with content but no links is still useful for mention
                # counting analytics; drop only when nothing was captured.
                if not r.internal_links and not r.response_text:
                    log.warning(
                        "cron: dropping empty result  source=%s query=%r",
                        r.source, r.query,
                    )
                    continue
                if not r.internal_links:
                    log.warning(
                        "cron: no links extracted (keeping row)  source=%s query=%r links=0",
                        r.source, r.query,
                    )
                r.project_id = q.project_id
                r.category = q.category
                r.intent = q.intent
                r.search_volume = q.search_volume
                enriched.append(r)

            if enriched:
                insert_results(engine, enriched)
                inserted += len(enriched)

    log.info("cron: done  inserted=%d", inserted)
    return inserted