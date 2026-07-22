"""
scheduler.py
------------
Standalone refresh script for the Oracle Readiness MCP server.

Intended for use when the MCP server runs over stdio (e.g. in Claude
Desktop/Code), where the server process only lives while a client is
connected and cannot run the background refresh loop itself. Point a
cron job, Windows Task Scheduler, or Docker healthcheck at this script
on the same READINESS_DATA_DIR as the server.

Usage:
    python scheduler.py                   # refresh all products once
    python scheduler.py erp hcm          # refresh specific products
    python scheduler.py --loop           # run in a loop (for Docker sidecar use)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from oracle_scraper import PRODUCTS, fetch_product
from db import ReadinessDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("oracle_readiness_scheduler")

DATA_DIR      = Path(os.environ.get("READINESS_DATA_DIR", "/data")).resolve()
DB_PATH       = DATA_DIR / "readiness.db"
REFRESH_HOURS = float(os.environ.get("READINESS_REFRESH_HOURS", "6"))


async def refresh_products(products: list[str]) -> list[dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = ReadinessDB(DB_PATH)
    results = []
    async with httpx.AsyncClient() as client:
        for p in products:
            try:
                features, used_url = await fetch_product(client, p)
                count = await db.upsert_features(features)
                await db.log_crawl(p, used_url, count)
                logger.info("Refreshed %s: %d entries from %s", p, count, used_url)
                results.append({"product": p, "ok": True, "count": count})
            except Exception as e:
                logger.warning("Refresh failed for %s: %s", p, e)
                await db.log_crawl(p, PRODUCTS.get(p, ""), 0, ok=False, error=str(e))
                results.append({"product": p, "ok": False, "error": str(e)})
            await asyncio.sleep(1.0)  # be polite to Oracle
    return results


async def main() -> None:
    args    = [a for a in sys.argv[1:] if not a.startswith("--")]
    looping = "--loop" in sys.argv

    products = args if args else list(PRODUCTS.keys())
    bad      = [p for p in products if p not in PRODUCTS]
    if bad:
        logger.error("Unknown product(s): %s. Valid: %s", bad, list(PRODUCTS.keys()))
        sys.exit(1)

    if looping:
        logger.info("Running in loop mode, refreshing every %.1f hours", REFRESH_HOURS)
        while True:
            await refresh_products(products)
            logger.info("Sleeping for %.1f hours…", REFRESH_HOURS)
            await asyncio.sleep(REFRESH_HOURS * 3600)
    else:
        results = await refresh_products(products)
        if any(not r["ok"] for r in results):
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
