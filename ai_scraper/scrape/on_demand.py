"""On-demand flow: stdin JSON → scrape → stdout JSON.

Ports Go's runOnDemand from cmd/server/ondemand.go. FastAPI subprocess
contract is preserved exactly — stdin is OnDemandRequest, stdout is
OnDemandStatus, stderr is human logs.
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import Engine

from ai_scraper.config import Config
from ai_scraper.models import (
    OnDemandError,
    OnDemandRequest,
    OnDemandStatus,
)
from ai_scraper.scrape.browser.pool import open_pool
from ai_scraper.scrape.competitors import compute_competitor_rankings
from ai_scraper.scrape.orchestrator import run_query
from ai_scraper.scrape.scrapers.registry import resolve_platforms
from ai_scraper.storage.migrations import ensure_ondemand_table
from ai_scraper.storage.ondemand import (
    insert_ondemand_result,
    is_already_inserted_ondemand,
)

log = logging.getLogger(__name__)


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


async def run_ondemand(engine: Engine, config: Config) -> int:
    """Read OnDemandRequest from stdin, run scrapes, write OnDemandStatus to stdout.

    Returns the process exit code (0 = ok/partial, 1 = fatal).
    """
    ensure_ondemand_table(engine)

    try:
        raw = sys.stdin.read()
        req = OnDemandRequest.model_validate_json(raw)
    except Exception as e:
        log.error("on-demand: failed to parse stdin JSON: %s", e)
        status = OnDemandStatus(
            run_id="",
            status="error",
            errors=[OnDemandError(query="", error=f"invalid stdin: {e}")],
        )
        sys.stdout.write(status.model_dump_json())
        return 1

    log.info(
        "on-demand: run_id=%s queries=%d platforms=%s competitors=%d",
        req.run_id, len(req.queries), req.platforms or "all", len(req.competitors),
    )

    try:
        resolved_platforms = resolve_platforms(req.platforms or None)
    except ValueError as e:
        log.error("on-demand: %s", e)
        status = OnDemandStatus(
            run_id=req.run_id,
            status="error",
            errors=[OnDemandError(query="", error=str(e))],
        )
        sys.stdout.write(status.model_dump_json())
        return 1

    queries = _dedupe_preserving_order(req.queries)

    errors: list[OnDemandError] = []
    rows_inserted = 0
    queries_processed = 0

    async with open_pool(headless=config.browser_headless) as pool:
        for i, query in enumerate(queries, start=1):
            log.info("on-demand: [%d/%d] query=%r", i, len(queries), query)
            queries_processed += 1

            pending = [
                p for p in resolved_platforms
                if not is_already_inserted_ondemand(engine, req.run_id, query, p)
            ]
            if not pending:
                log.info("on-demand: all platforms already inserted for this run")
                continue

            results = await run_query(
                query,
                pending,
                pool,
                per_platform_timeout_s=config.per_platform_timeout_seconds,
            )

            for r in results:
                if not r.internal_links:
                    log.warning(
                        "on-demand: dropping empty result source=%s query=%r",
                        r.source, r.query,
                    )
                    errors.append(OnDemandError(
                        query=r.query,
                        source=r.source,
                        error="no internal_links extracted",
                    ))
                    continue

                rankings = compute_competitor_rankings(
                    r.internal_links, req.competitors,
                )
                try:
                    insert_ondemand_result(engine, req.run_id, r, rankings)
                    rows_inserted += 1
                except Exception as e:  # noqa: BLE001
                    log.exception("on-demand: insert failed for %s", r.source)
                    errors.append(OnDemandError(
                        query=r.query, source=r.source,
                        error=f"db insert failed: {e}",
                    ))

            got_sources = {r.source for r in results}
            missing = [p for p in pending if p not in got_sources]
            for p in missing:
                errors.append(OnDemandError(
                    query=query, source=p,
                    error="scraper returned no result",
                ))

    if errors and rows_inserted > 0:
        status_str = "partial"
    elif errors and rows_inserted == 0:
        status_str = "error"
    else:
        status_str = "ok"

    status = OnDemandStatus(
        run_id=req.run_id,
        status=status_str,
        queries_processed=queries_processed,
        rows_inserted=rows_inserted,
        errors=errors,
    )
    sys.stdout.write(status.model_dump_json())
    log.info(
        "on-demand: done  status=%s rows_inserted=%d errors=%d",
        status_str, rows_inserted, len(errors),
    )
    return 0