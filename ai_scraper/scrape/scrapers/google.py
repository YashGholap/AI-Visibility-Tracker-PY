"""Google Search AI Mode (udm=50) scraper.

Ports Go's GoogleAIScraper. Two-phase extraction:

  1. Primary: intercept /async/folwr (or /async/folsrch fallback) via
     Playwright's page.on('response'). Google delivers the AI overview
     as ADL (Async Data Layer) HTML in these responses. Multiple
     fragments can fire; we pick the largest.

  2. Fallback: DOM extraction from known answer-container selectors.
     Kept as a safety net while the async-endpoint interception is
     being validated in prod.

Anti-bot notes:
  Cold headless Chromium hitting a udm=50 URL directly is flagged
  aggressively (429 → /sorry/index reCAPTCHA). We mitigate with a
  homepage warm-up navigation and a small scroll. Deeper anti-bot
  (stealth patches, residential proxies) is Phase 2 pre-cutover work.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Response

from ai_scraper.models import ScrapeResult
from ai_scraper.scrape.scrapers.base import ExtractionError, TransientError

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Regexes ported byte-for-byte from Go's google.go.
# ─────────────────────────────────────────────────────────────────────────────

# Match the async endpoint carrying the AI overview HTML/ADL payload.
_ASYNC_URL_RE = re.compile(r"google\.com/async/(folwr|folsrch)")

# Strip every HTML tag.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Collapse whitespace runs.
_WHITESPACE_RE = re.compile(r"\s+")

# Extract href="..." with http/https values.
_HREF_RE = re.compile(r'href="(https?://[^"]+)"')

# Locate opening tag of the AI answer container. Used as positional anchor;
# class names here are locators only. Link extraction never uses these.
_ADL_ANSWER_START_RE = re.compile(
    r'(?i)<div[^>]+class="[^"]*(?:n6owBd|LangJde|wDYxhc|IVvmDb|pWvJNd)[^"]*"'
)

# Strip <style>...</style> and <script>...</script> before anchor matching.
# Class names in CSS/JS files can otherwise collide with our answer-container
# selector and cause us to walk a style block instead of the AI overview.
_STYLE_SCRIPT_RE = re.compile(
    r"(?is)<(style|script)[^>]*>.*?</\1>"
)

# Div-nesting counters used by _extract_div_block.
_DIV_OPEN_RE = re.compile(r"(?i)<div[\s>]")
_DIV_CLOSE_RE = re.compile(r"(?i)</div>")

# Domains to skip when extracting citation links.
_SKIP_DOMAINS = (
    "google.com",
    "accounts.google",
    "gstatic.com",
    "youtube.com",
    "googleusercontent.com",
)

# How long to wait for at least one async fragment.
_FRAGMENT_TIMEOUT_S = 20.0


class GoogleAIScraper:
    name: str = "google_ai"

    async def scrape(self, context: BrowserContext, query: str) -> ScrapeResult:
        result = ScrapeResult(query=query, source=self.name)

        page = await context.new_page()

        # ── Phase 1: register response listener BEFORE navigation ──────────
        fragments: list[tuple[str, bytes]] = []
        fragment_seen = asyncio.Event()

        async def _on_response(resp: Response) -> None:
            if not _ASYNC_URL_RE.search(resp.url):
                return
            try:
                body = await resp.body()
            except Exception as e:  # noqa: BLE001
                log.debug("google: failed to read body from %s: %s", resp.url, e)
                return
            fragments.append((resp.url, body))
            fragment_seen.set()
            log.info(
                "google CDP: fragment %d url=%s size=%d",
                len(fragments), resp.url, len(body),
            )

        page.on("response", _on_response)

        # ── Phase 2: navigate to the AI Mode URL ───────────────────────────
        search_url = (
            f"https://www.google.com/search?q={quote_plus(query)}&udm=50"
        )
        log.info("google: navigate → %s", search_url)
        try:
            await page.goto(search_url, timeout=30_000)
        except Exception as e:  # noqa: BLE001
            raise TransientError(f"navigate failed: {e}") from e

        # Detect the "sorry" / captcha page early.
        current_url = page.url
        if "/sorry/" in current_url or "google.com/sorry" in current_url:
            raise TransientError(f"google served captcha page: {current_url}")

        # Small human-ish nudge — scrolling triggers lazy-loaded content and
        # signals "user is actually looking at the page" to Google's detection.
        try:
            await page.mouse.wheel(0, 400)
        except Exception:  # noqa: BLE001
            pass

        # ── Phase 3: wait for fragments ────────────────────────────────────
        try:
            await asyncio.wait_for(fragment_seen.wait(), timeout=_FRAGMENT_TIMEOUT_S)
            # After first fragment, keep listening briefly for additional ones
            # (Google can split the AI overview across multiple responses).
            await asyncio.sleep(3.0)
        except asyncio.TimeoutError:
            log.warning("google: no async fragment within %.1fs", _FRAGMENT_TIMEOUT_S)

        # Stop listening so the DOM fallback doesn't race with the handler.
        page.remove_listener("response", _on_response)

        # ── Phase 4: parse the best fragment ───────────────────────────────
        content = ""
        links: list[str] = []

        if fragments:
            largest = max(fragments, key=lambda f: len(f[1]))
            log.info(
                "google: parsing largest fragment url=%s size=%d",
                largest[0], len(largest[1]),
            )
            content, links = parse_google_fragment(largest[1])
            log.info(
                "google fragment: content=%d chars links=%d", len(content), len(links)
            )

        # ── Phase 5: DOM fallback ──────────────────────────────────────────
        if not content and not links:
            log.info("google: no fragment content, falling back to DOM")
            await asyncio.sleep(3.0)
            content, links = await _google_dom_fallback(page)
            log.info(
                "google DOM fallback: content=%d chars links=%d",
                len(content), len(links),
            )

        await page.close()

        if not content and not links:
            raise ExtractionError("no content or links extracted from google")

        result.response_text = content
        result.internal_links = _clean_links(links)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Pure functions — testable without a browser.
# ─────────────────────────────────────────────────────────────────────────────

def parse_google_fragment(body: bytes) -> tuple[str, list[str]]:
    """Parse an ADL response body → (content_text, links).

    Ports Go's parseGoogleFragment. Content via div-depth-tracked block
    extraction; links via href regex over the full body (class-independent).

    Candidate selection: enumerate ALL divs matching the anchor regex, extract
    each, then pick the candidate with the most citation links inside. This
    guards against class-name collisions with <style> blocks on the same page.
    """
    raw = body.decode("utf-8", errors="replace")

    # Remove <style>...</style> and <script>...</script> blocks up front so
    # our div-anchor regex can't match class-name references inside CSS/JS.
    stripped = _STYLE_SCRIPT_RE.sub("", raw)

    # Find every candidate div-block matching the anchor and score each by
    # its citation-link count. Pick the best-scoring candidate.
    best_content = ""
    best_score = -1
    for m in _ADL_ANSWER_START_RE.finditer(stripped):
        block_html = _walk_div_block(stripped, m.start(), m.end())
        if not block_html:
            continue
        score = len(_HREF_RE.findall(block_html))
        if score > best_score:
            best_score = score
            text = _HTML_TAG_RE.sub(" ", block_html)
            text = _WHITESPACE_RE.sub(" ", text).strip()
            if len(text) >= 80:
                best_content = text

    # Extract links from the FULL raw body (not stripped) — matches original
    # behaviour; link extraction is always class-independent.
    links: list[str] = []
    seen: set[str] = set()
    for m in _HREF_RE.finditer(raw):
        u = m.group(1)
        if any(dom in u for dom in _SKIP_DOMAINS):
            continue
        if u in seen:
            continue
        seen.add(u)
        links.append(u)

    return best_content, links


def _walk_div_block(raw: str, start: int, after_opening_tag: int) -> str:
    """Walk forward from `after_opening_tag`, tracking div nesting depth.
    Return the full HTML from `start` to the matching closing </div>.
    Returns empty string if the block doesn't close cleanly."""
    pos = after_opening_tag
    depth = 1

    while pos < len(raw) and depth > 0:
        open_m = _DIV_OPEN_RE.search(raw, pos)
        close_m = _DIV_CLOSE_RE.search(raw, pos)

        has_open = open_m is not None
        has_close = close_m is not None

        if not has_open and not has_close:
            break

        if has_open and (not has_close or open_m.start() < close_m.start()):
            depth += 1
            pos = open_m.end()
        else:
            assert close_m is not None
            depth -= 1
            pos = close_m.end()
            if depth == 0:
                return raw[start:pos]

    return ""

