"""Inspect beta DB — list projects with query counts."""
import os
from sqlalchemy import URL, create_engine, text

# EDIT THESE to match your beta MySQL:
BETA = dict(
    host="192.168.1.78",       # your beta host
    port=3308,                 # your beta port
    user="root",
    password="Dev_DX_(123)",   # your beta password
    database="app_ranking",    # beta DB name (may be same as prod, may differ)
)

e = create_engine(URL.create(
    "mysql+pymysql",
    username=BETA["user"], password=BETA["password"],
    host=BETA["host"], port=BETA["port"], database=BETA["database"],
))

with e.connect() as conn:
    print("--- projects with query counts ---")
    rows = conn.execute(text("""
        SELECT p.project_id, p.project_name, COUNT(q.id) AS query_count
        FROM projects p
        LEFT JOIN ai_visibility_queries q ON q.project_id = p.project_id
        GROUP BY p.project_id, p.project_name
        HAVING query_count > 0
        ORDER BY query_count DESC
        LIMIT 20
    """)).fetchall()
    for r in rows:
        print(r)