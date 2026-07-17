"""CLI-side rendering for split summaries.

Kept separate from pipeline.py so the pipeline stays pure business logic
and this file owns the presentation concern (formatting for humans).
"""
from __future__ import annotations

from ai_scraper.split.pipeline import ProjectSummary


def format_summary_table(summaries: list[ProjectSummary]) -> str:
    """Format the run summary as a table matching the Go binary's output.

    Ports the summary printer from Go's cmd/main.go.
    """
    if not summaries:
        return "\nSUMMARY\n" + "=" * 100 + "\n(no projects processed)\n"
    
    lines = []
    sep = "=" * 100
    lines.append("")
    lines.append(sep)
    lines.append("SUMMARY")
    lines.append(sep)
    lines.append(f"{'Project':<30} {'ID':<15} {'Rows':>6}  {'Table':<40}  Status")
    lines.append("-" * 100)
    for s in summaries:
        lines.append(
            f"{_trunc(s.project_name, 30):<30} "
            f"{_trunc(s.project_id, 15):<15} "
            f"{s.rows:>6}  "
            f"{_trunc(s.table_name, 40):<40}  "
            f"{s.status}"
        )
    lines.append("")
    return "\n".join(lines)

def _trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"