"""Tests for schema.py — the single source of truth."""
from __future__ import annotations

from ai_scraper.schema import (
    CARRIED_FIELDS,
    ai_visibility_ddl,
    project_table_ddl,
    project_table_insert_columns,
    project_table_name,
    visibility_select_columns
)


def test_carried_fields_not_empty():
    assert len(CARRIED_FIELDS) > 0

def test_carried_field_names_unique():
    names = [f.name for f in CARRIED_FIELDS]
    assert len(names) == len(set(names)), "duplicate field names in CARRIED_FIELDS"

def test_ai_visibility_ddl_contains_every_carried_field():
    ddl = ai_visibility_ddl()
    for f in CARRIED_FIELDS:
        assert f.source_column in ddl, f"missing {f.source_column} in ai_visibility DDL"


def test_ai_visibility_ddl_has_project_id_and_created_at():
    ddl = ai_visibility_ddl()
    assert "project_id" in ddl
    assert "created_at" in ddl
    assert "internal_links" in ddl


def test_visibility_select_columns_order():
    cols = visibility_select_columns()
    assert cols[0] == "id"
    assert cols[-2:] == ["internal_links", "created_at"]
    # All CARRIED_FIELDS source columns appear between id and internal_links,
    # in the same order as CARRIED_FIELDS.
    expected_middle = [f.source_column for f in CARRIED_FIELDS]
    assert cols[1 : 1 + len(expected_middle)] == expected_middle

def test_project_table_name_sanitization():
    assert project_table_name("ai_visi_", "myproject") == "ai_visi_myproject"
    assert project_table_name("ai_visi_", "my-project") == "ai_visi_my_project"
    assert project_table_name("ai_visi_", "my.project.v2") == "ai_visi_my_project_v2"
    assert project_table_name("ai_visi_", "my project") == "ai_visi_my_project"
    # Non-alphanumeric non-underscore characters are stripped.
    assert project_table_name("ai_visi_", "proj!@#$id") == "ai_visi_projid"

def test_project_table_ddl_contains_expected_columns():
    ddl = project_table_ddl("app_ranking", "ai_visi_test", max_competitors=3)
    # Identity + client columns.
    assert "prompt_id" in ddl
    assert "prompt" in ddl
    assert "client_url" in ddl
    assert "client_rank" in ddl
    # Content and date.
    assert "content" in ddl
    assert "date" in ddl
    assert "created_at" in ddl
    # Competitor columns for max_competitors=3.
    for i in range(1, 4):
        assert f"competitor_url{i}" in ddl
        assert f"competitor_rank{i}" in ddl
    # Unique constraint.
    assert "UNIQUE KEY uq_prompt_date (prompt_id, date)" in ddl
    # Schema qualification.
    assert "app_ranking.ai_visi_test" in ddl


def test_project_table_ddl_scales_with_max_competitors():
    ddl_5 = project_table_ddl("s", "t", max_competitors=5)
    ddl_10 = project_table_ddl("s", "t", max_competitors=10)
    assert ddl_5.count("competitor_url") == 5
    assert ddl_10.count("competitor_url") == 10


def test_insert_columns_count_matches_ddl():
    """The number of INSERT columns must match the DDL structure the writer
    is targeting, otherwise the placeholder count in the INSERT statement
    will drift out of sync with the values list."""
    max_comps = 10
    insert_cols = project_table_insert_columns(max_comps)
    # Expected count:
    #   prompt_id, prompt, client_url, client_rank  = 4
    #   search_volume, category, intent, source     = 4
    #   N × (competitor_url_i, competitor_rank_i)   = 2N
    #   content                                     = 1
    expected = 4 + 4 + 2 * max_comps + 1
    assert len(insert_cols) == expected, (
        f"insert_columns={len(insert_cols)}, expected={expected}. "
        f"If you added a CARRIED_FIELD, update project_table_ddl AND the "
        f"ordered list in project_table_insert_columns."
    )


def test_insert_columns_no_date_or_created_at():
    """date and created_at are appended as literal CURDATE()/NOW() by the
    writer, not as placeholders, so they must not appear in the insert
    column list."""
    cols = project_table_insert_columns(5)
    assert "date" not in cols
    assert "created_at" not in cols


def test_adding_a_field_flows_through_everywhere():
    """Meta-test: sanity check that CARRIED_FIELDS is truly the single
    source of truth for ai_visibility_ddl and visibility_select_columns.

    (project_table_ddl and insert_columns are tested separately because
    their column order is more nuanced — Go schema preserves a specific
    order.)"""
    all_source_cols_in_visibility_ddl = all(
        f.source_column in ai_visibility_ddl() for f in CARRIED_FIELDS
    )
    assert all_source_cols_in_visibility_ddl

    all_source_cols_in_select = all(
        f.source_column in visibility_select_columns() for f in CARRIED_FIELDS
    )
    assert all_source_cols_in_select