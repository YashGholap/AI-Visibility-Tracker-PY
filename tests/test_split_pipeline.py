"""Tests for split/pipeline.py.

- build_ranked_rows is pure — tested without DB.
- split_one_project and run_split are integration-tested against a live DB.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine, text

from ai_scraper.config import Config
from ai_scraper.models import Project, ScrapeResult, VisibilityRow
from ai_scraper.split.pipeline import build_ranked_rows, run_split, split_one_project
from ai_scraper.storage.migrations import ensure_ai_visibility
from ai_scraper.storage.visibility import insert_results


# ----------------------------------------------------------------------------
# build_ranked_rows — pure function tests.
# ----------------------------------------------------------------------------
def test_build_ranked_rows_maps_visibility_row_to_ranked_row():
    project = Project(
        project_id="p",
        project_name="P",
        client_url="https://client.com",
        competitor_urls=["https://comp1.com", "https://comp2.com"],
    )
    vis = VisibilityRow(
        id=42,
        query="best CRM",
        category="software",
        intent="commercial",
        source="chatgpt",
        internal_links=[
            "https://noise.com",
            "https://client.com/x",
            "https://comp1.com/y",
        ],
        content="body",
        search_volume=500,
        created_at=date.today(),
    )
    out = build_ranked_rows(project, [vis], max_competitors=3)
    assert len(out) == 1
    r = out[0]

    # prompt_id preserved as vis.id (per your instruction).
    assert r.prompt_id == 42
    assert r.prompt == "best CRM"
    assert r.client_url == "https://client.com/x"
    assert r.client_rank == 2
    assert r.competitors[0].url == "https://comp1.com/y"
    assert r.competitors[0].rank == 3
    # comp2 was not in the links list.
    assert r.competitors[1].url is None
    assert r.competitors[1].rank is None
    # max_competitors=3, only 2 competitor URLs → 3rd slot is empty.
    assert r.competitors[2].url is None
    assert r.competitors[2].rank is None

    # Preserved Go bug: intent set to category value.
    assert r.intent == "software"
    assert r.category == "software"


def test_build_ranked_rows_no_matches_all_null_ranks():
    project = Project(
        project_id="p", project_name="P",
        client_url="https://client.com",
        competitor_urls=["https://comp1.com"],
    )
    vis = VisibilityRow(
        id=1, query="q", category=None, intent=None, source="chatgpt",
        internal_links=["https://unrelated.com"],
        content="", search_volume=0, created_at=date.today(),
    )
    out = build_ranked_rows(project, [vis], max_competitors=2)
    r = out[0]
    assert r.client_url is None
    assert r.client_rank is None
    assert all(c.url is None and c.rank is None for c in r.competitors)


def test_build_ranked_rows_empty_visibility():
    project = Project(
        project_id="p", project_name="P",
        client_url="https://client.com",
        competitor_urls=[],
    )
    out = build_ranked_rows(project, [], max_competitors=3)
    assert out == []


# ----------------------------------------------------------------------------
# split_one_project + run_split — DB integration.
# ----------------------------------------------------------------------------
@pytest.fixture
def _config(clean_db: Engine) -> Config:
    """Config using the current test DB as both schema and prefix source."""
    schema = _current_schema(clean_db)
    # Constructing Config directly, bypassing env — this is a unit test.
    from ai_scraper.config import Config as _Cfg
    return _Cfg(
        mysql_host="127.0.0.1",
        mysql_port=3309,
        mysql_user="root",
        mysql_password="admin123",
        mysql_database=schema,
        max_competitors=3,
        output_schema=schema,
        table_prefix="ai_visi_",
        process_all_projects=True,
    )


@pytest.fixture
def _projects_and_vis(clean_db: Engine) -> Engine:
    """Populate projects and ai_visibility with one project's worth of data."""
    ensure_ai_visibility(clean_db)
    with clean_db.begin() as conn:
        conn.execute(text("""
            CREATE TABLE projects (
                project_id VARCHAR(255),
                project_name VARCHAR(255),
                client_website_url TEXT,
                competitor_urls JSON
            )
        """))
        conn.execute(text("""
            INSERT INTO projects VALUES
                ('p1', 'Project One', 'https://client1.com',
                 '["https://comp1.com", "https://comp2.com"]')
        """))
    insert_results(clean_db, [
        ScrapeResult(
            query="q1", source="chatgpt",
            internal_links=["https://client1.com/x", "https://comp1.com/y"],
            response_text="body1", project_id="p1",
            category="cat", intent="int", search_volume=100,
        ),
    ])
    return clean_db


def _current_schema(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT DATABASE()")).scalar()  # type: ignore[return-value]


def test_split_one_project_full_pipeline(
    _projects_and_vis: Engine,
    _config: Config,
):
    project = Project(
        project_id="p1", project_name="Project One",
        client_url="https://client1.com",
        competitor_urls=["https://comp1.com", "https://comp2.com"],
    )
    summary = split_one_project(_projects_and_vis, project, _config)
    assert summary.status == "Success"
    assert summary.rows == 1
    assert summary.table_name.endswith(".ai_visi_p1")

    # Verify the row landed.
    with _projects_and_vis.connect() as conn:
        r = conn.execute(text(f"SELECT * FROM {summary.table_name}")).mappings().one()
    assert r["prompt"] == "q1"
    assert r["client_url"] == "https://client1.com/x"
    assert r["client_rank"] == 1
    assert r["competitor_url1"] == "https://comp1.com/y"
    assert r["competitor_rank1"] == 2


def test_split_one_project_no_data(_config: Config, clean_db: Engine):
    """A project with no ai_visibility rows today should yield 'No data',
    not an error."""
    ensure_ai_visibility(clean_db)
    project = Project(
        project_id="p_empty", project_name="Empty",
        client_url="https://x.com", competitor_urls=[],
    )
    summary = split_one_project(clean_db, project, _config)
    assert summary.status == "No data"
    assert summary.rows == 0


def test_run_split_processes_all_projects(
    _projects_and_vis: Engine,
    _config: Config,
):
    summaries = run_split(_projects_and_vis, _config)
    assert len(summaries) == 1
    assert summaries[0].status == "Success"