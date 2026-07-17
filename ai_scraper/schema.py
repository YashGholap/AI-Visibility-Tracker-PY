"""Canonical schema definition — the single source of truth.

CARRIED_FIELDS is the ordered list of columns that flow from ai_visibility
into per-project tables. Every consumer (DDL generation, SELECT builders,
INSERT builders, row mappers) derives what it needs from this list.

Adding a column tomorrow: one line in CARRIED_FIELDS. The migration picks
it up, the scraper writes it, the splitter reads it, the per-project table
gets it, and the INSERT includes it — with zero other edits.
"""

from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class Field:
    """One column that flows from ai_visibility → per-project table.

    Attributes:
        name: canonical field name (used for Python attribute access).
        ddl_type: MySQL column type used when creating tables.
        source_column: column name in ai_visibility.
        target_column: column name in per-project table (usually same as
            source; kept explicit in case a rename is needed).
        nullable: whether the column allows NULL. Defaults to True since
            most carried fields can be missing from a scrape.
    """
    name: str
    ddl_type: str
    source_column: str
    target_column: str
    nullable: bool = True

    def ddl_fragment(self) -> str:
        """Render 'target_column ddl_type [NULL|NOT NULL]' for CREATE TABLE."""
        null = "NULL" if self.nullable else "NOT NULL"
        return f"{self.target_column} {self.ddl_type} {null}"
    
    # ----------------------------------------------------------------------------
# CARRIED_FIELDS — THE canonical list.
#
# Order matters: it defines column order in DDL and INSERT statements.
# Only append to this list; never reorder. If you need to rename a column,
# do it via an explicit migration, not by editing this list.
# ----------------------------------------------------------------------------

CARRIED_FIELDS: list[Field] = [
    Field("query",         "TEXT",          "query",         "prompt",         nullable=False),
    Field("category",      "VARCHAR(512)",  "category",      "category"),
    Field("intent",        "VARCHAR(512)",  "intent",        "intent"),
    Field("source",        "VARCHAR(255)",  "source",        "source",         nullable=False),
    Field("search_volume", "INT",           "search_volume", "search_volume"),
    Field("content",       "LONGTEXT",      "content",       "content"),
]

# ----------------------------------------------------------------------------
# ai_visibility table — the scraper's output table.
# ----------------------------------------------------------------------------
def ai_visibility_ddl() -> str:
    """CREATE TABLE for ai_visibility.

    Structure (project_id, then all CARRIED_FIELDS as source columns,
    then internal_links, then created_at). internal_links stored as JSON.
    Matches the current Go schema exactly so the two binaries can coexist.
    """
    # ai_visibility uses source_column names, not target_column names.
    cols = []
    for f in CARRIED_FIELDS:
        # In ai_visibility, "query" is stored as `query` (source), not `prompt`.
        null = "NULL" if f.nullable else "NOT NULL"
        cols.append(f"  {f.source_column} {f.ddl_type} {null}")


    cols_sql = ",\n".join(cols)
    return f"""
        CREATE TABLE IF NOT EXISTS ai_visibility (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id VARCHAR(255),
        {cols_sql},
            internal_links JSON,
            created_at DATE DEFAULT (CURDATE())
        )
        """.strip()

def visibility_select_columns() -> list[str]:
    """SELECT column list used when reading ai_visibility rows for the splitter.

    Order matches CARRIED_FIELDS + the extra columns the splitter needs
    (id, internal_links, created_at). Consumers rely on this order.
    """
    cols = ["id"]
    cols.extend(f.source_column for f in CARRIED_FIELDS)
    cols.extend(["internal_links", "created_at"])
    return cols

# ----------------------------------------------------------------------------
# Per-project tables — the splitter's output.
# ----------------------------------------------------------------------------
def project_table_name(prefix: str, project_id: str) -> str:
    """Sanitize project_id into a safe MySQL table name.

    Ports Go's TableName: replaces space/dash/dot with underscore, then
    strips anything that isn't [a-zA-Z0-9_]. Prepends prefix.
    """
    safe = project_id.replace(" ", "_").replace("-","_").replace(".","_")
    filtered = "".join(c for c in safe if c.isalnum() or c == "_")
    return f"{prefix}{filtered}"

def project_table_ddl(schema: str, table: str, max_competitors: int) -> str:
    """CREATE TABLE for a per-project ranking table.

    Structure (matches the current Go schema exactly):
        id, prompt_id, prompt, client_url, client_rank, search_volume,
        category, intent, source,
        competitor_url1, competitor_rank1, ...
        content,
        date, created_at,
        UNIQUE (prompt_id, date)
    """

    # Look up specific fields we treat individually here.
    _fields_by_name = {f.name: f for f in CARRIED_FIELDS}

    # Column order preserved from the current Go schema. Everything not in
    # this list falls through to "add remaining CARRIED_FIELDS at end".
    ordered_target_names = [
        "search_volume", "category", "intent", "source",
    ]

    # First block: identity + client columns (not in CARRIED_FIELDS).
    lines = [
        "  id            BIGINT NOT NULL AUTO_INCREMENT",
        "  prompt_id     BIGINT NOT NULL",
        f"  {_fields_by_name['query'].target_column} TEXT NOT NULL",
        "  client_url    TEXT NULL",
        "  client_rank   INT NULL",
    ]

    # Second block: the ordered CARRIED_FIELDS in the position the Go schema uses.
    for name in ordered_target_names:
        f = _fields_by_name[name]
        lines.append(f"     {f.ddl_fragment()}")

    # Third block: competitor columns.
    for i in range(1, max_competitors + 1):
        lines.append(f"  competitor_url{i} TEXT NULL")
        lines.append(f"  competitor_rank{i} INT NULL")

    # Fourth block: content (large column, kept at the end for row-size hygiene).
    lines.append(f"  {_fields_by_name['content'].ddl_fragment()}")

    # Fifth block: date/created_at + keys.
    lines.append("  date          DATE NOT NULL")
    lines.append("  created_at    DATETIME NOT NULL")
    lines.append("  PRIMARY KEY (id)")
    lines.append("  UNIQUE KEY uq_prompt_date (prompt_id, date)")

    body = ",\n".join(lines)
    return f"""
        CREATE TABLE IF NOT EXISTS {schema}.{table} (
        {body}
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """.strip()


def project_table_insert_columns(max_competitors: int) -> list[str]:
    """Ordered column list for INSERT INTO per-project table.

    Column order MUST match the value order produced by the splitter's
    row-builder. The final `date` and `created_at` columns are appended
    to the values as `CURDATE()` and `NOW()` literals by the writer, so
    they are NOT included in this list.
    """
    _fields_by_name = {f.name: f for f in CARRIED_FIELDS}

    cols = [
        "prompt_id",
        _fields_by_name["query"].target_column,   # "prompt"
        "client_url",
        "client_rank",
        _fields_by_name["search_volume"].target_column,
        _fields_by_name["category"].target_column,
        _fields_by_name["intent"].target_column,
        _fields_by_name["source"].target_column,
    ]
    for i in range(1, max_competitors + 1):
        cols.append(f"competitor_url{i}")
        cols.append(f"competitor_rank{i}")
    cols.append(_fields_by_name["content"].target_column)
    return cols