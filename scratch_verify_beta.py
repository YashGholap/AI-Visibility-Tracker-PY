from sqlalchemy import URL, create_engine, text
import json

e = create_engine(URL.create(
    "mysql+pymysql",
    username="root", password="Dev_DX_(123)",
    host="192.168.1.78", port=3308, database="app_ranking",
))

with e.connect() as conn:
    rows = conn.execute(text("""
        SELECT id, project_id, query, source,
               JSON_LENGTH(internal_links) AS link_count,
               LENGTH(content) AS content_len,
               created_at
        FROM ai_visibility
        WHERE project_id = 'E96B3E'
          AND source = 'google_ai'
          AND created_at = CURDATE()
        ORDER BY id DESC
        LIMIT 10
    """)).mappings().fetchall()

for r in rows:
    print(dict(r))
    