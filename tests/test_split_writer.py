"""Tests for split/writer.py — INSERT round-trip."""
from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from ai_scraper.models import CompetitorResult, RankedRow
from ai_scraper.split.writer import insert_ranked_rows
from ai_scraper.storage.migrations import ensure_project_table


@pytest.fixture
def pt_ready(clean_db: Engine) -> tuple[Engine, str]:
    schema = _current_schema(clean_db)
    qualified = ensure_project_table(
        clean_db,
        project_id="wtest",
        max_competitors=3,
        schema=schema,
        prefix="ai_visi_",
    )
    return clean_db, qualified


def _current_schema(engine: Engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT DATABASE()")).scalar()  # type: ignore[return-value]


def _row(prompt_id: int = 1, prompt: str = "q") -> RankedRow:
    return RankedRow(
        prompt_id=prompt_id,
        prompt=prompt,
        client_url="https://client.com",
        client_rank=1,
        search_volume=100,
        category="cat",
        intent="int",
        source="chatgpt",
        competitors=[
            CompetitorResult(url="https://c1.com", rank=2),
            CompetitorResult(url="https://c2.com", rank=5),
        ],
        content="answer",
    )


def test_insert_ranked_rows_writes_row(pt_ready: tuple[Engine, str]):
    engine, qualified = pt_ready
    n = insert_ranked_rows(engine, qualified, [_row()], max_competitors=3)
    assert n == 1

    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT * FROM {qualified}")).mappings().one()

    assert row["prompt_id"] == 1
    assert row["prompt"] == "q"
    assert row["client_url"] == "https://client.com"
    assert row["client_rank"] == 1
    assert row["category"] == "cat"
    assert row["intent"] == "int"
    assert row["source"] == "chatgpt"
    assert row["competitor_url1"] == "https://c1.com"
    assert row["competitor_rank1"] == 2
    assert row["competitor_url2"] == "https://c2.com"
    assert row["competitor_rank2"] == 5
    # Slot 3 was not populated in the RankedRow → should be NULL.
    assert row["competitor_url3"] is None
    assert row["competitor_rank3"] is None
    assert row["content"] == "answer"


def test_insert_ranked_rows_is_idempotent(pt_ready: tuple[Engine, str]):
    """INSERT IGNORE + UNIQUE (prompt_id, date) means re-running the same
    row on the same day is a no-op."""
    engine, qualified = pt_ready
    insert_ranked_rows(engine, qualified, [_row()], max_competitors=3)
    insert_ranked_rows(engine, qualified, [_row()], max_competitors=3)
    insert_ranked_rows(engine, qualified, [_row()], max_competitors=3)

    with engine.connect() as conn:
        n = conn.execute(text(f"SELECT COUNT(*) FROM {qualified}")).scalar()
    assert n == 1


def test_insert_ranked_rows_bulk(pt_ready: tuple[Engine, str]):
    engine, qualified = pt_ready
    rows = [_row(prompt_id=i, prompt=f"q{i}") for i in range(10)]
    n = insert_ranked_rows(engine, qualified, rows, max_competitors=3)
    assert n == 10

    with engine.connect() as conn:
        actual = conn.execute(text(f"SELECT COUNT(*) FROM {qualified}")).scalar()
    assert actual == 10


def test_insert_ranked_rows_empty_is_noop(pt_ready: tuple[Engine, str]):
    engine, qualified = pt_ready
    n = insert_ranked_rows(engine, qualified, [], max_competitors=3)
    assert n == 0


def test_insert_ranked_rows_null_client(pt_ready: tuple[Engine, str]):
    """When neither client nor competitors were found, all their columns
    should end up NULL."""
    engine, qualified = pt_ready
    row = RankedRow(
        prompt_id=99, prompt="q", client_url=None, client_rank=None,
        search_volume=None, category=None, intent=None, source="chatgpt",
        competitors=[],
        content=None,
    )
    insert_ranked_rows(engine, qualified, [row], max_competitors=3)
    with engine.connect() as conn:
        r = conn.execute(text(f"SELECT * FROM {qualified}")).mappings().one()
    assert r["client_url"] is None
    assert r["client_rank"] is None
    for i in range(1, 4):
        assert r[f"competitor_url{i}"] is None
        assert r[f"competitor_rank{i}"] is None