"""Tests for storage/projects.py."""
from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from ai_scraper.storage.projects import load_projects


@pytest.fixture
def projects_table(clean_db: Engine) -> Engine:
    with clean_db.begin() as conn:
        conn.execute(text("""
            CREATE TABLE projects (
                id INT AUTO_INCREMENT PRIMARY KEY,
                project_id VARCHAR(255),
                project_name VARCHAR(255),
                client_website_url TEXT,
                competitor_urls JSON
            )
        """))
        conn.execute(text("""
            INSERT INTO projects
                (project_id, project_name, client_website_url, competitor_urls)
            VALUES
                ('proj_a', 'Project A', 'https://a.com',
                 '["https://comp1.com", "https://comp2.com", "https://comp3.com"]'),
                ('proj_b', 'Project B', 'https://b.com', '[]'),
                ('proj_c', 'Project C', 'https://c.com', NULL)
        """))
    return clean_db


def test_load_projects_all(projects_table: Engine):
    projects = load_projects(projects_table, max_competitors=10)
    assert len(projects) == 3
    by_id = {p.project_id: p for p in projects}
    assert by_id["proj_a"].client_url == "https://a.com"
    assert by_id["proj_a"].competitor_urls == [
        "https://comp1.com", "https://comp2.com", "https://comp3.com",
    ]
    assert by_id["proj_b"].competitor_urls == []
    assert by_id["proj_c"].competitor_urls == []


def test_load_projects_filtered(projects_table: Engine):
    projects = load_projects(
        projects_table,
        max_competitors=10,
        project_ids=["proj_a", "proj_c"],
    )
    assert {p.project_id for p in projects} == {"proj_a", "proj_c"}


def test_load_projects_trims_to_max_competitors(projects_table: Engine):
    projects = load_projects(projects_table, max_competitors=2)
    by_id = {p.project_id: p for p in projects}
    assert by_id["proj_a"].competitor_urls == [
        "https://comp1.com", "https://comp2.com",
    ]


def test_load_projects_handles_invalid_json(clean_db: Engine):
    """A malformed competitor_urls value should log a warning and treat as
    empty — never raise."""
    with clean_db.begin() as conn:
        conn.execute(text("""
            CREATE TABLE projects (
                project_id VARCHAR(255),
                project_name VARCHAR(255),
                client_website_url TEXT,
                competitor_urls TEXT  -- deliberately TEXT so we can insert garbage
            )
        """))
        conn.execute(text("""
            INSERT INTO projects VALUES
                ('bad', 'Bad', 'https://bad.com', 'not-json')
        """))
    projects = load_projects(clean_db, max_competitors=10)
    assert len(projects) == 1
    assert projects[0].competitor_urls == []