"""Splitter pipeline — orchestration of load → transform → write.

Every step is a pure(-ish) function with one responsibility:
    - `build_ranked_rows`  is pure: rows in, rows out, no I/O.
    - `split_one_project`  is I/O-heavy but idempotent (safe to re-run).
    - `run_split`          loops over projects, gathers summaries.

Mid-run failure recovery: if `run_split` errors on project X, you can
re-run just that project with `split_one_project` — the ensure_table
migration and INSERT IGNORE combine to make the retry safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine

from ai_scraper.config import Config
from ai_scraper.models import CompetitorResult, Project, RankedRow, VisibilityRow
from ai_scraper.split.ranking import extract_domain, find_url_rank
from ai_scraper.split.writer import insert_ranked_rows
from ai_scraper.storage.migrations import ensure_project_table
from ai_scraper.storage.projects import load_projects
from ai_scraper.storage.vis_rows import load_visibility_rows

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Summary — one entry per processed project (for the CLI output table).
# ----------------------------------------------------------------------------
@dataclass
class ProjectSummary:
    project_id: str
    project_name: str
    rows: int
    table_name: str
    status: str  # "Success" | "No data" | "Error: <message>"


# ----------------------------------------------------------------------------
# build_ranked_rows — pure. No I/O.
# ----------------------------------------------------------------------------
def build_ranked_rows(
    project: Project,
    vis_rows: list[VisibilityRow],
    max_competitors: int,
    remove_www: bool = False,
    case_sensitive: bool = False,
) -> list[RankedRow]:
    """Transform (project + its ai_visibility rows) into RankedRows ready for INSERT.

    Ports Go's buildRows from internal/processor/processor.go, including:
      - prompt_id = vis_row.id (preserved unchanged per your instruction)
      - intent = vis_row.category (preserved unchanged; the known Go bug)
      - substring-match domain semantics from ranking.find_url_rank

    Pure function: same inputs → same outputs, no I/O, no globals.
    """
    client_domain = extract_domain(
        project.client_url,
        remove_www=remove_www,
        case_sensitive=case_sensitive,
    )
    comp_domains = [
        extract_domain(u, remove_www=remove_www, case_sensitive=case_sensitive)
        for u in project.competitor_urls
    ]

    out: list[RankedRow] = []
    for v in vis_rows:
        client_url, client_rank = find_url_rank(
            v.internal_links, [client_domain],
            remove_www=remove_www, case_sensitive=case_sensitive,
        )

        competitors: list[CompetitorResult] = []
        for i in range(max_competitors):
            if i < len(comp_domains):
                url, rank = find_url_rank(
                    v.internal_links, [comp_domains[i]],
                    remove_www=remove_www, case_sensitive=case_sensitive,
                )
                competitors.append(CompetitorResult(url=url, rank=rank))
            else:
                competitors.append(CompetitorResult(url=None, rank=None))

        out.append(RankedRow(
            prompt_id=v.id,               # unchanged per your instruction
            prompt=v.query,
            client_url=client_url,
            client_rank=client_rank,
            search_volume=v.search_volume,
            category=v.category,
            intent=v.category,            # unchanged Go bug — kept as-is per your instruction
            source=v.source,
            competitors=competitors,
            content=v.content,
            created_at=datetime.now(),
        ))

    return out


# ----------------------------------------------------------------------------
# split_one_project — I/O, but idempotent.
# ----------------------------------------------------------------------------
def split_one_project(
    engine: Engine,
    project: Project,
    config: Config,
) -> ProjectSummary:
    """Full pipeline for one project: ensure table → load vis rows → build → insert."""
    log.info("processing project %s (%s)", project.project_name, project.project_id)

    # Load first — if there's no data, we can skip the ensure/insert.
    try:
        vis_rows = load_visibility_rows(engine, project.project_id)
    except Exception as e:
        log.exception("load_visibility_rows failed for %s", project.project_id)
        return ProjectSummary(
            project_id=project.project_id,
            project_name=project.project_name,
            rows=0,
            table_name="",
            status=f"Error: load failed: {e}",
        )

    if not vis_rows:
        log.info("no ai_visibility data today for %s", project.project_name)
        return ProjectSummary(
            project_id=project.project_id,
            project_name=project.project_name,
            rows=0,
            table_name="",
            status="No data",
        )

    # Ensure the target table exists (idempotent, converges to desired schema).
    try:
        qualified = ensure_project_table(
            engine=engine,
            project_id=project.project_id,
            max_competitors=config.max_competitors,
            schema=config.output_schema,
            prefix=config.table_prefix,
        )
    except Exception as e:
        log.exception("ensure_project_table failed for %s", project.project_id)
        return ProjectSummary(
            project_id=project.project_id,
            project_name=project.project_name,
            rows=0,
            table_name="",
            status=f"Error: ensure_table failed: {e}",
        )

    # Pure transform.
    ranked = build_ranked_rows(
        project=project,
        vis_rows=vis_rows,
        max_competitors=config.max_competitors,
    )

    # Write.
    try:
        n = insert_ranked_rows(engine, qualified, ranked, config.max_competitors)
    except Exception as e:
        log.exception("insert_ranked_rows failed for %s", project.project_id)
        return ProjectSummary(
            project_id=project.project_id,
            project_name=project.project_name,
            rows=0,
            table_name=qualified,
            status=f"Error: insert failed: {e}",
        )

    return ProjectSummary(
        project_id=project.project_id,
        project_name=project.project_name,
        rows=n,
        table_name=qualified,
        status="Success",
    )


# ----------------------------------------------------------------------------
# run_split — the top-level function.
# ----------------------------------------------------------------------------
def run_split(engine: Engine, config: Config) -> list[ProjectSummary]:
    """Run the splitter for all (or filtered) projects.

    Errors on one project do not abort the run — each project's outcome
    ends up in its own ProjectSummary.
    """
    # One ID, a comma-separated list, or empty (= all projects). load_projects
    # already handles the IN-clause filtering; None means no filter.
    project_ids = config.project_id_list() or None

    projects = load_projects(
        engine=engine,
        max_competitors=config.max_competitors,
        project_ids=project_ids,
    )
    log.info(
        "loaded %d projects (filter=%s)",
        len(projects), ",".join(project_ids) if project_ids else "all",
    )

    return [split_one_project(engine, p, config) for p in projects]

