from sqlalchemy import URL, create_engine, text

e = create_engine(URL.create(
    "mysql+pymysql",
    username="root", password="Dev_DX_(123)",
    host="192.168.1.78", port=3308, database="app_ranking",
))

with e.connect() as conn:
    rows = conn.execute(text("""
        SELECT id, query, content
        FROM ai_visibility
        WHERE project_id = 'E96B3E'
          AND source = 'google_ai'
          AND created_at = CURDATE()
        ORDER BY id DESC
        LIMIT 1
    """)).mappings().fetchall()

for r in rows:
    print(f"id: {r['id']}")
    print(f"query: {r['query']}")
    print(f"content ({len(r['content'])} chars):")
    print("---")
    print(r['content'])
    print("---")