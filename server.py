"""
ai_scraper HTTP server — FastAPI wrapper around the on-demand scrape flow.

Runs as a systemd service on the same host as the FastAPI backend. The
backend inserts ondemand_runs / ondemand_chunks rows, then Celery workers
POST chunk requests here. This service returns immediately, runs the
scrape in the background under xvfb + patchright, writes results to
ai_visibility_ondemand, and updates chunk/run status directly.

Chunk status vocab matches app/ai_visibility/scrape_runner/ddl.py:
    pending | running | success | failed
Run status vocab matches ondemand_runs:
    pending | running | completed | partial | failed
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

log = logging.getLogger(__name__)

_engine = None
_config = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from ai_scraper.config import load_config
    from ai_scraper.storage.connection import get_engine
    from ai_scraper.storage.migrations import (
        ensure_ondemand_table,
        ensure_ondemand_chunks_table,
    )

    _config = load_config()
    _engine = get_engine(_config)
    ensure_ondemand_table(_engine)
    ensure_ondemand_chunks_table(_engine)

    log.info(
        "ai-scraper server ready  headless=%s  port=%s",
        _config.browser_headless,
        os.environ.get("AI_SCRAPER_SERVER_PORT", "8765"),
    )
    yield
    log.info("ai-scraper server shutting down")


app = FastAPI(
    title="ai-scraper",
    description="Internal scrape API — called by the FastAPI backend Celery workers.",
    lifespan=lifespan,
)


class ChunkRequest(BaseModel):
    run_id:      int
    chunk_index: int
    queries:     List[str]
    platforms:   List[str] = []
    competitors: List[str] = []


class ChunkResponse(BaseModel):
    status:      str
    run_id:      int
    chunk_index: int
    queries:     int


def _set_chunk_status(
    run_id: int,
    chunk_index: int,
    status: str,
    rows_inserted: int = 0,
    error: str | None = None,
) -> None:
    with _engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE ondemand_chunks
                   SET status        = :status,
                       rows_inserted = :rows,
                       error         = :error
                 WHERE run_id      = :run_id
                   AND chunk_index = :chunk_index
            """),
            {
                "status":      status,
                "rows":        rows_inserted,
                "error":       error,
                "run_id":      run_id,
                "chunk_index": chunk_index,
            },
        )


