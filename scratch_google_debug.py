"""Diagnostic: what does the actual google.com/search page look like
in our headless Chromium? Screenshot + save HTML + list all XHRs.

Run: uv run python scratch_google_debug.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("scratch_out")
OUT.mkdir(exist_ok=True)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,   # ← so YOU can see the page
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = await ctx.new_page()

        # Log every response URL and status.
        responses: list[tuple[int, str]] = []

        def _on_resp(r):
            responses.append((r.status, r.url))

        page.on("response", _on_resp)

        url = "https://www.google.com/search?q=best+crm+software&udm=50"
        print(f"→ navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Give the page 15s to do whatever it wants to do.
        await asyncio.sleep(15)

        # Save what we saw.
        await page.screenshot(path=str(OUT / "google.png"), full_page=True)
        html = await page.content()
        (OUT / "google.html").write_text(html, encoding="utf-8")

        # Save the response log — highlight anything containing 'async' or 'udm'.
        with (OUT / "responses.log").open("w", encoding="utf-8") as f:
            for status, url in responses:
                marker = ""
                if "/async/" in url:
                    marker = "  ← ASYNC"
                elif "udm" in url:
                    marker = "  ← UDM"
                f.write(f"{status}  {url}{marker}\n")

        # Print the ASYNC/UDM hits to stdout for quick eyeballing.
        print("\n=== Async/UDM responses ===")
        for status, url in responses:
            if "/async/" in url or "udm" in url:
                print(f"  {status}  {url}")

        print(f"\n=== Total responses: {len(responses)} ===")
        print(f"Screenshot: {OUT/'google.png'}")
        print(f"HTML dump:  {OUT/'google.html'}")
        print(f"Full log:   {OUT/'responses.log'}")

        # Leave the browser window open for 20 more seconds so you can look.
        print("\nBrowser open for 20s so you can inspect...")
        await asyncio.sleep(20)

        await browser.close()


asyncio.run(main())