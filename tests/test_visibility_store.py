"""Tests for storage/visibility.py."""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import Engine, text

from ai_scraper.models import ScrapeResult, VisibilityQuery
from ai_scraper.storage.migrations import ensure_ai_visibility
from ai_scraper.storage.visibility import (
    get_visibility_queries,
    insert_results,
    is_already_scraped,
)


@pytest.fixture
def av_ready(clean_db: Engine) -> Engine:
    """Clean DB + ai_visibility table ensured."""
    ensure_ai_visibility(clean_db)
    return clean_db


@pytest.fixture
def queries_table(clean_db: Engine) -> Engine:
    """Clean DB + a stub ai_visibility_queries table with some sample rows."""
    with clean_db.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ai_visibility_queries (
                id INT AUTO_INCREMENT PRIMARY KEY,
                project_id VARCHAR(255),
                query TEXT,
                category VARCHAR(255),
                intent VARCHAR(255),
                search_volume INT DEFAULT 0
            )
        """))
        conn.execute(text("""
            INSERT INTO ai_visibility_queries
                (project_id, query, category, intent, search_volume)
            VALUES
                ('proj_a', 'best CRM software', 'software', 'commercial', 5000),
                ('proj_a', 'top CRMs 2026',     'software', 'commercial', 3000),
                ('proj_b', 'cheapest hosting',  'hosting',  'commercial', 1000)
        """))
    return clean_db


# ----------------------------------------------------------------------------
# get_visibility_queries
# ----------------------------------------------------------------------------
def test_get_visibility_queries_all(queries_table: Engine):
    qs = get_visibility_queries(queries_table)
    assert len(qs) == 3
    assert {q.project_id for q in qs} == {"proj_a", "proj_b"}


def test_get_visibility_queries_filtered(queries_table: Engine):
    qs = get_visibility_queries(queries_table, project_id="proj_a")
    assert len(qs) == 2
    assert all(q.project_id == "proj_a" for q in qs)


def test_get_visibility_queries_populates_fields(queries_table: Engine):
    qs = get_visibility_queries(queries_table, project_id="proj_b")
    q = qs[0]
    assert q.query == "cheapest hosting"
    assert q.category == "hosting"
    assert q.intent == "commercial"
    assert q.search_volume == 1000


# ----------------------------------------------------------------------------
# insert_results
# ----------------------------------------------------------------------------
def test_insert_results_writes_all_carried_fields(av_ready: Engine):
    result = ScrapeResult(
        query="best CRM software",
        source="chatgpt",
        internal_links=["https://a.com/x", "https://b.com/y"],
        response_text="ChatGPT thinks the top CRMs are...",
        project_id="proj_a",
        category="software",
        intent="commercial",
        search_volume=5000,
    )
    insert_results(av_ready, [result])

    with av_ready.connect() as conn:
        row = conn.execute(text("SELECT * FROM ai_visibility")).mappings().one()

    assert row["project_id"] == "proj_a"
    assert row["query"] == "best CRM software"
    assert row["category"] == "software"
    assert row["intent"] == "commercial"
    assert row["source"] == "chatgpt"
    assert row["search_volume"] == 5000
    assert row["content"] == "ChatGPT thinks the top CRMs are..."
    assert row["created_at"] == date.today()
    assert json.loads(row["internal_links"]) == ["https://a.com/x", "https://b.com/y"]


def test_insert_results_empty_is_noop(av_ready: Engine):
    insert_results(av_ready, [])  # must not error
    with av_ready.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM ai_visibility")).scalar()
    assert n == 0


def test_insert_results_bulk(av_ready: Engine):
    results = [
        ScrapeResult(
            query=f"q{i}", source="google_ai", internal_links=[f"https://a{i}.com"],
            response_text="", project_id="proj",
        )
        for i in range(5)
    ]
    insert_results(av_ready, results)
    with av_ready.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM ai_visibility")).scalar()
    assert n == 5


# ----------------------------------------------------------------------------
# is_already_scraped
# ----------------------------------------------------------------------------
def test_is_already_scraped_finds_match(av_ready: Engine):
    result = ScrapeResult(
        query="q", source="chatgpt", internal_links=["https://a.com"],
        response_text="", project_id="proj",
    )
    insert_results(av_ready, [result])
    assert is_already_scraped(av_ready, "proj", "q", "chatgpt") is True


def test_is_already_scraped_false_when_no_row(av_ready: Engine):
    assert is_already_scraped(av_ready, "proj", "q", "chatgpt") is False


def test_is_already_scraped_ignores_empty_links(av_ready: Engine):
    """Rows with internal_links='[]' should NOT be treated as already scraped —
    they're failure rows and should be retried."""
    result = ScrapeResult(
        query="q", source="chatgpt", internal_links=[],  # empty
        response_text="", project_id="proj",
    )
    insert_results(av_ready, [result])
    assert is_already_scraped(av_ready, "proj", "q", "chatgpt") is False


def test_is_already_scraped_scopes_by_source(av_ready: Engine):
    result = ScrapeResult(
        query="q", source="chatgpt", internal_links=["https://a.com"],
        response_text="", project_id="proj",
    )
    insert_results(av_ready, [result])
    assert is_already_scraped(av_ready, "proj", "q", "chatgpt") is True
    assert is_already_scraped(av_ready, "proj", "q", "gemini") is False


# ----------------------------------------------------------------------------
# Model round-trip sanity check.
# ----------------------------------------------------------------------------
def test_visibility_query_model_defaults():
    q = VisibilityQuery(project_id="p", query="q")
    assert q.category is None
    assert q.intent is None
    assert q.search_volume == 0