import asyncio
from playwright.async_api import async_playwright


async def main() -> None:
    p = await async_playwright().start()
    b = await p.chromium.launch(headless=True)
    ctx = await b.new_context()
    page = await ctx.new_page()
    await page.goto("https://example.com")
    print(await page.title())
    await b.close()
    await p.stop()


asyncio.run(main())