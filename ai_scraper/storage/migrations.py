"""Idempotent, converge-to-desired-state DDL for every table this system
touches.

The pattern for every ensure_*_table function is the same:

  1. CREATE TABLE IF NOT EXISTS ... using the current schema.
  2. Query INFORMATION_SCHEMA.COLUMNS to see what actually exists.
  3. ALTER TABLE ADD COLUMN for anything defined in schema.py that
     is missing from the live table.

This means adding a column to schema.py CARRIED_FIELDS makes that column
appear on every table the next time an ensure_*_table runs — no manual
migration, no forgotten table.

Non-goals:
  - Removing or renaming columns. Never done implicitly; those need
    explicit migration scripts.
  - Widening column types. If a column exists with the wrong type, we
    leave it alone (log a warning). Explicit ALTER via migration script.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine , text
from sqlalchemy.exc import OperationalError

from ai_scraper.schema import (
    CARRIED_FIELDS,
    Field,
    ai_visibility_ddl,
    project_table_ddl,
    project_table_name
)

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Helpers.
# ----------------------------------------------------------------------------
def _existing_columns(
    engine: Engine,
    schema: str | None,
    table: str,
) -> set[str]:
    """Return the set of column names currently on `schema.table`.

    If schema is None, uses the default database from the connection.
    """
    if schema:
        sql = text("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table
        """)
        params = {"schema": schema, "table": table}
    else:
        sql = text("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table
        """)
        params = {"table": table}

    with engine.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {r[0] for r in rows}

def _add_column(
    engine: Engine,
    qualified_table: str,
    column_name: str,
    ddl_type: str,
    nullable: bool = True,
) -> None:
    """Add a single column via ALTER TABLE.

    Idempotent: if the column already exists (MySQL errno 1060), treat as
    success. This can happen when two migrator processes race between the
    existing-columns SELECT and this ALTER.
    """
    null = "NULL" if nullable else "NOT NULL"
    sql = text(f"ALTER TABLE {qualified_table} ADD COLUMN {column_name} {ddl_type} {null}")
    try:
        with engine.begin() as conn:
            conn.execute(sql)
        log.info("added column %s to %s", column_name, qualified_table)
    except OperationalError as e:
        errno = None
        if hasattr(e, "orig") and hasattr(e.orig, "args") and e.orig.args:
            errno = e.orig.args[0]
        if errno == 1060:
            log.warning(
                "column %s.%s already exists (concurrent migration), ignoring",
                qualified_table, column_name,
            )
            return
        raise


def _run_ddl(engine: Engine, ddl: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(ddl))


# ----------------------------------------------------------------------------
# ai_visibility.
# ----------------------------------------------------------------------------
def ensure_ai_visibility(engine: Engine) -> None:
    """Create ai_visibility if it doesn't exist, then add any missing
    columns declared in CARRIED_FIELDS."""
    _run_ddl(engine, ai_visibility_ddl())

    existing = _existing_columns(engine, None, "ai_visibility")
    for f in CARRIED_FIELDS:
        if f.source_column not in existing:
            _add_column(
                engine,
                "ai_visibility",
                f.source_column,
                f.ddl_type,
                nullable=f.nullable,
            )


# ----------------------------------------------------------------------------
# ai_visibility_ondemand.
# ----------------------------------------------------------------------------
_ONDEMAND_DDL = """
CREATE TABLE IF NOT EXISTS ai_visibility_ondemand (
    id INT AUTO_INCREMENT PRIMARY KEY,
    run_id VARCHAR(255),
    query TEXT,
    source VARCHAR(50),
    internal_links JSON,
    competitor_rankings JSON,
    content LONGTEXT,
    created_at DATE DEFAULT (CURDATE()),
    INDEX idx_run_id (run_id)
)
""".strip()


def ensure_ondemand_table(engine: Engine) -> None:
    """Create ai_visibility_ondemand if it doesn't exist, then add any
    missing columns.

    Columns not in CARRIED_FIELDS but expected on this table:
      internal_links, competitor_rankings, content (the last two were
      added recently in the Go code via silent ALTERs — we handle them
      explicitly here).
    """
    _run_ddl(engine, _ONDEMAND_DDL)

    existing = _existing_columns(engine, None, "ai_visibility_ondemand")

    # Non-CARRIED columns this table needs.
    ondemand_extras = [
        ("internal_links", "JSON", True),
        ("competitor_rankings", "JSON", True),
        ("content", "LONGTEXT", True),
    ]
    for col, ddl_type, nullable in ondemand_extras:
        if col not in existing:
            _add_column(engine, "ai_visibility_ondemand", col, ddl_type, nullable=nullable)


# ----------------------------------------------------------------------------
# Per-project tables.
# ----------------------------------------------------------------------------
def ensure_project_table(
    engine: Engine,
    project_id: str,
    max_competitors: int,
    schema: str,
    prefix: str,
) -> str:
    """Ensure a per-project table exists with the current desired columns.

    Returns the fully qualified table name (schema.table) so callers can
    use it in subsequent INSERTs.

    Adds:
      - Any CARRIED_FIELDS column missing from the table (matched by
        target_column name).
      - Any competitor_url_i / competitor_rank_i column missing for i up
        to max_competitors.

    Does NOT drop or rename anything. If max_competitors was previously 5
    and is now 3, the old competitor_url_4/5 columns stay in place.
    """
    table = project_table_name(prefix, project_id)
    qualified = f"{schema}.{table}"

    # 1. CREATE IF NOT EXISTS with the current desired schema.
    _run_ddl(engine, project_table_ddl(schema, table, max_competitors))

    existing = _existing_columns(engine, schema, table)

    # 2. Add any CARRIED_FIELDS column that's missing (matched by target name).
    for f in CARRIED_FIELDS:
        if f.target_column not in existing:
            _add_column(engine, qualified, f.target_column, f.ddl_type, nullable=f.nullable)

    # 3. Add any competitor columns that are missing.
    for i in range(1, max_competitors + 1):
        for col in (f"competitor_url{i}", f"competitor_rank{i}"):
            if col not in existing:
                ddl_type = "TEXT" if col.startswith("competitor_url") else "INT"
                _add_column(engine, qualified, col, ddl_type, nullable=True)

    return qualified