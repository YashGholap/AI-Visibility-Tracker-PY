"""Integration tests for storage/migrations.py.

These run against a live MySQL throwaway database (test_engine fixture).
Every test starts from a clean DB and asserts that the ensure_*_table
functions converge the schema toward the desired state.
"""
from __future__ import annotations

from sqlalchemy import Engine, text

from ai_scraper.schema import CARRIED_FIELDS, project_table_name
from ai_scraper.storage.migrations import (
    ensure_ai_visibility,
    ensure_ondemand_table,
    ensure_project_table,
)


# ----------------------------------------------------------------------------
# Helpers.
# ----------------------------------------------------------------------------
def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ),
            {"t": table},
        ).fetchall()
    return {r[0] for r in rows}


def _table_exists(engine: Engine, table: str) -> bool:
    with engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ),
            {"t": table},
        ).scalar()
    return bool(n)


# ----------------------------------------------------------------------------
# ai_visibility.
# ----------------------------------------------------------------------------
def test_ensure_ai_visibility_creates_from_scratch(clean_db: Engine):
    assert not _table_exists(clean_db, "ai_visibility")
    ensure_ai_visibility(clean_db)
    assert _table_exists(clean_db, "ai_visibility")

    cols = _columns(clean_db, "ai_visibility")
    for f in CARRIED_FIELDS:
        assert f.source_column in cols
    assert "project_id" in cols
    assert "internal_links" in cols
    assert "created_at" in cols


def test_ensure_ai_visibility_is_idempotent(clean_db: Engine):
    ensure_ai_visibility(clean_db)
    ensure_ai_visibility(clean_db)  # second call must not error
    # Column count unchanged.
    cols_after = _columns(clean_db, "ai_visibility")
    assert "query" in cols_after
    assert "content" in cols_after


def test_ensure_ai_visibility_adds_missing_column(clean_db: Engine):
    """Create a stripped-down ai_visibility (as if from an old Go schema
    that predates the `content` column), then run ensure and confirm
    `content` gets added."""
    with clean_db.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ai_visibility (
                id INT AUTO_INCREMENT PRIMARY KEY,
                project_id VARCHAR(255),
                query TEXT,
                category VARCHAR(255),
                intent VARCHAR(255),
                source VARCHAR(50),
                internal_links JSON,
                search_volume INT DEFAULT 0,
                created_at DATE DEFAULT (CURDATE())
            )
        """))

    cols_before = _columns(clean_db, "ai_visibility")
    assert "content" not in cols_before

    ensure_ai_visibility(clean_db)

    cols_after = _columns(clean_db, "ai_visibility")
    assert "content" in cols_after


# ----------------------------------------------------------------------------
# ai_visibility_ondemand.
# ----------------------------------------------------------------------------
def test_ensure_ondemand_table_creates_with_all_columns(clean_db: Engine):
    ensure_ondemand_table(clean_db)
    cols = _columns(clean_db, "ai_visibility_ondemand")
    for expected in [
        "id", "run_id", "query", "source",
        "internal_links", "competitor_rankings", "content", "created_at",
    ]:
        assert expected in cols


def test_ensure_ondemand_adds_missing_columns(clean_db: Engine):
    """Create a stripped-down version (as if from before the competitor_rankings
    + content columns existed), then run ensure and confirm both get added."""
    with clean_db.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ai_visibility_ondemand (
                id INT AUTO_INCREMENT PRIMARY KEY,
                run_id VARCHAR(255),
                query TEXT,
                source VARCHAR(50),
                internal_links JSON,
                created_at DATE DEFAULT (CURDATE()),
                INDEX idx_run_id (run_id)
            )
        """))

    cols_before = _columns(clean_db, "ai_visibility_ondemand")
    assert "competitor_rankings" not in cols_before
    assert "content" not in cols_before

    ensure_ondemand_table(clean_db)

    cols_after = _columns(clean_db, "ai_visibility_ondemand")
    assert "competitor_rankings" in cols_after
    assert "content" in cols_after


