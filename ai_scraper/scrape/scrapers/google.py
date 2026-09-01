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

        # ── Phase 1.5: homepage warm-up ────────────────────────────────────
        # Cold Chromium hitting a udm=50 URL directly is flagged aggressively.
        # Land on the homepage first so Google sees a normal session origin
        # (sets NID/consent cookies, establishes a referer) before the search.
        # Best-effort: a warm-up failure must not abort the scrape.
        try:
            await page.goto("https://www.google.com/", timeout=30_000)
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await asyncio.sleep(1.0)
            await page.mouse.wheel(0, 300)
            await asyncio.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            log.debug("google: homepage warm-up failed: %s", e)

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

        # ── Phase 4: wait for fragments AND for DOM content to stabilize ───
        try:
            await asyncio.wait_for(fragment_seen.wait(), timeout=_FRAGMENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("google: no async fragment within %.1fs", _FRAGMENT_TIMEOUT_S)

        # Google's AI Mode streams content over time. Poll the answer
        # container's size every 2s; stop when it stops growing for 3
        # consecutive polls (~6s of stability). Cap total wait at 30s.
        prev_size = -1
        stable_count = 0
        max_polls = 15
        for i in range(max_polls):
            await asyncio.sleep(2.0)
            try:
                current_size = await page.evaluate(_DOM_SIZE_PROBE_JS)
            except Exception:  # noqa: BLE001
                current_size = 0
            log.debug("google: poll %d/%d size=%d", i + 1, max_polls, current_size)
            if current_size == prev_size and current_size > 0:
                stable_count += 1
                if stable_count >= 3:
                    log.info("google: content stable at %d chars", current_size)
                    break
            else:
                stable_count = 0
            prev_size = current_size

        # Stop listening so the DOM fallback doesn't race with the handler.
        page.remove_listener("response", _on_response)

        # ── Phase 5: DOM extraction (content) + fragment parse (links) ─────
        # Content comes from the rendered DOM — the AI Mode answer container's
        # innerText. This captures everything the user sees, including bulleted
        # sections and section headings whose class names change between queries.
        #
        # Links still come from parsing intercepted fragments — the fragment
        # has full citation URLs while the DOM only shows short display names.
        content = ""
        try:
            content = await page.evaluate(_DOM_CONTENT_JS_V2)
            log.info("google DOM: content=%d chars", len(content))
        except Exception as e:  # noqa: BLE001
            log.warning("google: DOM extraction failed: %s", e)

        links: list[str] = []
        seen_links: set[str] = set()
        for _url, body in fragments:
            _content, frag_links = parse_google_fragment(body)
            for link in frag_links:
                if link not in seen_links:
                    seen_links.add(link)
                    links.append(link)
        log.info("google fragments: total unique links=%d", len(links))

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

    Google's AI Mode answer is delivered as a series of sibling <div> blocks,
    each matching our anchor regex. We collect ALL of them, dedupe substring
    overlaps, and merge in document order.

    <style>/<script> blocks are stripped up front so class-name references
    inside CSS/JS can't match the anchor.
    """
    raw = body.decode("utf-8", errors="replace")

    stripped = _STYLE_SCRIPT_RE.sub("", raw)

    candidates: list[tuple[int, str]] = []
    for m in _ADL_ANSWER_START_RE.finditer(stripped):
        block_html = _walk_div_block(stripped, m.start(), m.end())
        if not block_html:
            continue
        text = _HTML_TAG_RE.sub(" ", block_html)
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if len(text) < 20:  # drop empty/near-empty candidates
            continue
        candidates.append((m.start(), text))

    # Merge candidates by document order, dropping substrings.
    content = _merge_dedup(candidates)

    # Extract citation links from the full raw body (class-independent).
    links: list[str] = []
    seen_links: set[str] = set()
    for m in _HREF_RE.finditer(raw):
        u = m.group(1)
        if any(dom in u for dom in _SKIP_DOMAINS):
            continue
        if u in seen_links:
            continue
        seen_links.add(u)
        links.append(u)

    return content, links


def _merge_dedup(candidates: list[tuple[int, str]]) -> str:
    """Merge candidates in document order, dropping substring duplicates.

    Google's ADL can nest blocks, so a candidate's text may be contained in
    a sibling or ancestor. We keep candidates whose text adds something new.
    """
    if not candidates:
        return ""

    # Document order.
    by_pos = sorted(candidates, key=lambda t: t[0])

    accepted: list[str] = []
    for _pos, text in by_pos:
        # Skip if this text is already contained in any accepted candidate.
        if any(text in a for a in accepted):
            continue
        # Drop any accepted candidate that's now a substring of this one.
        accepted = [a for a in accepted if a not in text]
        accepted.append(text)

    return "\n\n".join(accepted)


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

_DOM_CONTENT_JS_V2 = """
(() => {
    // AI Mode answer container. Google renames classes frequently; we cast
    // a wide net including old and new anchor classes, then pick the biggest.
    const selectors = [
        // Current (Oct 2025)
        '.EIYajf', '.g7lqo', '.Wgphwb',
        // Older, keep for backwards compat
        '.n6owBd', '.cRH23c',
        // Structural
        '[data-container-id]',
    ];
    const seen = new Set();
    const candidates = [];
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            if (seen.has(el)) continue;
            seen.add(el);
            const text = el.innerText || '';
            if (text.length >= 50) {
                candidates.push({el, text, size: text.length});
            }
        }
    }
    if (!candidates.length) return '';
    // Biggest single container wins — this is the AI Mode answer wrapper.
    candidates.sort((a, b) => b.size - a.size);
    return candidates[0].text;
})()
"""

# _DOM_LINKS_JS_TEMPLATE = """
# (() => Array.from(document.querySelectorAll(%s))
#     .map(a => a.href)
#     .filter(h => h && h.startsWith("http") && !h.includes("google.com")))()
# """


_DOM_SIZE_PROBE_JS = """
(() => {
    const selectors = [
        '.EIYajf', '.g7lqo', '.Wgphwb', '.n6owBd', '.cRH23c',
        '[data-container-id]',
    ];
    let maxSize = 0;
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            const s = (el.innerText || '').length;
            if (s > maxSize) maxSize = s;
        }
    }
    return maxSize;
})()
"""