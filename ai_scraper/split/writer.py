"""INSERT rows into per-project tables. Uses schema.py for column list."""
from __future__ import annotations

import logging

from sqlalchemy import Engine, text

from ai_scraper.models import RankedRow
from ai_scraper.schema import project_table_insert_columns

log = logging.getLogger(__name__)


def insert_ranked_rows(
    engine: Engine,
    qualified_table: str,
    rows: list[RankedRow],
    max_competitors: int,
) -> int:
    """Bulk INSERT IGNORE into the per-project table.

    Uses INSERT IGNORE so re-runs are idempotent against the
    UNIQUE KEY uq_prompt_date (prompt_id, date). Returns the count of rows
    the writer attempted to insert (not the count MySQL actually accepted,
    since IGNORE swallows duplicates silently).

    max_competitors must match what ensure_project_table was called with
    or the placeholder count will drift.

    Ports Go's InsertRows from internal/db/writer.go.
    """
    if not rows:
        return 0
    
    cols = project_table_insert_columns(max_competitors)
    cols_list = ", ".join(cols)
    placeholder_list = ", ".join(f":{c}" for c in cols)
    # date and created_at are literals in the INSERT, not placeholders,
    # matching the Go behaviour (CURDATE(), NOW()).
    sql = text(
        f"INSERT IGNORE INTO {qualified_table} "
        f"({cols_list}, date, created_at) "
        f"VALUES ({placeholder_list}, CURDATE(), NOW())"
    )

    params_list = [_row_to_params(r, max_competitors) for r in rows]

    with engine.begin() as conn:
        conn.execute(sql, params_list)

    log.info("inserted %d rows into %s (IGNORE-MODE)", len(rows), qualified_table)
    return len(rows)









def _row_to_params(row: RankedRow, max_competitors: int) -> dict[str, object]:
    """Flatten a RankedRow into a param dict matching project_table_insert_columns."""
    params: dict[str, object] = {
        "prompt_id": row.prompt_id,
        "prompt": row.prompt,
        "client_url": row.client_url,
        "client_rank": row.client_rank,
        "search_volume": row.search_volume,
        "category": row.category,
        "intent": row.intent,
        "source": row.source,
    }
    # N competitor slots. Pad with None when the RankedRow has fewer
    # competitors than max_competitors (or when a slot's URL is None).
    for i in range(max_competitors):
        col_url = f"competitor_url{i + 1}"
        col_rank = f"competitor_rank{i + 1}"
        if i < len(row.competitors):
            c = row.competitors[i]
            params[col_url] = c.url
            params[col_rank] = c.rank
        else:
            params[col_url] = None
            params[col_rank] = None

    params["content"] = row.content
    return params