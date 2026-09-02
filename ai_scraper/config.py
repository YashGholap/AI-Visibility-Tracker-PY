"""Application configuration.

Single source of truth for all runtime configuration. Every module receives
a Config instance as a parameter — no module reads os.environ directly.

Reads discrete DB fields from env vars (MYSQL_HOST, MYSQL_PORT, ...) and
composes them into a SQLAlchemy URL via URL.create(), which handles special
characters in passwords correctly.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,  # set dynamically in load_config()
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- database (discrete fields; URL is composed in load_config) ---
    mysql_host: str = Field(...)
    mysql_port: int = Field(default=3306)
    mysql_user: str = Field(...)
    mysql_password: str = Field(...)
    mysql_database: str = Field(...)

    # Composed at load_config() time; not read from env.
    mysql_url: str = ""

    # --- scrape/split filtering ---
    project_id: str = Field(
        default="",
        description=(
            "Filter to specific project ID(s). Accepts one ID or a "
            "comma-separated list (e.g. 'E96B3E' or 'E96B3E,3632AE'). "
            "Empty = all projects. Applies to both scrape and split."
        ),
    )
    platforms: str = Field(
        default="",
        description="Comma-separated platform names to run; empty = all.",
    )

    query_limit: int = Field(
        default=0,
        description="Cap number of queries processed by cron. 0 = no limit.",
        ge=0,
    )

    gate_retries: int = Field(
        default=0,
        description=(
            "Fresh-context retries when a platform reports a gate. 0 (default) "
            "gives up on that platform for the current query and moves to the "
            "next one — an anonymous/IP gate won't clear with a fresh context "
            "on the same IP. Raise only on a residential IP where gates are "
            "cookie-based."
        ),
        ge=0,
        le=3,
    )

    query_concurrency: int =  Field(
        default=1,
        description=(
            "How many queries to scrape concurrently. Each query internally "
            "runs all requested platforms in parallel. Total concurrent "
            "browser contexts = query_concurrency * len(platforms). "
            "Default 1 (sequential). Recommended 3-5 for prod."
        ),
        ge=1,
        le=20,
    )

    # --- splitter ---
    max_competitors: int = Field(default=10, ge=1, le=50)
    output_schema: str = Field(default="app_ranking")
    table_prefix: str = Field(default="ai_visi_")
    process_all_projects: bool = Field(default=True)

    # --- browser ---
    browser_headless: bool = Field(default=False)
    browser_pool_size: int = Field(default=1, ge=1, le=8)
    per_platform_timeout_seconds: int = Field(default=180, ge=30, le=600)

    browser_block_media: bool = Field(
        default=False,
        description=(
            "Abort image/font/media requests in every browser context. Saves "
            "bandwidth, but blocking images is a known bot signal and Google "
            "already captchas this IP heavily, so it stays off by default. "
            "Enable only if bandwidth becomes the constraint."
        ),
    )
    browser_recycle_after_contexts: int = Field(
        default=150,
        description=(
            "Relaunch Chromium after this many leased contexts. Bounds how "
            "much state — memory, file descriptors, threads — one process can "
            "accumulate over an 8-hour run. 0 disables."
        ),
        ge=0,
    )


    def project_id_list(self) -> list[str]:
        """Parse project_id (one ID or comma-separated) into a list.

        Empty list means "all projects". Used by both the scrape and split
        flows so a single PROJECT_ID setting drives both.
        """
        return [p.strip() for p in self.project_id.split(",") if p.strip()]


def load_config() -> Config:
    """Load config, honoring the ENV_FILE env var like the Go binary does."""
    env_file = os.environ.get("ENV_FILE", "").strip()
    if env_file:
        env_path = Path(env_file).resolve()
        if not env_path.is_file():
            raise SystemExit(f"ENV_FILE points to non-existent file: {env_path}")
        Config.model_config["env_file"] = str(env_path)

    cfg = Config()  # type: ignore[call-arg]

    # Compose SQLAlchemy URL from discrete fields.
    # URL.create() handles escaping of special characters in passwords.
    url = URL.create(
        drivername="mysql+pymysql",
        username=cfg.mysql_user,
        password=cfg.mysql_password,
        host=cfg.mysql_host,
        port=cfg.mysql_port,
        database=cfg.mysql_database,
    )
    cfg.mysql_url = url.render_as_string(hide_password=False)

    return cfg