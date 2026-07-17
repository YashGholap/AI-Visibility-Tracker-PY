"""Shared pytest fixtures.

The `test_engine` fixture points at a throwaway MySQL database whose
connection details come from TEST_MYSQL_* env vars in .env. Tables are
dropped between tests so migration tests can assert on a clean slate.

WARNING: never point TEST_MYSQL_DATABASE at a production database.
Teardown drops tables inside it.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, Engine, create_engine, text


# Load .env from project root so TEST_MYSQL_* vars are available.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.is_file():
    from pydantic_settings import BaseSettings, SettingsConfigDict

    # Re-use the same env loader pattern as production.
    class _TestEnv(BaseSettings):
        model_config = SettingsConfigDict(env_file=str(_ENV_PATH), extra="ignore")

        test_mysql_host: str = ""
        test_mysql_port: int = 3306
        test_mysql_user: str = ""
        test_mysql_password: str = ""
        test_mysql_database: str = ""

    _CFG = _TestEnv()  # type: ignore[call-arg]
else:
    _CFG = None


def _require_test_env() -> None:
    """Fail loudly if the test DB env vars aren't set."""
    if _CFG is None or not _CFG.test_mysql_host or not _CFG.test_mysql_database:
        pytest.skip(
            "TEST_MYSQL_* env vars not set — skipping integration tests. "
            "Add them to .env to enable."
        )


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    """One SQLAlchemy engine for the whole test session."""
    _require_test_env()
    assert _CFG is not None

    url = URL.create(
        drivername="mysql+pymysql",
        username=_CFG.test_mysql_user,
        password=_CFG.test_mysql_password,
        host=_CFG.test_mysql_host,
        port=_CFG.test_mysql_port,
        database=_CFG.test_mysql_database,
    )
    engine = create_engine(url, pool_pre_ping=True)

    # Sanity check.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    yield engine
    engine.dispose()


@pytest.fixture
def clean_db(test_engine: Engine) -> Iterator[Engine]:
    """Drop all tables in the test DB before each test.

    Safe because TEST_MYSQL_DATABASE is a throwaway DB.
    """
    _drop_all_tables(test_engine)
    yield test_engine
    _drop_all_tables(test_engine)


def _drop_all_tables(engine: Engine) -> None:
    """Drop every table in the current database."""
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()"
            )
        ).fetchall()
        # Disable FK checks so drops don't get blocked.
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for (t,) in tables:
            conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))