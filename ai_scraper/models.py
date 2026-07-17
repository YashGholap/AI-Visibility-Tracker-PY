"""Pydantic row models — the shared data contracts.

These are the types that cross module boundaries: scraper → storage,
storage → splitter, splitter → writer, and (via the on-demand CLI)
FastAPI → scraper → FastAPI.

Nothing here is a DB row mapper — SQLAlchemy Core handles the DB side.
These are the "in-flight" shapes that Python code passes around.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

# ----------------------------------------------------------------------------
# Scraper output.
# ----------------------------------------------------------------------------
class ScrapeResult(BaseModel):
    """One scrape's output. Produced by a Scraper, consumed by the storage
    layer (cron flow → ai_visibility; on-demand flow → ai_visibility_ondemand).

    Fields set by the scraper itself:
        query, source, internal_links, response_text.

    Fields set by the cron flow (from the VisibilityQuery being scraped),
    unset for on-demand:
        project_id, category, intent, search_volume.
    """
    model_config = ConfigDict(extra="forbid")

    #set by scraper.
    query: str
    source: str
    internal_links: list[str] = Field(default_factory=list)
    response_txt: str = ""

    #set by cron flow
    project_id: str = ""
    category: str | None = None
    intent: str | None = None
    search_volume: int = 0


class VisibilityQuery(BaseModel):
    """One row from ai_visibility_queries — the queries the cron flow scrapes."""
    model_config = ConfigDict(extra="forbid")

    project_id: str 
    query: str
    category: str | None = None
    intent: str | None = None
    search_volume: int = 0


# ----------------------------------------------------------------------------
# Splitter input.
# ----------------------------------------------------------------------------
class VisibilityRow(BaseModel):
    """One row read out of ai_visibility for the splitter."""
    model_config = ConfigDict(extra="forbid")

    id: int
    query: str
    category: str | None = None
    intent: str | None = None
    source: str
    internal_links: list[str] = Field(default_factory=list)
    content: str | None = None
    search_volume: int | None = None
    created_at: date


class Project(BaseModel):
    """One row from the projects table."""
    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_name: str
    client_url: str
    competitor_urls: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------
# Splitter output.
# ----------------------------------------------------------------------------
class CompetitorResult(BaseModel):
    """One competitor's URL + 1-based rank inside a scrape's internal_links.

    Both fields None means the competitor was not found in this scrape's links.
    """
    model_config = ConfigDict(extra="forbid")
    url: str | None = None
    rank: int | None = None


class RankedRow(BaseModel):
    """One row about to be written to a per-project ranking table."""
    model_config = ConfigDict(extra="forbid")

    prompt_id: int
    prompt: str    
    client_url: str | None = None
    client_rank: int | None = None
    search_volume: int | None = None
    category: str | None = None
    intent: str | None = None
    source: str
    competitors: list[CompetitorResult] = Field(default_factory=list)
    content: str | None = None
    created_at: datetime | None = None


# ----------------------------------------------------------------------------
# On-demand JSON contract (stdin/stdout with FastAPI).
# ----------------------------------------------------------------------------
class OnDemandRequest(BaseModel):
    """The stdin JSON that FastAPI sends via subprocess.

    Fields match the Go binary's onDemandRequest exactly so FastAPI's
    request-builder doesn't have to change during the Go → Python swap.
    """
    model_config = ConfigDict(extra="forbid")

    run_id: str
    platforms: list[str] = Field(default_factory=list)
    queries: list[str]
    competitors: list[str] = Field(default_factory=list)


class OnDemandError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    source: str = ""
    error: str

class OnDemandStatus(BaseModel):
    """The stdout JSON returned to FastAPI. Matches the Go binary's
    onDemandStatus exactly."""
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str # "ok" | "partial" | "error"
    queries_processed: int = 0
    rows_inserted: int = 0
    errors: list[OnDemandError] = Field(default_factory=list)