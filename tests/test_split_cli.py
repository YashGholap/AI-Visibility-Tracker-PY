"""Tests for split/cli.py — pure formatting logic."""
from __future__ import annotations

from ai_scraper.split.cli import format_summary_table
from ai_scraper.split.pipeline import ProjectSummary


def test_format_summary_empty():
    out = format_summary_table([])
    assert "SUMMARY" in out
    assert "no projects processed" in out


def test_format_summary_single_row():
    summaries = [
        ProjectSummary(
            project_id="p1", project_name="Project One",
            rows=5, table_name="app_ranking.ai_visi_p1",
            status="Success",
        ),
    ]
    out = format_summary_table(summaries)
    assert "SUMMARY" in out
    assert "Project One" in out
    assert "p1" in out
    assert "app_ranking.ai_visi_p1" in out
    assert "Success" in out
    assert " 5" in out  # rows count


def test_format_summary_truncates_long_fields():
    summaries = [
        ProjectSummary(
            project_id="a_very_long_project_id_over_fifteen",
            project_name="A very long project name that exceeds the thirty character width",
            rows=1,
            table_name="app_ranking.some_extremely_long_table_name_that_will_definitely_be_truncated",
            status="Success",
        ),
    ]
    out = format_summary_table(summaries)
    # Truncation marker is present.
    assert "…" in out


def test_format_summary_mixed_statuses():
    summaries = [
        ProjectSummary("p1", "P1", 5, "t1", "Success"),
        ProjectSummary("p2", "P2", 0, "", "No data"),
        ProjectSummary("p3", "P3", 0, "", "Error: insert failed: connection lost"),
    ]
    out = format_summary_table(summaries)
    assert "Success" in out
    assert "No data" in out
    assert "Error" in out