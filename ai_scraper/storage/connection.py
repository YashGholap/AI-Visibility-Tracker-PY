"""SQLAlchemy engine factory. One engine per DSN, cached."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine

from ai_scraper.config import Config

@lru_cache(maxsize=1)
def _make_engine(mysql_url: str) -> Engine:
    return create_engine(
        mysql_url,
        pool_size=10,
        max_overflow=5,
        pool_recycle=300,
        pool_pre_ping=True,
        echo=False
    )


def get_engine(config: Config) -> Engine:
    return _make_engine(config.mysql_url)