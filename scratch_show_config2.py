from ai_scraper.config import load_config
c = load_config()
print(f"browser_headless = {c.browser_headless}")