def _update_run_status(run_id: int) -> None:
    """Recompute ondemand_runs status/counters from all its chunks."""
    with _engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT status, COUNT(*) AS n
                  FROM ondemand_chunks
                 WHERE run_id = :run_id
                 GROUP BY status
            """),
            {"run_id": run_id},
        ).mappings().all()

        counts  = {r["status"]: int(r["n"]) for r in rows}
        total   = sum(counts.values())
        success = counts.get("success", 0)
        failed  = counts.get("failed", 0)

        if counts.get("running", 0) > 0 or counts.get("pending", 0) > 0:
            run_status = "running"
        elif success == total:
            run_status = "completed"
        elif failed == total:
            run_status = "failed"
        else:
            run_status = "partial"

        total_rows = conn.execute(
            text("""
                SELECT COALESCE(SUM(rows_inserted), 0)
                  FROM ondemand_chunks
                 WHERE run_id = :run_id
            """),
            {"run_id": run_id},
        ).scalar()

        conn.execute(
            text("""
                UPDATE ondemand_runs
                   SET status        = :status,
                       rows_inserted = :rows,
                       chunks_done   = :done,
                       chunks_failed = :failed,
                       updated_at    = :now
                 WHERE run_id = :run_id
            """),
            {
                "status": run_status,
                "rows":   total_rows,
                "done":   success + failed,
                "failed": failed,
                "now":    datetime.now(timezone.utc),
                "run_id": run_id,
            },
        )

    log.info(
        "[run_id=%d] status -> %s  success=%d/%d failed=%d rows=%d",
        run_id, run_status, success, total, failed, total_rows,
    )


async def _run_chunk(
    run_id: int,
    chunk_index: int,
    queries: List[str],
    platforms: List[str],
    competitors: List[str],
) -> None:
    from ai_scraper.scrape.browser.pool import open_pool
    from ai_scraper.scrape.competitors import compute_competitor_rankings
    from ai_scraper.scrape.orchestrator import run_query
    from ai_scraper.scrape.scrapers.registry import resolve_platforms
    from ai_scraper.storage.ondemand import (
        insert_ondemand_result,
        is_already_inserted_ondemand,
    )

    log.info(
        "[chunk] run_id=%d chunk=%d starting  queries=%d platforms=%s competitors=%d",
        run_id, chunk_index, len(queries), platforms or "all", len(competitors),
    )

    _set_chunk_status(run_id, chunk_index, "running")

    try:
        resolved_platforms = resolve_platforms(platforms or None)
    except ValueError as e:
        log.error("[chunk] run_id=%d chunk=%d bad platforms: %s", run_id, chunk_index, e)
        _set_chunk_status(run_id, chunk_index, "failed", error=str(e))
        _update_run_status(run_id)
        return

    seen: set[str] = set()
    deduped: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)

    rows_inserted = 0
    errors: list[str] = []

    try:
        async with open_pool(headless=_config.browser_headless) as pool:
            for i, query in enumerate(deduped, start=1):
                log.info(
                    "[chunk] run_id=%d chunk=%d [%d/%d] query=%r",
                    run_id, chunk_index, i, len(deduped), query,
                )

                pending = [
                    p for p in resolved_platforms
                    if not is_already_inserted_ondemand(_engine, run_id, query, p)
                ]
                if not pending:
                    log.info(
                        "[chunk] run_id=%d all platforms already inserted for query=%r",
                        run_id, query,
                    )
                    continue

                results = await run_query(
                    query,
                    pending,
                    pool,
                    per_platform_timeout_s=_config.per_platform_timeout_seconds,
                )

                for r in results:
                    if not r.internal_links:
                        log.warning(
                            "[chunk] dropping empty result source=%s query=%r",
                            r.source, r.query,
                        )
                        errors.append(f"no links: {r.source} / {r.query!r}")
                        continue

                    rankings = compute_competitor_rankings(r.internal_links, competitors)
                    try:
                        insert_ondemand_result(_engine, run_id, r, rankings)
                        rows_inserted += 1
                    except Exception as e:
                        log.exception(
                            "[chunk] insert failed source=%s query=%r", r.source, r.query
                        )
                        errors.append(f"insert failed {r.source}: {e}")

                got = {r.source for r in results}
                for p in pending:
                    if p not in got:
                        errors.append(f"no result: {p} / {query!r}")

    except Exception as e:
        log.exception("[chunk] run_id=%d chunk=%d fatal error", run_id, chunk_index)
        _set_chunk_status(run_id, chunk_index, "failed", rows_inserted, str(e)[:500])
        _update_run_status(run_id)
        return

    if errors and rows_inserted == 0:
        final_status = "failed"
        error_str    = "; ".join(errors[:5])
    elif errors:
        final_status = "success"
        error_str    = "; ".join(errors[:5])
    else:
        final_status = "success"
        error_str    = None

    _set_chunk_status(run_id, chunk_index, final_status, rows_inserted, error_str)
    _update_run_status(run_id)

    log.info(
        "[chunk] run_id=%d chunk=%d %s  rows=%d errors=%d",
        run_id, chunk_index, final_status, rows_inserted, len(errors),
    )


@app.post("/ondemand", response_model=ChunkResponse)
async def run_ondemand(body: ChunkRequest, background_tasks: BackgroundTasks):
    if not body.queries:
        raise HTTPException(status_code=400, detail="queries must not be empty")

    background_tasks.add_task(
        _run_chunk,
        body.run_id,
        body.chunk_index,
        body.queries,
        body.platforms,
        body.competitors,
    )

    log.info(
        "[server] accepted run_id=%d chunk=%d queries=%d competitors=%d",
        body.run_id, body.chunk_index, len(body.queries), len(body.competitors),
    )

    return ChunkResponse(
        status="started",
        run_id=body.run_id,
        chunk_index=body.chunk_index,
        queries=len(body.queries),
    )


@app.get("/health")
async def health():
    try:
        with _engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db error: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("AI_SCRAPER_SERVER_PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")