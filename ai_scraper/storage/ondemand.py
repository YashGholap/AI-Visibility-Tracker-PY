"""Storage operations for the on-demand flow — writes to ai_visibility_ondemand."""
from __future__ import annotations

import json
import logging

from sqlalchemy import Engine, text

from ai_scraper.models import ScrapeResult

log = logging.getLogger(__name__)


def insert_ondemand_result(
    engine: Engine,
    run_id: int,
    result: ScrapeResult,
    competitor_rankings: dict[str, int] | None,
) -> None:
    """Insert a single on-demand result under run_id.

    competitor_rankings is stored as JSON. Pass None (not an empty dict) to
    write SQL NULL — matches the Go binary's behavior when the on-demand
    request had no competitors.

    Ports the Go InsertOnDemandResult function.
    """
    rankings_json: str | None
    if competitor_rankings:
        rankings_json = json.dumps(competitor_rankings)
    else:
        rankings_json = None

    sql = text("""
        INSERT INTO ai_visibility_ondemand
            (run_id, query, source, internal_links, competitor_rankings, content)
        VALUES
            (:run_id, :query, :source, :internal_links, :competitor_rankings, :content)
    """)
    with engine.begin() as conn:
        conn.execute(sql, {
            "run_id": run_id,
            "query": result.query,
            "source": result.source,
            "internal_links": json.dumps(result.internal_links),
            "competitor_rankings": rankings_json,
            "content": result.response_text,
        })
    log.info(
        "on-demand insert: run_id=%s query=%r source=%s links=%d",
        run_id, result.query, result.source, len(result.internal_links),
    )


def is_already_inserted_ondemand(
    engine: Engine,
    run_id: int,
    query: str,
    source: str,
) -> bool:
    """Return True if a (run_id, query, source) row already exists.

    Ports the Go IsAlreadyInsertedOnDemand function.
    """
    sql = text("""
        SELECT COUNT(*)
        FROM ai_visibility_ondemand
        WHERE run_id = :run_id
          AND query = :query
          AND source = :source
    """)
    with engine.connect() as conn:
        n = conn.execute(sql, {
            "run_id": run_id,
            "query": query,
            "source": source,
        }).scalar()
    return bool(n)