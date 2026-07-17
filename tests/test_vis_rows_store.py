"""Tests for storage/vis_rows.py."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine

from ai_scraper.models import ScrapeResult
from ai_scraper.storage.migrations import ensure_ai_visibility
from ai_scraper.storage.vis_rows import load_visibility_rows
from ai_scraper.storage.visibility import insert_results


@pytest.fixture
def av_ready(clean_db: Engine) -> Engine:
    ensure_ai_visibility(clean_db)
    return clean_db


def test_load_visibility_rows_returns_today_rows_for_project(av_ready: Engine):
    insert_results(av_ready, [
        ScrapeResult(
            query="q1", source="chatgpt",
            internal_links=["https://a.com"], response_text="text 1",
            project_id="proj_a", category="cat", intent="int", search_volume=100,
        ),
        ScrapeResult(
            query="q2", source="gemini",
            internal_links=["https://b.com"], response_text="text 2",
            project_id="proj_a",
        ),
        ScrapeResult(
            query="q3", source="chatgpt",
            internal_links=["https://c.com"], response_text="text 3",
            project_id="proj_b",
        ),
    ])

    rows = load_visibility_rows(av_ready, "proj_a")
    assert len(rows) == 2
    queries = {r.query for r in rows}
    assert queries == {"q1", "q2"}


def test_load_visibility_rows_populates_all_fields(av_ready: Engine):
    insert_results(av_ready, [
        ScrapeResult(
            query="best CRM", source="chatgpt",
            internal_links=["https://a.com", "https://b.com"],
            response_text="answer body",
            project_id="proj_a", category="software",
            intent="commercial", search_volume=5000,
        ),
    ])
    rows = load_visibility_rows(av_ready, "proj_a")
    row = rows[0]
    assert row.query == "best CRM"
    assert row.source == "chatgpt"
    assert row.category == "software"
    assert row.intent == "commercial"
    assert row.search_volume == 5000
    assert row.content == "answer body"
    assert row.internal_links == ["https://a.com", "https://b.com"]
    assert row.created_at == date.today()


def test_load_visibility_rows_excludes_other_projects(av_ready: Engine):
    insert_results(av_ready, [
        ScrapeResult(
            query="q_a", source="chatgpt", internal_links=["https://x.com"],
            response_text="", project_id="proj_a",
        ),
        ScrapeResult(
            query="q_b", source="chatgpt", internal_links=["https://y.com"],
            response_text="", project_id="proj_b",
        ),
    ])
    rows_a = load_visibility_rows(av_ready, "proj_a")
    assert len(rows_a) == 1
    assert rows_a[0].query == "q_a"


def test_load_visibility_rows_empty_when_no_matches(av_ready: Engine):
    rows = load_visibility_rows(av_ready, "nonexistent")
    assert rows == []