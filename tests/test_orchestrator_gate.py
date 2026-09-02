"""run_query gate handling — no browser, no network.

Locks in the behaviour added after the 2026-09-02 gating: on a gate signal
(NeedsFreshContextError) the platform gives up on the current query and the
run moves on, instead of burning identical fresh-context retries against an
IP-level gate. gate_retries>0 re-enables retries for cookie-based gates.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from ai_scraper.models import ScrapeResult
from ai_scraper.scrape import orchestrator
from ai_scraper.scrape.scrapers.base import NeedsFreshContextError


class _FakePool:
    """Stands in for BrowserPool; context() yields a throwaway sentinel."""

    @asynccontextmanager
    async def context(self, **_kwargs):
        yield object()


class _GatingScraper:
    name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    async def scrape(self, _ctx, _query) -> ScrapeResult:
        self.calls += 1
        raise NeedsFreshContextError("gate")


class _CountingOkScraper:
    name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    async def scrape(self, _ctx, query) -> ScrapeResult:
        self.calls += 1
        return ScrapeResult(query=query, source=self.name, response_text="ok")


def _run(scraper, monkeypatch, **kwargs):
    monkeypatch.setattr(orchestrator, "get_scraper", lambda _n: scraper)
    monkeypatch.setattr(orchestrator, "resolve_platforms", lambda _p: ["mock"])
    return asyncio.run(
        orchestrator.run_query("q", ["mock"], _FakePool(), **kwargs)
    )


def test_gate_does_not_retry_by_default(monkeypatch):
    scraper = _GatingScraper()
    results = _run(scraper, monkeypatch)
    assert results == []
    assert scraper.calls == 1  # one attempt, then move on — no retries


def test_gate_retries_when_configured(monkeypatch):
    scraper = _GatingScraper()
    results = _run(scraper, monkeypatch, gate_retries=2)
    assert results == []
    assert scraper.calls == 3  # initial attempt + 2 fresh-context retries


def test_success_returns_result_without_retry(monkeypatch):
    scraper = _CountingOkScraper()
    results = _run(scraper, monkeypatch)
    assert len(results) == 1
    assert results[0].response_text == "ok"
    assert scraper.calls == 1
