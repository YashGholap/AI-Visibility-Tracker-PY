import asyncio
from patchright.async_api import async_playwright


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Bot detection test page — shows red/green for each fingerprint vector.
        await page.goto("https://bot.sannysoft.com/")
        await asyncio.sleep(5)
        await page.screenshot(path="patchright_sannysoft.png", full_page=True)
        print("screenshot saved: patchright_sannysoft.png")

        # Then try Google.
        await page.goto("https://www.google.com/")
        await asyncio.sleep(3)
        await page.goto("https://www.google.com/search?q=best+crm+software&udm=50")
        await asyncio.sleep(5)
        print(f"final url: {page.url}")
        await page.screenshot(path="patchright_google.png", full_page=True)

        await browser.close()


asyncio.run(main())