"""Storage operations for the splitter — reads the projects table."""
from __future__ import annotations

import json
import logging

from sqlalchemy import Engine, text

from ai_scraper.models import Project

log = logging.getLogger(__name__)


def load_projects(
    engine: Engine,
    max_competitors: int,
    project_ids: list[str] | None = None,
) -> list[Project]:
    """Load projects from the `projects` table.

    project_ids: if non-empty, filter to these specific IDs; otherwise all.
    max_competitors: trim each project's competitor_urls to at most this many.

    Ports the Go LoadProjects function.
    """
    sql = (
        "SELECT project_id, project_name, client_website_url, "
        "COALESCE(competitor_urls, '[]') "
        "FROM projects"
    )
    params: dict[str, str] = {}

    if project_ids:
        placeholders = ", ".join(f":pid{i}" for i in range(len(project_ids)))
        sql += f" WHERE project_id IN ({placeholders})"
        params = {f"pid{i}": pid for i, pid in enumerate(project_ids)}

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    projects: list[Project] = []
    for r in rows:
        try:
            competitor_urls = json.loads(r[3]) if r[3] else []
            if not isinstance(competitor_urls, list):
                competitor_urls = []
        except json.JSONDecodeError:
            log.warning(
                "project %s has invalid competitor_urls JSON, treating as empty",
                r[0],
            )
            competitor_urls = []

        # Trim to max_competitors.
        competitor_urls = competitor_urls[:max_competitors]

        projects.append(Project(
            project_id=r[0],
            project_name=r[1],
            client_url=r[2],
            competitor_urls=competitor_urls,
        ))

    return projects