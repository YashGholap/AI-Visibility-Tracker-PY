"""Storage operations for the splitter — reads ai_visibility rows for a project."""
from __future__ import annotations

import json
import logging

from sqlalchemy import Engine, text

from ai_scraper.models import VisibilityRow
from ai_scraper.schema import visibility_select_columns

log = logging.getLogger(__name__)


def load_visibility_rows(engine: Engine, project_id: str) -> list[VisibilityRow]:
    """Fetch today's ai_visibility rows for one project.

    SELECT column order comes from schema.visibility_select_columns() so it
    stays in sync with CARRIED_FIELDS.

    Ports the Go LoadVisibilityRows function.
    """
    cols = visibility_select_columns()
    cols_sql = ", ".join(cols)
    sql = text(f"""
        SELECT {cols_sql}
        FROM ai_visibility
        WHERE project_id = :project_id
          AND DATE(created_at) = CURDATE()
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql, {"project_id": project_id}).fetchall()

    out: list[VisibilityRow] = []
    for r in rows:
        # Column order: id, query, category, intent, source, search_volume,
        #               content, internal_links, created_at
        # (Matches visibility_select_columns() output.)
        row_dict = dict(zip(cols, r, strict=True))

        try:
            links_raw = row_dict.get("internal_links") or "[]"
            internal_links = json.loads(links_raw)
            if not isinstance(internal_links, list):
                internal_links = []
        except json.JSONDecodeError:
            log.warning(
                "vis row id=%s has invalid internal_links JSON, treating as empty",
                row_dict["id"],
            )
            internal_links = []

        out.append(VisibilityRow(
            id=row_dict["id"],
            query=row_dict["query"],
            category=row_dict.get("category"),
            intent=row_dict.get("intent"),
            source=row_dict["source"],
            internal_links=internal_links,
            content=row_dict.get("content"),
            search_volume=row_dict.get("search_volume"),
            created_at=row_dict["created_at"],
        ))

    return out