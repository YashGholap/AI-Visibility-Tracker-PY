"""Reproduce the working script exactly, then add scraper features one by one
to find which one triggers Google's detection.

Run each variant separately by editing VARIANT at the top.
"""
import asyncio
from urllib.parse import quote_plus

from patchright.async_api import async_playwright

# Change this to test each variant in turn.
# 1 = bare (identical to the working script)
# 2 = bare + response listener
# 3 = bare + response listener + warm-up
# 4 = bare + response listener + warm-up + explicit domcontentloaded wait
VARIANT = 4


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # ─ VARIANT 2+: register response listener BEFORE nav ───────────────
        if VARIANT >= 2:
            fragments = []

            async def _on_response(resp):
                if "google.com/async/" in resp.url:
                    try:
                        body = await resp.body()
                        fragments.append((resp.url, len(body)))
                        print(f"  intercepted: {resp.url}  size={len(body)}")
                    except Exception as e:
                        print(f"  body error: {e}")

            page.on("response", _on_response)

        # ─ VARIANT 3+: warm-up nav ─────────────────────────────────────────
        if VARIANT >= 3:
            print("→ warm-up: google.com")
            await page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(1.5)

        # ─ Navigate to search ──────────────────────────────────────────────
        url = f"https://www.google.com/search?q={quote_plus('best crm software')}&udm=50"
        print(f"→ nav: {url}")

        # ─ VARIANT 4+: explicit wait_until ─────────────────────────────────
        if VARIANT >= 4:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        else:
            await page.goto(url)  # let Playwright pick default (load)

        await asyncio.sleep(5)
        print(f"final url: {page.url}")
        print(f"is sorry: {'/sorry/' in page.url}")

        await browser.close()


asyncio.run(main())