# ----------------------------------------------------------------------------
# Per-project tables — the key test for solving your original problem.
# ----------------------------------------------------------------------------
def test_ensure_project_table_creates_with_all_carried_fields(clean_db: Engine):
    qualified = ensure_project_table(
        clean_db,
        project_id="myproject",
        max_competitors=3,
        schema=_current_schema(clean_db),
        prefix="ai_visi_",
    )
    assert qualified.endswith(".ai_visi_myproject")

    table = project_table_name("ai_visi_", "myproject")
    cols = _columns(clean_db, table)

    # Every CARRIED_FIELDS target column present.
    for f in CARRIED_FIELDS:
        assert f.target_column in cols, f"missing {f.target_column}"

    # Identity + client columns.
    for expected in ["id", "prompt_id", "client_url", "client_rank", "date", "created_at"]:
        assert expected in cols

    # Competitor columns.
    for i in range(1, 4):
        assert f"competitor_url{i}" in cols
        assert f"competitor_rank{i}" in cols


def test_ensure_project_table_adds_missing_content_column(clean_db: Engine):
    """The scenario from your original complaint: an existing per-project
    table lacks the `content` column. After ensure_project_table runs,
    `content` should be added — no manual ALTER needed."""
    schema = _current_schema(clean_db)
    table = project_table_name("ai_visi_", "legacyproject")

    # Simulate a legacy per-project table (pre-content column).
    with clean_db.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE {schema}.{table} (
                id BIGINT NOT NULL AUTO_INCREMENT,
                prompt_id BIGINT NOT NULL,
                prompt TEXT NOT NULL,
                client_url TEXT NULL,
                client_rank INT NULL,
                search_volume INT NULL,
                category VARCHAR(512) NULL,
                intent VARCHAR(512) NULL,
                source VARCHAR(255) NOT NULL,
                competitor_url1 TEXT NULL,
                competitor_rank1 INT NULL,
                date DATE NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                UNIQUE KEY uq_prompt_date (prompt_id, date)
            ) ENGINE=InnoDB
        """))

    cols_before = _columns(clean_db, table)
    assert "content" not in cols_before

    ensure_project_table(
        clean_db,
        project_id="legacyproject",
        max_competitors=1,
        schema=schema,
        prefix="ai_visi_",
    )

    cols_after = _columns(clean_db, table)
    assert "content" in cols_after


def test_ensure_project_table_adds_missing_competitor_slots(clean_db: Engine):
    """If a table was created with max_competitors=2 and now we want =5,
    ensure_project_table should add the missing competitor_url_{3,4,5}
    and competitor_rank_{3,4,5}."""
    schema = _current_schema(clean_db)
    table = project_table_name("ai_visi_", "growing")

    # First run: 2 competitor slots.
    ensure_project_table(
        clean_db,
        project_id="growing",
        max_competitors=2,
        schema=schema,
        prefix="ai_visi_",
    )
    cols_after_2 = _columns(clean_db, table)
    assert "competitor_url2" in cols_after_2
    assert "competitor_url3" not in cols_after_2

    # Second run: 5 competitor slots.
    ensure_project_table(
        clean_db,
        project_id="growing",
        max_competitors=5,
        schema=schema,
        prefix="ai_visi_",
    )
    cols_after_5 = _columns(clean_db, table)
    for i in range(1, 6):
        assert f"competitor_url{i}" in cols_after_5
        assert f"competitor_rank{i}" in cols_after_5


def test_ensure_project_table_is_idempotent(clean_db: Engine):
    schema = _current_schema(clean_db)
    ensure_project_table(clean_db, "idem", 3, schema, "ai_visi_")
    ensure_project_table(clean_db, "idem", 3, schema, "ai_visi_")
    ensure_project_table(clean_db, "idem", 3, schema, "ai_visi_")
    # No error is the assertion.


# ----------------------------------------------------------------------------
# Helper.
# ----------------------------------------------------------------------------
def _current_schema(engine: Engine) -> str:
    """Return the current database name (used as schema in per-project DDL)."""
    with engine.connect() as conn:
        return conn.execute(text("SELECT DATABASE()")).scalar()  # type: ignore[return-value]