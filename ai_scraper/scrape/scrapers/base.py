"""Scraper protocol and error taxonomy.

Every platform scraper implements the `Scraper` protocol. Errors are
classified so the orchestrator (and future retry policy) can decide
whether to retry, back off, or fail hard.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from playwright.async_api import BrowserContext

from ai_scraper.models import ScrapeResult

class ScrapeError(Exception):
    """Base class for scraper failures."""

class TransientError(ScrapeError):
    """Timeout, network blip, transient CDP fail. Safe to retry."""


class BlockedError(ScrapeError):
    """CAPTCHA, 429, automation-detected. Retry with backoff/different fingerprint."""


class ExtractionError(ScrapeError):
    """Page loaded but expected structure wasn't found. Signals a DOM change."""
    
class NeedsFreshContextError(ScrapeError):
    """Signal that the current BrowserContext looks gated (Cloudflare, login).
    Orchestrator should retry with a fresh context. Different from
    TransientError which is any-context retry."""

@runtime_checkable
class Scraper(Protocol):
    name: str

    async def scrape(self, context: BrowserContext, query: str) -> ScrapeResult:
        ...
        