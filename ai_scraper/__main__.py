"""Typer CLI. Subcommand dispatch only — no business logic here."""

from __future__ import annotations

import typer
from sqlalchemy import text

from ai_scraper.config import load_config
from ai_scraper.storage.connection import get_engine

app = typer.Typer(
    name="ai-scraper",
    help="AI visibility scraper: cron, on-demand, and splitter.",
    no_args_is_help=True
)

@app.command()
def ping() -> None:
    """Sanity check: load config, open a DB connection, run SELECT 1."""
    config = load_config()
    engine = get_engine(config)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT 1")).scalar()
    typer.echo(
        f"OK  db={row}  "
        f"project_filter={config.project_id or 'all'}  "
        f"platforms={config.platforms or 'all'}"
    )


@app.command()
def scrape() -> None:
    """[Phase 1 stub] Cron scrape flow"""
    typer.echo("scrape: not implemented yet", err=True)
    raise typer.Exit(code=1)

@app.command()
def ondemand() -> None:
    """[Phase 1 stub] On-demand scrape via stdin JSON."""
    typer.echo("ondemand: not implemented yet", err=True)
    raise typer.Exit(code=1)

@app.command()
def split() -> None:
    """Run the splitter: read today's ai_visibility rows, rank client and
    competitors per project, write into per-project tables.

    Honors PROJECT_ID env var (or config.project_id) as a project filter.
    """
    from ai_scraper.split.cli import format_summary_table
    from ai_scraper.split.pipeline import run_split

    config = load_config()
    engine = get_engine(config)
    summaries = run_split(engine, config)
    typer.echo(format_summary_table(summaries))

    # Non-zero exit if any project errored, so cron can alert on failure.
    if any(s.status.startswith("Error") for s in summaries):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()