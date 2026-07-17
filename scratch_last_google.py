from sqlalchemy import URL, create_engine, text

e = create_engine(URL.create(
    "mysql+pymysql",
    username="root",
    password="Dev_DX_(123)",
    host="192.168.1.78",
    port=3308,
    database="app_ranking",
))

with e.connect() as conn:
    print("--- google_ai daily counts, last 30 days ---")
    rows = conn.execute(text("""
        SELECT DATE(created_at) AS d, COUNT(*) AS n
        FROM ai_visibility
        WHERE source = 'google_ai'
          AND created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
          AND JSON_LENGTH(internal_links) > 0
        GROUP BY DATE(created_at)
        ORDER BY d DESC
    """)).fetchall()
    for r in rows:
        print(r)

    print("\n--- for comparison, all platforms daily last 14 days ---")
    rows = conn.execute(text("""
        SELECT DATE(created_at) AS d, source, COUNT(*) AS n
        FROM ai_visibility
        WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
          AND JSON_LENGTH(internal_links) > 0
        GROUP BY DATE(created_at), source
        ORDER BY d DESC, source
    """)).fetchall()
    for r in rows:
        print(r)