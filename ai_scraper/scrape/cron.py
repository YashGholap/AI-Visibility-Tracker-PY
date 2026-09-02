"""Cron scrape flow: read queries → scrape → write to ai_visibility.

Ports Go's runCron from cmd/server/main.go plus its per-platform dedupe.
"""
from __future__ import annotations

import logging
import os
import shutil

try:
    import resource  # POSIX only; diagnostics degrade gracefully without it
except ImportError:  # pragma: no cover - Windows dev boxes
    resource = None  # type: ignore[assignment]

from patchright.async_api import Error as PlaywrightError
from sqlalchemy import Engine
import asyncio

from ai_scraper.config import Config
from ai_scraper.models import ScrapeResult
from ai_scraper.scrape.browser.pool import open_pool
from ai_scraper.scrape.orchestrator import run_query
from ai_scraper.scrape.scrapers.registry import resolve_platforms
from ai_scraper.storage.migrations import ensure_ai_visibility
from ai_scraper.storage.visibility import (
    get_visibility_queries,
    insert_results,
    is_already_scraped,
)

log = logging.getLogger(__name__)

# Give up on the whole run after this many consecutive queries fail because
# no browser could be obtained. The pool already relaunches Chromium on
# death, so reaching this means the host itself can't run one.
_MAX_INFRA_FAILURES = 3

# How often to log the resource snapshot, in queries.
_RESOURCE_LOG_EVERY = 25


def _parse_platforms(spec: str) -> list[str] | None:
    """Parse comma-separated platforms config into a list, or None if empty."""
    spec = spec.strip()
    if not spec:
        return None
    return [p.strip() for p in spec.split(",") if p.strip()]


def _resource_snapshot() -> str:
    """Chromium resource usage, as a log-friendly string.

    On 2026-09-02 the browser died 4h30m into the run with no OOM kill and
    188 GB of RAM free, so memory was never the problem. The host's fd soft
    limit was 1024 (since raised in run.sh), which is the kind of ceiling a
    run of that length can hit silently. We log open fds against the limit,
    plus threads, RSS and /tmp, so the next long run shows which resource was
    actually climbing instead of leaving us to infer it afterwards.

    Best-effort and Linux-only (returns "" elsewhere). Summing RSS across the
    process tree double-counts shared pages; these are trend indicators, not
    exact figures. Uses /proc and shutil only, so no new dependency.
    """
    parts: list[str] = []

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        rss = fds = threads = procs = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm") as f:
                    if "chrome" not in f.read():
                        continue
                with open(f"/proc/{entry}/statm") as f:
                    rss += int(f.read().split()[1]) * page_size
                procs += 1
                try:
                    fds += len(os.listdir(f"/proc/{entry}/fd"))
                except OSError:
                    pass  # needs matching uid; skip rather than lie
                with open(f"/proc/{entry}/status") as f:
                    for line in f:
                        if line.startswith("Threads:"):
                            threads += int(line.split()[1])
                            break
            except (OSError, ValueError, IndexError):
                continue  # process exited between listdir and open
        # Chromium is a child of this process, so it inherits our fd limit.
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0] if resource else -1
        parts.append(
            f"chromium procs={procs} rss={rss / 1048576:.0f}MB "
            f"fds={fds}/{soft} threads={threads}"
        )
    except Exception:  # noqa: BLE001
        pass  # diagnostics must never take down the run

    try:
        parts.append(f"tmp_free={shutil.disk_usage('/tmp').free / 1048576:.0f}MB")
    except Exception:  # noqa: BLE001
        pass

    return "  ".join(parts)


