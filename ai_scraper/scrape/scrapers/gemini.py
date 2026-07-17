"""Gemini scraper.

URL access: https://gemini.google.com/app — anonymous, but requires typing
the query into a contenteditable input rather than passing it as a URL
parameter. The `?prompt=` URL parameter is silently ignored.

Interaction flow:
  1. Set localStorage flag to skip image loading UI.
  2. Navigate to /app.
  3. Wait for the contenteditable input to become enabled.
  4. Fill query via JS (React contenteditable needs proper events).
  5. Submit with Enter key.
  6. Poll for response: not streaming AND content stable for a few seconds.
  7. Extract from .model-response-text (or fallback body innerText).

Wall time ~30-60s per query.
"""
from __future__ import annotations

import asyncio
import logging
import re

from playwright.async_api import BrowserContext

from ai_scraper.models import ScrapeResult
from ai_scraper.scrape.scrapers.base import (
    ExtractionError,
    NeedsFreshContextError,
    TransientError,
)

log = logging.getLogger(__name__)

_URL = "https://gemini.google.com/app"

_NAV_TIMEOUT_MS = 30_000
_INPUT_TIMEOUT_MS = 30_000
_RESPONSE_TIMEOUT_S = 90.0
_STABILITY_MAX_POLLS = 30    # 30 × 1s = 30s
_STABILITY_REQUIRED = 3      # 3 consecutive equal polls (~3s stable)


class GeminiScraper:
    name: str = "gemini"

    async def scrape(self, context: BrowserContext, query: str) -> ScrapeResult:
        result = ScrapeResult(query=query, source=self.name)
        page = await context.new_page()

        # ── Phase 1: pre-navigation setup ──────────────────────────────────
        # Bypass Gemini's image-load handshake. Setting this localStorage
        # flag before nav prevents the "Loading images..." UI which can
        # block the input from becoming interactive.
        try:
            await context.add_init_script("""
                try {
                    localStorage.setItem("gemini-image-load-status", "loaded");
                } catch (e) {}
            """)
        except Exception as e:  # noqa: BLE001
            log.debug("gemini: add_init_script failed: %s", e)

        # ── Phase 2: navigate ──────────────────────────────────────────────
        log.info("gemini: navigate → %s", _URL)
        try:
            await page.goto(_URL, timeout=_NAV_TIMEOUT_MS)
        except Exception as e:  # noqa: BLE001
            raise TransientError(f"navigate failed: {e}") from e

        # Detect if we got a login/consent gate — Gemini's app URL should
        # land us directly on the input UI.
        current_url = page.url
        if "accounts.google" in current_url or "signin" in current_url:
            await page.close()
            raise NeedsFreshContextError(
                f"gemini redirected to sign-in: {current_url}"
            )

        # ── Phase 3: wait for input to be enabled ──────────────────────────
        try:
            await page.wait_for_selector(
                "div.ql-editor",
                timeout=_INPUT_TIMEOUT_MS,
                state="visible",
            )
        except Exception as e:  # noqa: BLE001
            await page.close()
            raise NeedsFreshContextError(
                f"gemini input not visible (likely gated): {e}"
            ) from e

        # ── Phase 4: focus, type, submit ───────────────────────────────────
        # Gemini uses Quill.js contenteditable. Trusted Types policy blocks
        # innerHTML assignment, so we type the query via Playwright's key
        # dispatch — the same path a real user takes.
        try:
            await page.evaluate(_FOCUS_INPUT_JS)
        except Exception as e:  # noqa: BLE001
            await page.close()
            raise TransientError(f"failed to focus input: {e}") from e

        editor = page.locator("div.ql-editor")
        try:
            # Type the query character-by-character with a small delay so
            # Quill's internal state updates cleanly.
            typed_query = f"{query} with sources"
            await editor.type(typed_query, delay=20)
        except Exception as e:  # noqa: BLE001
            await page.close()
            raise TransientError(f"failed to type query: {e}") from e

        await asyncio.sleep(0.5)  # let Quill settle its state

        try:
            await editor.press("Enter")
        except Exception as e:  # noqa: BLE001
            await page.close()
            raise TransientError(f"failed to submit: {e}") from e

        log.info("gemini: query submitted, waiting for response")

        # ── Phase 5: wait for response container to appear ─────────────────
        try:
            await page.wait_for_selector(
                ".model-response-text, .response-content, message-content",
                timeout=_RESPONSE_TIMEOUT_S * 1000,
            )
        except Exception as e:  # noqa: BLE001
            await page.close()
            raise NeedsFreshContextError(
                f"gemini response container not visible: {e}"
            ) from e

        # ── Phase 6: DOM stability polling ─────────────────────────────────
        # Wait for streaming to complete. Content stops growing when done.
        prev_size = -1
        stable_count = 0
        for i in range(_STABILITY_MAX_POLLS):
            await asyncio.sleep(1.0)
            try:
                current_size = await page.evaluate(_DOM_SIZE_PROBE_JS)
            except Exception:  # noqa: BLE001
                current_size = 0
            log.debug("gemini: poll %d size=%d", i + 1, current_size)
            if current_size == prev_size and current_size > 100:
                stable_count += 1
                if stable_count >= _STABILITY_REQUIRED:
                    log.info("gemini: content stable at %d chars", current_size)
                    break
            else:
                stable_count = 0
            prev_size = current_size

        # ── Phase 7: extract content + links ───────────────────────────────
        content = ""
        try:
            content = await page.evaluate(_DOM_CONTENT_JS)
        except Exception as e:  # noqa: BLE001
            log.warning("gemini: content extraction failed: %s", e)

        raw_links: list[str] = []
        try:
            raw_links = await page.evaluate(_DOM_LINKS_JS)
        except Exception as e:  # noqa: BLE001
            log.warning("gemini: link extraction failed: %s", e)

        links = _clean_links(raw_links)
        log.info(
            "gemini: content=%d chars raw_links=%d final_links=%d",
            len(content), len(raw_links), len(links),
        )

        await page.close()

        if not content and not links:
            raise ExtractionError("no content or links extracted from gemini")

        result.response_text = content
        result.internal_links = links
        return result


