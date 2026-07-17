"""Storage operations for the cron flow — writes to ai_visibility."""
from __future__ import annotations

import json
import logging
from datetime import date

from sqlalchemy import Engine, text

from ai_scraper.models import ScrapeResult, VisibilityQuery

log = logging.getLogger(__name__)

def get_visibility_queries(
    engine: Engine,
    project_id: str = "", 
) -> list[VisibilityQuery]:
    """Fetch queries from ai_visibility_queries.

    project_id: filter to a specific project; empty string = all projects.
    Ports the Go GetVisibiltyQueries function.
    """
    sql = "SELECT project_id, query, category, intent, search_volume FROM ai_visibility_queries"
    params: dict[str, str] = {}
    if project_id:
        sql += " WHERE project_id = :project_id"
        params["project_id"] = project_id

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    return [
        VisibilityQuery(
            project_id=r[0],
            query=r[1],
            category=r[2],
            intent=r[3],
            search_volume=r[4] or 0,
        )
        for r in rows
    ]

def insert_results(engine: Engine, results: list[ScrapeResult]) -> None:
    """Bulk INSERT into ai_visibility.

    Each ScrapeResult produces one row. Uses today's date for created_at.
    Ports the Go InsertResults function.
    """
    if not results:
        return
    
    sql = text("""
        INSERT INTO ai_visibility
            (project_id, query, category, intent, source, internal_links,
             content, search_volume, created_at)
        VALUES
            (:project_id, :query, :category, :intent, :source, :internal_links,
             :content, :search_volume, :created_at)
    """)
    today = date.today()
    rows = [
        {
            "project_id": r.project_id,
            "query": r.query,
            "category": r.category,
            "intent": r.intent,
            "source": r.source,
            "internal_links": json.dumps(r.internal_links),
            "content": r.response_text,
            "search_volume": r.search_volume,
            "created_at": today,
        }
        for r in results
    ]

    with engine.begin() as conn:
        conn.execute(sql, rows)
        log.info("inserted %d rows into ai_visibility", len(results))


def is_already_scraped(
    engine: Engine,
    project_id: str,
    query: str,
    source: str,
) -> bool:
    """Return True if this (project, query, source) was already scraped today
    with non-empty internal_links. Used by the cron flow for dedupe.

    Ports the Go IsAlreadyScraped function. Uses JSON_LENGTH() for the empty
    check since internal_links is a JSON column and string comparison on JSON
    columns doesn't behave the way it does on TEXT.
    """
    sql = text("""
        SELECT COUNT(*)
        FROM ai_visibility
        WHERE project_id = :project_id
          AND query = :query
          AND source = :source
          AND created_at = CURDATE()
          AND internal_links IS NOT NULL
          AND JSON_LENGTH(internal_links) > 0
    """)
    with engine.connect() as conn:
        n = conn.execute(sql, {
            "project_id": project_id,
            "query": query,
            "source": source,
        }).scalar()
    return bool(n)