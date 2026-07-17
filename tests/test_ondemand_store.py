"""Tests for storage/ondemand.py."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine, text

from ai_scraper.models import ScrapeResult
from ai_scraper.storage.migrations import ensure_ondemand_table
from ai_scraper.storage.ondemand import (
    insert_ondemand_result,
    is_already_inserted_ondemand,
)


@pytest.fixture
def od_ready(clean_db: Engine) -> Engine:
    ensure_ondemand_table(clean_db)
    return clean_db


def _one_result() -> ScrapeResult:
    return ScrapeResult(
        query="best CRM",
        source="chatgpt",
        internal_links=["https://a.com", "https://b.com"],
        response_text="answer text",
    )


def test_insert_ondemand_writes_result_with_rankings(od_ready: Engine):
    insert_ondemand_result(
        od_ready,
        run_id="run_xyz",
        result=_one_result(),
        competitor_rankings={"competitor.com": 1, "other.com": 2},
    )
    with od_ready.connect() as conn:
        row = conn.execute(text("SELECT * FROM ai_visibility_ondemand")).mappings().one()

    assert row["run_id"] == "run_xyz"
    assert row["query"] == "best CRM"
    assert row["source"] == "chatgpt"
    assert row["content"] == "answer text"
    assert json.loads(row["internal_links"]) == ["https://a.com", "https://b.com"]
    assert json.loads(row["competitor_rankings"]) == {"competitor.com": 1, "other.com": 2}


def test_insert_ondemand_with_no_rankings_writes_null(od_ready: Engine):
    insert_ondemand_result(od_ready, "run_1", _one_result(), competitor_rankings=None)
    with od_ready.connect() as conn:
        row = conn.execute(text("SELECT competitor_rankings FROM ai_visibility_ondemand")).one()
    assert row[0] is None


def test_insert_ondemand_with_empty_rankings_writes_null(od_ready: Engine):
    """Empty dict must be treated as 'no competitors requested' — SQL NULL,
    matching the Go binary's behavior."""
    insert_ondemand_result(od_ready, "run_1", _one_result(), competitor_rankings={})
    with od_ready.connect() as conn:
        row = conn.execute(text("SELECT competitor_rankings FROM ai_visibility_ondemand")).one()
    assert row[0] is None


def test_is_already_inserted_ondemand_dedupe(od_ready: Engine):
    assert not is_already_inserted_ondemand(od_ready, "run_1", "q", "chatgpt")
    insert_ondemand_result(od_ready, "run_1", _one_result(), None)
    assert is_already_inserted_ondemand(od_ready, "run_1", "best CRM", "chatgpt")
    # Different run_id shouldn't collide.
    assert not is_already_inserted_ondemand(od_ready, "run_2", "best CRM", "chatgpt")