# ─────────────────────────────────────────────────────────────────────────────
# JS + helpers
# ─────────────────────────────────────────────────────────────────────────────

_FOCUS_INPUT_JS = """
() => {
    const editor = document.querySelector("div.ql-editor");
    if (!editor) throw new Error("input not found");
    editor.focus();
}
"""

_DOM_CONTENT_JS = """
(() => {
    // Primary: model-response-text is Gemini's answer container.
    const responseEls = document.querySelectorAll(
        ".model-response-text, .response-content, message-content"
    );
    let biggest = "";
    for (const el of responseEls) {
        const t = el.innerText || "";
        if (t.length > biggest.length) biggest = t;
    }
    if (biggest.length >= 80) return biggest;
    // Fallback: everything visible on the page.
    return (document.body.innerText || "").trim();
})()
"""

_DOM_SIZE_PROBE_JS = """
(() => {
    const sels = [
        ".model-response-text",
        ".response-content",
        "message-content",
    ];
    let maxSize = 0;
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            const s = (el.innerText || "").length;
            if (s > maxSize) maxSize = s;
        }
    }
    if (maxSize === 0) maxSize = (document.body.innerText || "").length;
    return maxSize;
})()
"""

_DOM_LINKS_JS = """
(() => {
    // Collect anchor hrefs from within the response container.
    const containers = document.querySelectorAll(
        ".model-response-text, .response-content, message-content"
    );
    const links = new Set();
    for (const c of containers) {
        for (const a of c.querySelectorAll("a[href]")) {
            if (a.href && a.href.startsWith("http")) links.add(a.href);
        }
    }
    return Array.from(links);
})()
"""

_SKIP_DOMAINS = (
    "gemini.google.com",
    "google.com/search",
    "accounts.google",
    "support.google.com",
    "policies.google.com",
    "myactivity.google.com",
)


def _clean_links(raw: list[str]) -> list[str]:
    """Dedupe + strip Gemini's own domains + strip tracking params."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    tracking = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term",
        "utm_content", "utm_id", "gclid", "fbclid",
    }
    out: list[str] = []
    seen: set[str] = set()
    for link in raw:
        link = link.strip().rstrip('.,;:!?)]')
        if not link:
            continue
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https"):
            continue
        host = (parsed.hostname or "").lower()
        if any(host == dom or host.endswith("." + dom) for dom in _SKIP_DOMAINS):
            continue
        clean_q = [(k, v) for k, v in parse_qsl(parsed.query) if k not in tracking]
        parsed = parsed._replace(query=urlencode(clean_q))
        cleaned = urlunparse(parsed)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out