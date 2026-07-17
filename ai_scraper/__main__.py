"""Typer CLI. Subcommand dispatch only — no business logic here."""
from __future__ import annotations

import asyncio
import logging

import typer
from sqlalchemy import text

from ai_scraper.config import load_config
from ai_scraper.storage.connection import get_engine
import sys


# Windows: force ProactorEventLoop so Playwright can spawn Chromium subprocess.
# Without this, asyncio.run() may select SelectorEventLoop which lacks
# subprocess_exec on Windows, causing NotImplementedError deep in Playwright.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = typer.Typer(
    name="ai-scraper",
    help="AI visibility scraper: cron, on-demand, and splitter.",
    no_args_is_help=True,
)


def _init_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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
    """Cron scrape flow: read ai_visibility_queries, scrape, write results."""
    _init_logging()
    from ai_scraper.scrape.cron import run_cron

    config = load_config()
    engine = get_engine(config)

    # Windows: force ProactorEventLoop RIGHT BEFORE asyncio.run().
    # Setting at module load doesn't survive Typer's setup.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    inserted = asyncio.run(run_cron(engine, config))
    typer.echo(f"cron done  inserted={inserted}")


@app.command()
def ondemand() -> None:
    """On-demand scrape via stdin JSON, stdout JSON."""
    _init_logging()
    from ai_scraper.scrape.on_demand import run_ondemand

    config = load_config()
    engine = get_engine(config)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    exit_code = asyncio.run(run_ondemand(engine, config))
    raise typer.Exit(code=exit_code)


@app.command()
def split() -> None:
    """Run the splitter."""
    from ai_scraper.split.cli import format_summary_table
    from ai_scraper.split.pipeline import run_split

    config = load_config()
    engine = get_engine(config)
    summaries = run_split(engine, config)
    typer.echo(format_summary_table(summaries))
    if any(s.status.startswith("Error") for s in summaries):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()