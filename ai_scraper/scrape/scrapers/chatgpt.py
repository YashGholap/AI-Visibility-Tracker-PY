"""ChatGPT scraper.

Anonymous URL access via chatgpt.com/?hints=search&q=<query>.

The answer is delivered via SSE at /backend-anon/f/conversation. ChatGPT
strips `hints=search` from the URL client-side but still routes the
query into the anonymous conversation flow.

Content comes from the SSE stream — NOT from the DOM. Delta payloads
have a `"v"` field carrying markdown chunks; we concatenate them into
the full answer. Citations arrive as URL objects in various fields;
each is marked with `utm_source=chatgpt` making them easy to extract.

DOM is not used for extraction — it's mostly empty for anonymous
sessions. We only navigate + wait for SSE to complete.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Response

from ai_scraper.models import ScrapeResult
from ai_scraper.scrape.scrapers.base import ExtractionError, TransientError, NeedsFreshContextError

log = logging.getLogger(__name__)

# The SSE endpoint that streams the assistant response.
_SSE_URL_RE = re.compile(r"chatgpt\.com/backend-anon/f/conversation(?:$|\?|/[^/]*$)")

# Citation URLs in the SSE stream always carry utm_source=chatgpt|openai.
_CITATION_URL_RE = re.compile(
    r'https?://[^\s"\'\\<>]+?[?&]utm_source=(?:chatgpt|openai)[^\s"\'\\<>]*'
)

_SKIP_DOMAINS = (
    "chatgpt.com",
    "openai.com",
    "cdn.openai.com",
    "oaidalleapiprodscus.blob",
    "auth.openai.com",
    "auth0.openai.com",
)

_NAV_TIMEOUT_MS = 30_000
_SSE_TIMEOUT_S = 90.0
_SSE_QUIET_PERIOD_S = 8.0  # after last SSE chunk, wait this long to be sure it's done


class ChatGPTScraper:
    name: str = "chatgpt"

    async def scrape(self, context: BrowserContext, query: str) -> ScrapeResult:
        result = ScrapeResult(query=query, source=self.name)
        page = await context.new_page()

        # ── Phase 1: register SSE listener BEFORE navigation ───────────────
        sse_bodies: list[bytes] = []
        last_sse_at = asyncio.Event()

        async def _on_response(resp: Response) -> None:
            if not _SSE_URL_RE.search(resp.url):
                return
            try:
                body = await resp.body()
            except Exception as e:  # noqa: BLE001
                log.debug("chatgpt: failed to read SSE body: %s", e)
                return
            sse_bodies.append(body)
            last_sse_at.set()
            log.info(
                "chatgpt SSE: fragment %d url=%s size=%d",
                len(sse_bodies), resp.url, len(body),
            )

        page.on("response", _on_response)

        # ── Phase 2: navigate ──────────────────────────────────────────────
        search_url = f"https://chatgpt.com/?hints=search&q={quote_plus(query)}"
        log.info("chatgpt: navigate → %s", search_url)
        try:
            await page.goto(search_url, timeout=_NAV_TIMEOUT_MS)
        except Exception as e:  # noqa: BLE001
            raise TransientError(f"navigate failed: {e}") from e

        # ── Phase 3: wait for <main> — if it never appears, we're gated ────
        try:
            await page.wait_for_selector("main", timeout=30_000)
        except Exception as e:  # noqa: BLE001
            log.warning("chatgpt: <main> not visible (likely gated): %s", e)
            await page.close()
            raise NeedsFreshContextError(
                f"main element not visible within 30s: {e}"
            ) from e

        # ── Phase 4: wait for first SSE chunk ──────────────────────────────
        # If we made it past <main> but the SSE stream never starts, the
        # gate rejected the actual query. Fresh context is worth trying.
        try:
            await asyncio.wait_for(last_sse_at.wait(), timeout=_SSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            await page.close()
            raise NeedsFreshContextError(
                f"no SSE fragment within {_SSE_TIMEOUT_S}s (likely gated)"
            )

        # ── Phase 5: keep listening until SSE stream goes quiet ────────────
        # Poll for new fragments; when nothing new arrives for _SSE_QUIET_PERIOD_S,
        # consider the stream complete.
        last_count = len(sse_bodies)
        quiet_start = None
        max_wait = 60.0  # cap total post-first-fragment wait
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(1.0)
            waited += 1.0
            if len(sse_bodies) != last_count:
                last_count = len(sse_bodies)
                quiet_start = None
            else:
                if quiet_start is None:
                    quiet_start = waited
                elif waited - quiet_start >= _SSE_QUIET_PERIOD_S:
                    log.info(
                        "chatgpt SSE: stream quiet for %.0fs, extracting",
                        _SSE_QUIET_PERIOD_S,
                    )
                    break

        page.remove_listener("response", _on_response)

        # ── Phase 6: parse content + links from SSE ────────────────────────
        content = _extract_content_from_sse(sse_bodies)
        citation_urls = _extract_citations_from_sse(sse_bodies)
        links = _clean_links(citation_urls)

        log.info(
            "chatgpt: content=%d chars raw_citations=%d final_links=%d",
            len(content), len(citation_urls), len(links),
        )

        await page.close()

        if not content and not links:
            raise ExtractionError("no content or links extracted from chatgpt")

        result.response_text = content
        result.internal_links = links
        return result


# ─────────────────────────────────────────────────────────────────────────────
# SSE parsing — no browser, testable.
# ─────────────────────────────────────────────────────────────────────────────

def _extract_content_from_sse(sse_bodies: list[bytes]) -> str:
    """Concatenate all assistant text chunks from delta SSE payloads.

    ChatGPT sends deltas in several shapes:
      1. Top-level string `v`: chunk of assistant text.
      2. Top-level patch with `p='/message/content/parts/0'` and `o='append'`:
         also a chunk of assistant text.
      3. Batched patch list at `v`: a list of nested patch ops. We recurse
         into each and extract any 'append' op targeting the content parts.

    We collect only chunks whose target path is the assistant content parts
    (or top-level shape 1, which we treat as content by convention).
    """
    chunks: list[str] = []
    for body in sse_bodies:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            _collect_text(obj, chunks)
    full =  "".join(chunks).strip()
    # Strip ChatGPT's private-use citation markers (rendered as invisible in UI).
    full = re.sub(r"[\ue200-\ue2ff]", "", full)
    return full


def _collect_text(obj: dict, chunks: list[str]) -> None:
    """Recursively pull assistant-content text from a delta payload.

    Shape A: top-level string `v` on the delta itself → text chunk.
    Shape B: top-level `p='/message/content/parts/N'` + `o='append'` + string `v`
             → text append.
    Shape C: `v` is a list of nested patch objects → recurse into each.
    """
    p = obj.get("p", "")
    o = obj.get("o", "")
    v = obj.get("v")

    # Shape C: batched patches
    if isinstance(v, list):
        for inner in v:
            if isinstance(inner, dict):
                _collect_text(inner, chunks)
        return

    # Shape B: content-append patch
    if isinstance(p, str) and p.startswith("/message/content/parts/") and o == "append":
        if isinstance(v, str) and v:
            chunks.append(v)
        return

    # Shape A: top-level string chunk (no path, no op)
    if p == "" and o == "" and isinstance(v, str) and v:
        chunks.append(v)


def _extract_citations_from_sse(sse_bodies: list[bytes]) -> list[str]:
    """Pull all utm_source=chatgpt/openai marked URLs from raw SSE bodies.

    We don't try to parse the JSON structure of citation objects (too
    variable); we just find every URL matching the citation regex and
    dedupe.
    """
    seen: set[str] = set()
    out: list[str] = []
    for body in sse_bodies:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for m in _CITATION_URL_RE.finditer(text):
            url = _unescape_url(m.group(0))
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _unescape_url(url: str) -> str:
    """Decode common JSON escapes in an SSE-embedded URL."""
    return (
        url.replace("\\u0026", "&")
           .replace("\\u003d", "=")
           .replace("\\/", "/")
           .replace('\\"', "")
    )


def _clean_links(raw: list[str]) -> list[str]:
    """Dedupe + strip ChatGPT's own domains + strip tracking params.

    Domain-skip check runs against the PARSED hostname only, not the full
    URL, because ChatGPT's citation URLs carry `utm_source=chatgpt.com`
    in the query string — a substring-in-URL check would false-positive
    on every citation.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    tracking = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term",
        "utm_content", "utm_id", "gclid", "fbclid",
    }
    out: list[str] = []
    seen: set[str] = set()
    for link in raw:
        # Strip trailing markdown/citation punctuation.
        link = link.strip().rstrip('.,;:!?)]')
        if not link:
            continue
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https"):
            continue
        # Skip only when the HOSTNAME matches a skip domain.
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