async def run_cron(engine: Engine, config: Config) -> int:
    """Run the cron scrape flow.

    Returns count of rows inserted into ai_visibility.
    """
    ensure_ai_visibility(engine)

    project_filter = config.project_id.strip()
    platforms = _parse_platforms(config.platforms)

    try:
        resolved_platforms = resolve_platforms(platforms)
    except ValueError as e:
        log.error("platform validation failed: %s", e)
        raise

    log.info(
        "cron: starting  project_filter=%s  platforms=%s",
        project_filter or "all", resolved_platforms,
    )

    queries = get_visibility_queries(engine, project_id=project_filter, limit=config.query_limit)
    log.info("cron: loaded %d queries", len(queries))

    if not queries:
        return 0

    concurrency = max(1, config.query_concurrency)
    log.info("Cron: query concurrency=%d", concurrency)

    async with open_pool(
        headless=config.browser_headless,
        block_media=config.browser_block_media,
        recycle_after=config.browser_recycle_after_contexts,
    ) as pool:
        semaphore = asyncio.Semaphore(concurrency)
        total = len(queries)
        counter = {"started": 0, "done": 0, "inserted": 0, "infra_failures": 0}
        aborted = False

        async def _process(q) -> None:
            nonlocal aborted
            async with semaphore:
                if aborted:
                    return

                counter["started"] += 1
                idx = counter["started"]
                log.info(
                    "cron: [%d/%d] START project=%s query=%r",
                    idx, total, q.project_id, q.query,
                )

                if idx % _RESOURCE_LOG_EVERY == 0:
                    snapshot = _resource_snapshot()
                    if snapshot:
                        log.info("cron: [%d/%d] resources  %s", idx, total, snapshot)

                pending = [
                    p for p in resolved_platforms
                    if not is_already_scraped(engine, q.project_id, q.query, p)
                ]
                if not pending:
                    log.info(
                        "cron: [%d/%d] all platforms already scraped, skipping",
                        idx, total,
                    )
                    counter["done"] += 1
                    return
                try:
                    results = await run_query(
                        q.query,
                        pending,
                        pool,
                        per_platform_timeout_s=config.per_platform_timeout_seconds
                    )
                except PlaywrightError as e:
                    # The pool relaunches Chromium when it dies, so reaching
                    # here means even the relaunch failed — the host can't run
                    # a browser at all. Continuing would drain the remaining
                    # queue in seconds and fill the log with identical
                    # tracebacks, which is exactly the 2026-09-02 failure.
                    counter["infra_failures"] += 1
                    log.error(
                        "cron: [%d/%d] no browser available (%d consecutive): %s",
                        idx, total, counter["infra_failures"], e,
                    )
                    if counter["infra_failures"] >= _MAX_INFRA_FAILURES:
                        aborted = True
                        log.error(
                            "cron: ABORTING at query %d/%d — Chromium could not "
                            "be relaunched %d times running. %s  Re-run today to "
                            "resume; rows already scraped are skipped.",
                            idx, total, counter["infra_failures"],
                            _resource_snapshot() or "",
                        )
                    counter["done"] += 1
                    return
                except Exception: # noqa: BLE001
                    log.exception(
                        "cron: [%d/%d] run_query crashed  project=%s query=%r",
                        idx, total, q.project_id, q.query,
                    )
                    counter["done"] += 1
                    return

                counter["infra_failures"] = 0

                enriched: list[ScrapeResult] = []
                for r in results:
                    if not r.internal_links and not r.response_text:
                        log.warning(
                            "cron: [%d/%d] dropping empty result  source=%s query=%r",
                            idx, total, r.source, r.query,
                        )
                        continue
                    if not r.internal_links:
                        log.warning(
                            "cron: [%d/%d] no links extracted (keeping row)  "
                            "source=%s query=%r links=0",
                            idx, total, r.source, r.query,
                        )
                    r.project_id = q.project_id
                    r.category = q.category
                    r.intent = q.intent
                    r.search_volume = q.search_volume
                    enriched.append(r)
                
                if enriched:
                    try:
                        insert_results(engine, enriched)
                        counter["inserted"] += len(enriched)
                        log.info(
                            "cron: [%d/%d] DONE inserted=%d",
                            idx, total, len(enriched),
                        )
                    except Exception:   # noqa: BLE001
                        log.exception(
                            "cron: [%d/%d] insert failed  project=%s query=%r",
                            idx, total, q.project_id, q.query,
                        )
                counter["done"] += 1

        await asyncio.gather(*(_process(q) for q in queries))
        inserted = counter["inserted"]
        processed = counter["started"]

    if aborted:
        log.error(
            "cron: ABORTED  inserted=%d  processed=%d/%d  (%d queries never "
            "attempted — re-run to resume)",
            inserted, processed, total, total - processed,
        )
    else:
        log.info("cron: done  inserted=%d", inserted)
    return inserted