def _extract_div_block(raw: str, anchor: re.Pattern[str]) -> str:
    """Find the first match of anchor, then walk the div block."""
    m = anchor.search(raw)
    if m is None:
        return ""
    return _walk_div_block(raw, m.start(), m.end())


def _clean_links(links: list[str]) -> list[str]:
    """Minimal cleaner: dedupe + strip common tracking params.

    Matches ai_scraper's parser.CleanLinks intent from the Go code. We
    intentionally DO NOT remove youtube/google here — that filter already
    ran during parse_google_fragment via _SKIP_DOMAINS.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    tracking = {
        "utm_source", "utm_medium", "utm_campaign",
        "utm_term", "utm_content", "utm_id",
        "gclid", "fbclid",
    }

    out: list[str] = []
    seen: set[str] = set()
    for link in links:
        link = link.strip()
        if not link:
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


# ─────────────────────────────────────────────────────────────────────────────
# DOM fallback — kept only as a safety net.
# ─────────────────────────────────────────────────────────────────────────────

_DOM_CONTENT_JS = """
(() => {
    const sels = [
        ".n6owBd", ".awi2gc", ".jKhXsc", ".wDYxhc",
        ".pWvJNd", ".IVvmDb", ".LGOjhe", ".vxQmIe"
    ];
    for (const sel of sels) {
        const els = document.querySelectorAll(sel);
        if (!els.length) continue;
        const text = Array.from(els).map(e => e.innerText).join("\\n").trim();
        if (text.length > 30) return text;
    }
    return "";
})()
"""

_DOM_LINKS_JS_TEMPLATE = """
(() => Array.from(document.querySelectorAll(%s))
    .map(a => a.href)
    .filter(h => h && h.startsWith("http") && !h.includes("google.com")))()
"""


async def _google_dom_fallback(page) -> tuple[str, list[str]]:
    content = await page.evaluate(_DOM_CONTENT_JS)

    for sel in ("a.NDNGvf", ".EJw9bc a.NDNGvf", ".bTFeG a.NDNGvf"):
        js = _DOM_LINKS_JS_TEMPLATE % (repr(sel),)
        links = await page.evaluate(js)
        if links:
            log.info("google DOM fallback: %d links via %s", len(links), sel)
            return content, links

    return content, []