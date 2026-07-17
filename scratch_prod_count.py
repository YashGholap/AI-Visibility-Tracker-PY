from sqlalchemy import URL, create_engine, text

e = create_engine(URL.create(
    "mysql+pymysql",
    username="root",
    password="dX@090877",
    host="192.168.1.78",
    port=3307,
    database="app_ranking",
))

with e.connect() as conn:
    rows = conn.execute(text("""
        SELECT source, COUNT(*) AS n
        FROM ai_visibility
        WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
          AND JSON_LENGTH(internal_links) > 0
        GROUP BY source
        ORDER BY n DESC
    """)).fetchall()

for row in rows:
    print(row)
