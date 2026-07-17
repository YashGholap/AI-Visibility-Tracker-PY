import os
os.environ.setdefault("ENV_FILE", ".env.beta")
from ai_scraper.config import load_config
c = load_config()
print(f"mysql_host    = {c.mysql_host}")
print(f"mysql_port    = {c.mysql_port}")
print(f"mysql_database= {c.mysql_database}")
print(f"project_id    = {c.project_id!r}")
print(f"query_limit   = {c.query_limit}")
