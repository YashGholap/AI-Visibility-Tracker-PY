"""Central platform registry. Adding a platform is one line here."""
from __future__ import annotations

from collections.abc import Callable

from ai_scraper.scrape.scrapers.base import Scraper
from ai_scraper.scrape.scrapers.google import GoogleAIScraper
from ai_scraper.scrape.scrapers.mock import MockScraper
from ai_scraper.scrape.scrapers.perplexity import PerplexityScraper


def _google_factory() -> Scraper:
    return GoogleAIScraper()


def _mock_factory() -> Scraper:
    return MockScraper()

def _perplexity_factory() -> Scraper:
    return PerplexityScraper()

_FACTORIES: dict[str, Callable[[], Scraper]] = {
    "google_ai": _google_factory,
    "perplexity": _perplexity_factory,
    "mock": _mock_factory,   # hidden — must be requested explicitly
}


def all_platforms() -> list[str]:
    """All non-hidden platforms. Mock is hidden so it doesn't show up in
    'all' expansion — must be requested explicitly."""
    return sorted(k for k in _FACTORIES if k != "mock")


def resolve_platforms(requested: list[str] | None) -> list[str]:
    """Empty/None → all (excluding hidden). Unknown → ValueError."""
    if not requested:
        return all_platforms()

    seen: set[str] = set()
    out: list[str] = []
    for name in requested:
        if name not in _FACTORIES:
            raise ValueError(
                f"unknown platform {name!r} (known: {sorted(_FACTORIES.keys())})"
            )
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def get_scraper(name: str) -> Scraper:
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(f"unknown platform {name!r}")
    return factory()