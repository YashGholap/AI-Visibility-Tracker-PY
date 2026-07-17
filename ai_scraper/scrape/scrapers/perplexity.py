"""Perplexity scraper.

URL access: perplexity.ai/search?q=<query> redirects to /search/<uuid>.
No login required for anonymous access. No CAPTCHA observed as of Nov 2025
from datacenter/residential IPs.

Content extraction: document.body.innerText — same approach as google.
Perplexity's answer container is the biggest text block on the page, and
body innerText matches it closely.

Wait strategy: DOM stability polling. Content streams over 10-15s; we poll
every 2s and consider content stable after 3 consecutive equal-size polls.

Link extraction: DOM scrape of anchor hrefs pointing to external domains.
Perplexity places citations inline in the rendered DOM.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext

from ai_scraper.models import ScrapeResult
from ai_scraper.scrape.scrapers.base import ExtractionError, TransientError

log = logging.getLogger(__name__)

_NAV_TIMEOUT_MS = 30_000
_STABILITY_MAX_POLLS = 20   # 20 * 2s = 40s max
_STABILITY_REQUIRED = 3     # 3 consecutive equal polls = stable

class PerplexityScraper:
    name: str = "perplexity"
    
    async def scrape(self, context: BrowserContext, query: str) -> ScrapeResult:
        result = ScrapeResult(query=query, source=self.name)
        page = await context.new_page()
        
        # ── Phase 1: navigate to search URL ────────────────────────────────
        search_url = f"https://www.perplexity.ai/search?q={quote_plus(query)}"
        log.info("perplexity: navigate → %s", search_url)
        try:
            await page.goto(search_url, timeout=_NAV_TIMEOUT_MS)
        except Exception as e: # noqa: BLE001
            raise TransientError(f"navigate failed: {e}") from e
        
        # Perplexity redirects /search?q=... to /search/<uuid>. Wait briefly
        # for redirect + first paint before starting stability polls.
        await asyncio.sleep(3)
        
        # Detect signals of failure early.
        current_url = page.url
        if "cloudflare" in current_url.lower() or "/challenge" in current_url:
            raise TransientError(f"perplexity antibot challenge: {current_url}")
        
        # ── Phase 2: wait for content stability ────────────────────────────
        prev_size = -1
        stable_count = 0
        for i in range(_STABILITY_MAX_POLLS):
            await asyncio.sleep(2.0)
            try:
                current_size = await page.evaluate(_DOM_SIZE_PROBE_JS)
            except Exception:  # noqa: BLE001
                current_size = 0
            log.debug("perplexity: poll %d size=%d", i + 1, current_size)
            if current_size == prev_size and current_size > 100:
                stable_count += 1
                if stable_count >= _STABILITY_REQUIRED:
                    log.info("perplexity: content stable at %d chars", current_size)
                    break
            else:
                stable_count = 0
            prev_size = current_size
            
        # ── Phase 3: extract content + links ───────────────────────────────
        try:
            content = await page.evaluate(_DOM_CONTENT_JS)
        except Exception as e:  # noqa: BLE001
            log.warning("perplexity: content extraction failed: %s", e)
            content = ""

        # ── Phase 3a: inline citations from the Answer tab ─────────────────
        # These show up in the visible answer text and preserve positional
        # information (which claim cites which source). We collect them first
        # so a later tab click doesn't lose the Answer tab's DOM state.
        try:
            inline_links = await page.evaluate(_DOM_LINKS_JS)
        except Exception as e:  # noqa: BLE001
            log.warning("perplexity: inline link extraction failed: %s", e)
            inline_links = []

        # ── Phase 3b: full source list from the Links tab ──────────────────
        # Click the "Links" tab to reveal the fuller list of all sources
        # Perplexity used to build the answer, not just the ones cited inline.
        tab_clicked = False
        try:
            tab_clicked = await page.evaluate(_CLICK_LINKS_TAB_JS)
            if tab_clicked:
                await asyncio.sleep(2.0)  # wait for tab content to render
        except Exception as e:  # noqa: BLE001
            log.warning("perplexity: could not click Links tab: %s", e)

        links_tab_links: list[str] = []
        if tab_clicked:
            try:
                links_tab_links = await page.evaluate(_DOM_LINKS_JS)
            except Exception as e:  # noqa: BLE001
                log.warning("perplexity: Links tab extraction failed: %s", e)

        # Union: inline first (preserves inline order), then any Links-tab
        # entries not already seen. Dedup happens in _clean_links.
        raw_links = inline_links + links_tab_links

        links = _clean_links(raw_links)
        log.info(
            "perplexity: content=%d chars links=%d",
            len(content), len(links),
        )

        await page.close()

        if not content and not links:
            raise ExtractionError("no content or links extracted from perplexity")

        result.response_text = content
        result.internal_links = links
        return result
    
# ─────────────────────────────────────────────────────────────────────────────
# JS extraction primitives
# ─────────────────────────────────────────────────────────────────────────────

_DOM_CONTENT_JS = """
(() => {
    let text = document.body.innerText || '';
    
    // Strip trailing cookie-policy blob if present (stable text).
    const cookieMarker = 'Cookie Policy';
    const cookieIdx = text.indexOf(cookieMarker);
    if (cookieIdx > 0) {
        text = text.slice(0, cookieIdx);
    }
    
    // Strip leading nav chrome. Perplexity's answer body starts with
    // the query being echoed back OR "Searching the web". Find whichever
    // comes first and take from there.
    const startMarkers = ['Searching the web', 'Answer\\n'];
    let startIdx = -1;
    for (const marker of startMarkers) {
        const idx = text.indexOf(marker);
        if (idx > 0 && (startIdx === -1 || idx < startIdx)) {
            startIdx = idx;
        }
    }
    if (startIdx > 0) {
        // Skip past the marker line
        const nlAfter = text.indexOf('\\n', startIdx);
        if (nlAfter > 0) text = text.slice(nlAfter + 1);
    }
    
    return text.trim();
})()
"""

_DOM_SIZE_PROBE_JS = """
(() => (document.body.innerText || '').length)()
"""

_DOM_LINKS_JS = """
(() => {
    const anchors = Array.from(document.querySelectorAll('a[href]'));
    return anchors
        .map(a => a.href)
        .filter(h => h && (h.startsWith('http://') || h.startsWith('https://')));
})()
"""

_CLICK_LINKS_TAB_JS = """
(() => {
    // Find the "Links" tab. Perplexity uses a tab button with visible text.
    const candidates = document.querySelectorAll(
        'button, a, [role="tab"], [role="button"]'
    );
    for (const el of candidates) {
        const text = (el.innerText || '').trim().toLowerCase();
        if (text === 'links' || text === 'sources') {
            el.click();
            return true;
        }
    }
    return false;
})()
"""

_SKIP_DOMAINS = (
    "perplexity.ai",
    "apple.com/apple-account",
    "accounts.google",
    "facebook.com",
    "twitter.com",
    "x.com/i/",
)


def _clean_links(raw: list[str]) -> list[str]:
    """Dedupe + strip perplexity's own domain + strip tracking params."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    tracking = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term",
        "utm_content", "utm_id", "gclid", "fbclid",
    }
    out: list[str] = []
    seen: set[str] = set()
    for link in raw:
        link = link.strip()
        if not link:
            continue
        # Skip perplexity's own paths and known-noise domains.
        if any(dom in link for dom in _SKIP_DOMAINS):
            continue
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https"):
            continue
        clean_q = [(k, v) for k, v in parse_qsl(parsed.query) if k not in tracking]
        parsed = parsed._replace(query=urlencode(clean_q))
        cleaned = urlunparse(parsed)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out