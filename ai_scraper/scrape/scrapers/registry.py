"""Central platform registry. Adding a platform is one line here."""
from __future__ import annotations

from playwright.async_api import BrowserContext

from ai_scraper.scrape.scrapers.base import Scraper
from ai_scraper.scrape.scrapers.google import GoogleAIScraper



def _google_factory() -> Scraper:
    return GoogleAIScraper()


# Adding chatgpt/gemini/perplexity in Phase 2 is one line per platform here.
_FACTORIES: dict[str, callable] = {
    "google_ai": _google_factory,
}

def all_platforms() -> list[str]:
    return sorted(_FACTORIES.keys())

def resolve_platforms(requested: list[str] | None) -> list[str]:
    """Empty/None → all. Unknown → ValueError (matches Go behaviour)."""
    if not requested:
        return all_platforms()

    seen: set[str] = set()
    out: list[str] = []
    for name in requested:
        if name not in _FACTORIES:
            raise ValueError(
                f"unknown platform {name!r} (known: {all_platforms()})"
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