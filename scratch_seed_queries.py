from sqlalchemy import URL, create_engine, text

e = create_engine(URL.create(
    "mysql+pymysql",
    username="root",
    password="admin123",
    host="127.0.0.1",
    port=3309,
    database="ai_scraper_test",
))

with e.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS ai_visibility_queries"))
    conn.execute(text("""
        CREATE TABLE ai_visibility_queries (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id VARCHAR(255),
            query TEXT,
            category VARCHAR(255),
            intent VARCHAR(255),
            search_volume INT DEFAULT 0
        )
    """))
    conn.execute(text("""
        INSERT INTO ai_visibility_queries (project_id, query, category, intent, search_volume)
        VALUES
          ('proj_a', 'best crm software', 'software', 'commercial', 5000),
          ('proj_a', 'top hosting providers', 'hosting', 'commercial', 3000)
    """))
    # Wipe today's ai_visibility so dedupe doesn't skip everything.
    conn.execute(text("DELETE FROM ai_visibility WHERE created_at = CURDATE()"))

print("seeded 2 queries, wiped today's ai_visibility rows")