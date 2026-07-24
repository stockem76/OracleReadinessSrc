"""
server.py
---------
Unified Oracle Readiness MCP Server.

Combines the best of:
  - ClaudeCode oracle_readiness_mcp: proven scraper, background refresh loop,
    generate_report, get_document_content, HTTP + stdio transport.
  - oracle-readiness-mcp (TypeScript): rich feature schema (impact, enablement,
    AI flags, Redwood, opt-in, auto-enabled), compare_releases, statistical
    summaries, XLSX dump ingestion, filtered feature tools.

Run modes:
  python server.py           → stdio (for Claude Desktop / local MCP clients)
  python server.py --http    → Streamable HTTP on READINESS_HTTP_PORT (default 8080)
                               Health endpoint: GET http://host:port/health

Environment variables:
  READINESS_DATA_DIR          Directory for readiness.db and reports/ (default: /data)
  READINESS_REFRESH_HOURS     Hours between automatic background refresh (default: 6)
  READINESS_AUTOSTART_REFRESH "0" to disable the background refresh loop (default: "1")
  READINESS_HTTP_HOST         Bind host for HTTP mode (default: 0.0.0.0)
  READINESS_HTTP_PORT         Bind port for HTTP mode (default: 8080)
  READINESS_TOKEN             Optional Bearer token for HTTP mode auth
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import ica as _ica

from oracle_scraper import (
    PRODUCTS,
    PRODUCT_LABELS,
    READINESS_APP_URL,
    Feature,
    fetch_product,
    fetch_document_content,
    fetch_feature_details_for_module,
    parse_features_from_xlsx_dump,
)
from db import ReadinessDB
from settings import Settings
from auth import AuthDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("oracle_readiness_mcp")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

DATA_DIR      = Path(os.environ.get("READINESS_DATA_DIR",      "/data")).resolve()
DB_PATH       = DATA_DIR / "readiness.db"
REPORTS_DIR   = DATA_DIR / "reports"
REFRESH_HOURS = float(os.environ.get("READINESS_REFRESH_HOURS", "6"))
AUTOSTART     = os.environ.get("READINESS_AUTOSTART_REFRESH",  "1") != "0"
HTTP_HOST     = os.environ.get("READINESS_HTTP_HOST",           "0.0.0.0")
HTTP_PORT     = int(os.environ.get("READINESS_HTTP_PORT",       "8080"))
AUTH_TOKEN    = os.environ.get("READINESS_TOKEN",               "")

PRODUCT_NAMES = tuple(PRODUCTS.keys())
DEFAULT_PILLARS = ("erp", "scm", "hcm", "service")
MAX_INLINE_CONTENT = 15

# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db             = ReadinessDB(DB_PATH)
        self.settings       = Settings(DATA_DIR)
        self.auth           = AuthDB(DB_PATH)
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_lock  = asyncio.Lock()

    async def start_background_refresh(self) -> None:
        if self._refresh_task is not None:
            return

        async def _loop():
            while True:
                try:
                    async with self._refresh_lock:
                        results = await self._do_refresh(None)
                    logger.info("Background refresh complete: %s", results)
                except Exception:
                    logger.exception("Background refresh loop error")
                # Re-read refresh_hours from settings each cycle so UI changes are honoured
                sleep_secs = state.settings.refresh_hours * 3600
                logger.info("Next refresh in %.1f hours", state.settings.refresh_hours)
                await asyncio.sleep(sleep_secs)

        self._refresh_task = asyncio.create_task(_loop())

    async def _do_refresh(self, products: Optional[list[str]]) -> list[dict]:
        targets = products or self.settings.active_pillars or list(PRODUCTS.keys())
        results = []
        async with httpx.AsyncClient() as client:
            for p in targets:
                try:
                    features, used_url = await fetch_product(client, p)
                    count = await self.db.upsert_features(features)
                    await self.db.log_crawl(p, used_url, count)
                    results.append({"product": p, "ok": True, "total_entries": count, "source_url": used_url})
                except Exception as e:
                    logger.warning("Refresh failed for %s: %s", p, e)
                    await self.db.log_crawl(p, PRODUCTS[p], 0, ok=False, error=str(e))
                    results.append({"product": p, "ok": False, "error": str(e)})
                await asyncio.sleep(1.0)  # be polite to Oracle

            # Deep-scrape feature detail pages (steps to enable, tips, etc.)
            # Only scrape releases that are in the target list (settings) or all if unset
            target_releases = (
                [r.upper() for r in self.settings.target_releases]
                if self.settings.target_releases else None
            )
            for p in [t for t in targets if t != "news"]:
                try:
                    pages, feats = await self._deep_scrape_product(client, p, target_releases)
                    logger.info("Deep-scrape %s: %d pages, %d feature details", p, pages, feats)
                except Exception as e:
                    logger.warning("Deep-scrape failed for %s: %s", p, e)
                await asyncio.sleep(0.5)

        # Auto-push to GitHub if configured
        if self.settings.github_auto_push:
            try:
                await _github_push(
                    token=self.settings.github_token,
                    repo=self.settings.github_repo,
                    branch=self.settings.github_branch,
                    file_path=self.settings.github_file_path,
                    pillars=self.settings.active_pillars,
                )
                logger.info("Auto-push to GitHub complete: %s/%s",
                            self.settings.github_repo, self.settings.github_file_path)
            except Exception as e:
                logger.warning("Auto-push to GitHub failed: %s", e)

        return results

    async def _deep_scrape_product(
        self,
        client: httpx.AsyncClient,
        product: str,
        releases: Optional[list[str]] = None,
    ) -> tuple[int, int]:
        """Fetch all module index pages for a product and deep-scrape feature details."""
        import sqlite3 as _sq
        rows = self.db._execute(
            """
            SELECT DISTINCT html_url, module, release, product_family
            FROM features
            WHERE product_family = ?
              AND html_url LIKE '%index.html'
              AND html_url NOT LIKE '%www.oracle.com%'
            ORDER BY release DESC
            """,
            (product,),
        ).fetchall()

        if releases:
            rows = [r for r in rows if r["release"].upper() in releases]

        pages = 0
        total_feats = 0
        for row in rows:
            dets = await fetch_feature_details_for_module(
                client,
                row["html_url"],
                row["release"],
                row["product_family"],
                row["module"],
            )
            for d in dets:
                await self.db.upsert_feature_detail(d)
            pages += 1
            total_feats += len(dets)
            await asyncio.sleep(0.2)

        return pages, total_feats

    async def refresh_now(self, products: Optional[list[str]] = None) -> list[dict]:
        async with self._refresh_lock:
            return await self._do_refresh(products)


state = AppState()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Lifespan for stdio mode (Claude Desktop). HTTP mode uses combined_lifespan."""
    if AUTOSTART:
        await state.start_background_refresh()
    else:
        logger.info("Background refresh disabled (READINESS_AUTOSTART_REFRESH=0)")
    yield {}


mcp = FastMCP(
    "oracle_readiness_mcp",
    host=HTTP_HOST,
    port=HTTP_PORT,
)


def _validate_products(products: list[str]) -> None:
    bad = [p for p in products if p not in PRODUCT_NAMES]
    if bad:
        raise ValueError(f"Unknown product(s) {bad}. Valid: {', '.join(PRODUCT_NAMES)}")


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class RefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    products: Optional[list[str]] = Field(
        default=None,
        description=f"Products to refresh. Omit for all ({', '.join(PRODUCT_NAMES)}).",
    )


class ListNotesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str = Field(..., description=f"Product line. One of: {', '.join(PRODUCT_NAMES)}.")
    release: Optional[str] = Field(default=None, description="Release filter e.g. '26C'.")
    limit: int = Field(default=50, ge=1, le=500)


class SearchNotesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=2, max_length=200)
    product: Optional[str] = Field(default=None, description=f"Restrict to one product. One of: {', '.join(PRODUCT_NAMES)}.")
    release: Optional[str] = Field(default=None)
    module: Optional[str] = Field(default=None)
    limit: int = Field(default=50, ge=1, le=200)


class GetModuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: str = Field(..., description="Oracle release code e.g. '26C'.")
    module: str = Field(..., description="Module name (substring match).")


class FilteredFeaturesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: str = Field(..., description="Oracle release code e.g. '26C'.")
    module: Optional[str] = Field(default=None, description="Module name filter (substring match).")


class CompareReleasesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: str = Field(..., description="Module name (substring match).")
    old_release: str = Field(..., description="Earlier release e.g. '26B'.")
    new_release: str = Field(..., description="Later release e.g. '26C'.")


class ListModulesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: str = Field(..., description="Oracle release code e.g. '26C'.")
    product_family: Optional[str] = Field(default=None, description="Product family filter e.g. 'HCM'.")


class ReportFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pillars: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PILLARS),
        description=f"Pillars to include. Any of: {', '.join(PRODUCT_NAMES)}.",
    )
    modules: Optional[list[str]] = Field(default=None, description="Module name substrings (OR'd).")
    releases: Optional[list[str]] = Field(default=None, description="Release codes (OR'd) e.g. ['26C', '26B'].")
    query: Optional[str] = Field(default=None, description="Free-text substring filter on title/description.")


class GenerateReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: ReportFilters = Field(default_factory=ReportFilters)
    include_content: bool = Field(default=False, description="Download full document text for each match.")
    force_redownload: bool = Field(default=False)
    save_report: bool = Field(default=True, description="Write JSON + Markdown report files to disk.")


class DocumentContentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., description="HTML or PDF URL of a readiness document.")
    force_redownload: bool = Field(default=False)


class XlsxIngestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    json_path: str = Field(..., description="Path to Feature_Summary.json from Oracle Readiness Reports Centre XLSX dump (use xlsx-dump tool).")
    source_url: Optional[str] = Field(default=None, description="Source URL to record. Defaults to Readiness App URL.")


# ---------------------------------------------------------------------------
# Tools — catalogue / status
# ---------------------------------------------------------------------------

@mcp.tool(name="list_products",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def list_products() -> dict:
    """List the Oracle Cloud readiness pillars this server tracks."""
    return {
        "readiness_reports_center_url": READINESS_APP_URL,
        "products": {p: {"label": PRODUCT_LABELS[p], "source_url": PRODUCTS[p]} for p in PRODUCT_NAMES},
    }


@mcp.tool(name="get_cache_status",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_cache_status() -> dict:
    """Report when each product was last refreshed, entry counts, and content cache stats."""
    return {
        "metadata": state.db.cache_status(),
        "content":  state.db.content_status(),
        "crawl_log": state.db.get_crawl_log(limit=20),
    }


@mcp.tool(name="list_releases",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def list_releases() -> dict:
    """List all Oracle Fusion releases indexed in the local database."""
    releases = state.db.list_releases()
    if not releases:
        return {"releases": [], "message": "No releases indexed yet. Use refresh_readiness_data to load data."}
    return {"releases": releases}


class ListFamiliesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: Optional[str] = Field(default=None, description="Oracle release code e.g. '26C'. Omit for all releases.")


@mcp.tool(name="list_product_families",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def list_product_families(params: ListFamiliesInput) -> dict:
    """List product families available, optionally filtered to one release."""
    families = state.db.list_product_families(params.release)
    return {"product_families": families, "release": params.release or "all"}


@mcp.tool(name="list_modules",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def list_modules(params: ListModulesInput) -> dict:
    """List modules available for a given release and optional product family."""
    modules = state.db.list_modules(params.release, params.product_family)
    return {"release": params.release, "product_family": params.product_family or "all", "modules": modules}


# ---------------------------------------------------------------------------
# Tools — notes retrieval
# ---------------------------------------------------------------------------

@mcp.tool(name="get_release_notes",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_release_notes(params: ListNotesInput) -> dict:
    """Return cached readiness entries for one Oracle Cloud pillar, optionally filtered by release."""
    _validate_products([params.product])
    entries = state.db.get_entries_for_product(params.product, params.release, params.limit)
    return {
        "product":       params.product,
        "release":       params.release,
        "total_matched": len(entries),
        "entries":       entries,
    }


@mcp.tool(name="search_release_notes",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def search_release_notes(params: SearchNotesInput) -> dict:
    """Case-insensitive substring search across cached release note titles, descriptions, and modules."""
    if params.product:
        _validate_products([params.product])
    entries = state.db.search_features(
        params.query,
        release=params.release,
        module=params.module,
        product_family=params.product,
        limit=params.limit,
    )
    return {"query": params.query, "total_matched": len(entries), "entries": entries}


@mcp.tool(name="get_features_by_module",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_features_by_module(params: GetModuleInput) -> dict:
    """Get all features for a specific release and module (rich schema: impact, enablement, AI flags)."""
    features = state.db.get_features_by_module(params.release, params.module)
    if not features:
        modules = state.db.list_modules(params.release)
        return {"release": params.release, "module": params.module, "total": 0, "features": [],
                "hint": f"No features found. Available modules for {params.release}: {modules[:20]}"}
    return {"release": params.release, "module": params.module, "total": len(features), "features": features}


@mcp.tool(name="get_feature_summary",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_feature_summary(params: GetModuleInput) -> dict:
    """Get statistical summary (impact, setup, AI, Redwood counts) for a release + module."""
    features = state.db.get_features_by_module(params.release, params.module)
    family   = features[0]["product_family"] if features else "Unknown"
    return {
        "release":        params.release,
        "product_family": family,
        "module":         params.module,
        "total":          len(features),
        "large_scale":    sum(1 for f in features if "large" in (f.get("impact") or "").lower()),
        "small_scale":    sum(1 for f in features if "small" in (f.get("impact") or "").lower()),
        "setup_required": sum(1 for f in features if f.get("setup_required")),
        "opt_in_required":sum(1 for f in features if f.get("opt_in_required")),
        "auto_enabled":   sum(1 for f in features if f.get("auto_enabled_in")),
        "redwood":        sum(1 for f in features if f.get("is_redwood")),
        "ai_features":    sum(1 for f in features if f.get("is_ai")),
        "features":       features,
    }


# ---------------------------------------------------------------------------
# Tools — filtered feature views (TS oracle-readiness-mcp parity)
# ---------------------------------------------------------------------------

@mcp.tool(name="get_opt_in_features",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_opt_in_features(params: FilteredFeaturesInput) -> dict:
    """Features in a release that require an Opt-In action to enable."""
    features = state.db.get_filtered_features(params.release, "opt_in", params.module)
    return {"release": params.release, "module": params.module, "filter": "opt_in_required",
            "total": len(features), "features": features}


@mcp.tool(name="get_setup_required_features",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_setup_required_features(params: FilteredFeaturesInput) -> dict:
    """Features in a release that require Setup configuration to be enabled."""
    features = state.db.get_filtered_features(params.release, "setup_required", params.module)
    return {"release": params.release, "module": params.module, "filter": "setup_required",
            "total": len(features), "features": features}


@mcp.tool(name="get_high_impact_features",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_high_impact_features(params: FilteredFeaturesInput) -> dict:
    """Features flagged as Large scale impact — most important for testing and change management."""
    features = state.db.get_filtered_features(params.release, "large_scale", params.module)
    return {"release": params.release, "module": params.module, "filter": "large_scale",
            "total": len(features), "features": features}


@mcp.tool(name="get_auto_enabled_features",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_auto_enabled_features(params: FilteredFeaturesInput) -> dict:
    """Features that will be automatically enabled in a future update — require proactive review."""
    features = state.db.get_filtered_features(params.release, "auto_enabled", params.module)
    return {"release": params.release, "module": params.module, "filter": "auto_enabled_future",
            "total": len(features), "features": features}


@mcp.tool(name="get_ai_features",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_ai_features(params: FilteredFeaturesInput) -> dict:
    """AI and Agent features in a release (Generative AI, Agent, Agentic App)."""
    features = state.db.get_filtered_features(params.release, "ai", params.module)
    return {"release": params.release, "module": params.module, "filter": "ai_features",
            "total": len(features), "features": features}


@mcp.tool(name="get_redwood_features",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_redwood_features(params: FilteredFeaturesInput) -> dict:
    """Features using the Oracle Redwood design system in a release."""
    features = state.db.get_filtered_features(params.release, "redwood", params.module)
    return {"release": params.release, "module": params.module, "filter": "redwood",
            "total": len(features), "features": features}


# ---------------------------------------------------------------------------
# Tools — comparison
# ---------------------------------------------------------------------------

@mcp.tool(name="compare_releases",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def compare_releases(params: CompareReleasesInput) -> dict:
    """Compare Oracle Fusion features between two releases for a module.

    Shows added, changed (same name, different impact/enablement), and removed
    features — plus new large-scale, setup-required, opt-in, and auto-enabled items.
    """
    old_features = state.db.get_features_by_module(params.old_release, params.module)
    new_features = state.db.get_features_by_module(params.new_release, params.module)

    if not old_features and not new_features:
        return {"error": f"No features for module '{params.module}' in {params.old_release} or {params.new_release}."}

    old_map = {f["feature_name"].lower(): f for f in old_features}
    new_map = {f["feature_name"].lower(): f for f in new_features}

    added   = [f for f in new_features if f["feature_name"].lower() not in old_map]
    removed = [f["feature_name"] for f in old_features if f["feature_name"].lower() not in new_map]
    changed = [
        nf for nf in new_features
        if nf["feature_name"].lower() in old_map and (
            old_map[nf["feature_name"].lower()].get("impact")     != nf.get("impact") or
            old_map[nf["feature_name"].lower()].get("enablement") != nf.get("enablement") or
            old_map[nf["feature_name"].lower()].get("setup_required") != nf.get("setup_required")
        )
    ]

    return {
        "module":             params.module,
        "old_release":        params.old_release,
        "new_release":        params.new_release,
        "added":              added,
        "changed":            changed,
        "removed_names":      removed,
        "new_large_scale":    [f for f in added if "large" in (f.get("impact") or "").lower()],
        "new_setup_required": [f for f in added if f.get("setup_required")],
        "new_opt_in":         [f for f in added if f.get("opt_in_required")],
        "new_auto_enabled":   [f for f in added if f.get("auto_enabled_in")],
    }


# ---------------------------------------------------------------------------
# Tools — report generation (ClaudeCode parity)
# ---------------------------------------------------------------------------

@mcp.tool(name="generate_report",
          annotations={"readOnlyHint": False, "idempotentHint": False})
async def generate_report(params: GenerateReportInput, ctx: Context) -> dict:
    """Generate a filtered report of Oracle readiness features by Pillar, Module, and Update.

    Mirrors the Readiness Reports Centre's Pillar / Module / Update filter facets.
    Optionally downloads full document text for each matched entry.
    Always writes a JSON + Markdown report file pair to the reports directory on disk.
    """
    _validate_products(params.filters.pillars)
    entries = state.db.filter_entries(
        pillars=params.filters.pillars,
        modules=params.filters.modules,
        releases=params.filters.releases,
        query=params.filters.query,
    )

    content_by_url: dict[str, dict] = {}
    if params.include_content and entries:
        urls = sorted({e.get("html_url") or e.get("pdf_url") for e in entries if (e.get("html_url") or e.get("pdf_url"))})
        await ctx.log_info(f"Fetching content for {len(urls)} document(s)…")
        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(4)
            async def _fetch_one(url):
                async with sem:
                    cached = state.db.get_content(url)
                    if cached and not params.force_redownload:
                        return cached
                    try:
                        doc = await fetch_document_content(client, url)
                        await state.db.cache_content(doc)
                        return doc
                    except Exception as e:
                        return {"url": url, "error": str(e)}
            docs = await asyncio.gather(*(_fetch_one(u) for u in urls))
        for doc in docs:
            if "url" in doc:
                content_by_url[doc["url"]] = doc

    full_entries = []
    for e in entries:
        e2 = dict(e)
        if params.include_content:
            u = e.get("html_url") or e.get("pdf_url")
            doc = content_by_url.get(u) if u else None
            if doc and "content" in doc:
                e2["content"] = doc["content"]
                e2["content_truncated"] = doc.get("truncated", False)
            elif doc and "error" in doc:
                e2["content_error"] = doc["error"]
        full_entries.append(e2)

    report = {
        "generated_at": time.time(),
        "readiness_reports_center_url": READINESS_APP_URL,
        "filters": params.filters.model_dump(),
        "total_matched": len(full_entries),
        "entries": full_entries,
    }

    report_json_path = report_md_path = None
    if params.save_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        jp = REPORTS_DIR / f"report_{stamp}.json"
        mp = REPORTS_DIR / f"report_{stamp}.md"
        jp.write_text(json.dumps(report, indent=2))
        mp.write_text(_render_markdown(report))
        report_json_path = str(jp)
        report_md_path   = str(mp)

    # Trim inline content for context safety
    resp_entries, inlined = [], 0
    for e in full_entries:
        er = dict(e)
        if "content" in er:
            if inlined >= MAX_INLINE_CONTENT:
                del er["content"]
            else:
                inlined += 1
        resp_entries.append(er)

    return {
        "filters":                        params.filters.model_dump(),
        "total_matched":                  len(full_entries),
        "entries":                        resp_entries,
        "content_inlined":                inlined,
        "content_truncated_for_response": params.include_content and inlined < len(full_entries),
        "report_json_path":               report_json_path,
        "report_markdown_path":           report_md_path,
    }


def _render_markdown(report: dict) -> str:
    lines = [
        "# Oracle Cloud Readiness Report",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(report['generated_at']))}",
        f"Source: [Readiness Reports Center]({report['readiness_reports_center_url']})",
        f"Filters: `{json.dumps(report['filters'])}`",
        f"Total matched: {report['total_matched']}", "",
    ]
    for e in report["entries"]:
        lines.append(f"## {e['feature_name']}")
        lines.append(
            f"- Pillar: {e['product_family']}  |  Module: {e.get('module', '')}  "
            f"|  Release: {e.get('release') or 'n/a'}  "
            f"|  Impact: {e.get('impact') or 'n/a'}"
        )
        if e.get("enablement"):   lines.append(f"- Enablement: {e['enablement']}")
        if e.get("is_ai"):        lines.append(f"- AI Type: {e.get('ai_type', 'AI')}")
        if e.get("is_redwood"):   lines.append("- Redwood: Yes")
        if e.get("auto_enabled_in"): lines.append(f"- Auto-enabled in: {e['auto_enabled_in']}")
        if e.get("html_url"):     lines.append(f"- HTML: {e['html_url']}")
        if e.get("pdf_url"):      lines.append(f"- PDF: {e['pdf_url']}")
        if e.get("description"):  lines.append(f"\n{e['description']}")
        if e.get("content"):
            lines += ["", "<details><summary>Full content</summary>", "", e["content"], "", "</details>"]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools — document content
# ---------------------------------------------------------------------------

@mcp.tool(name="get_document_content",
          annotations={"readOnlyHint": False, "idempotentHint": True})
async def get_document_content(params: DocumentContentInput) -> dict:
    """Download (or serve from local cache) the full text of a single readiness document.

    HTML pages are converted to Markdown; PDFs have their text extracted.
    """
    if not params.force_redownload:
        cached = state.db.get_content(params.url)
        if cached:
            return cached
    async with httpx.AsyncClient() as client:
        doc = await fetch_document_content(client, params.url)
    await state.db.cache_content(doc)
    return doc


# ---------------------------------------------------------------------------
# Tools — refresh
# ---------------------------------------------------------------------------

@mcp.tool(name="refresh_readiness_data",
          annotations={"readOnlyHint": False, "idempotentHint": True})
async def refresh_readiness_data(params: RefreshInput, ctx: Context) -> dict:
    """Fetch the latest readiness metadata from Oracle right now, without waiting for the scheduled refresh."""
    if params.products:
        _validate_products(params.products)
    await ctx.log_info(f"Refreshing Oracle readiness data for: {params.products or 'all products'}")
    results = await state.refresh_now(params.products)
    return {"results": results}


# ---------------------------------------------------------------------------
# Tools — XLSX ingestion (from oracle-readiness-mcp TS parity)
# ---------------------------------------------------------------------------

@mcp.tool(name="ingest_xlsx_dump",
          annotations={"readOnlyHint": False, "idempotentHint": True})
async def ingest_xlsx_dump(params: XlsxIngestInput) -> dict:
    """Load Oracle Fusion release features from a local XLSX JSON dump.

    The dump must be a JSON file with {"headers": [...], "rows": [[...],...]}
    shape — e.g. the Feature_Summary.json produced by the bob xlsx-dump tool.
    """
    path = Path(params.json_path)
    if not path.exists():
        return {"error": f"File not found: {params.json_path}"}
    try:
        raw = json.loads(path.read_text())
        features = parse_features_from_xlsx_dump(
            raw["rows"], raw["headers"],
            source_url=params.source_url or READINESS_APP_URL,
        )
        count = await state.db.upsert_features(features)
        return {"features_loaded": count, "source": str(path),
                "message": f"Successfully loaded {count} features from {params.json_path}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# GitHub structured publish — taxonomy tree with crosslinks
# ---------------------------------------------------------------------------

import base64 as _base64


def _slug(text: str) -> str:
    """Convert text to a safe path/anchor slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _render_feature_md(detail: dict, feature: dict,
                       release: str, pillar: str, module: str,
                       repo: str, branch: str) -> str:
    """Render one feature detail page as GitHub Flavored Markdown."""
    name  = detail.get("feature_name") or feature.get("feature_name", "Unknown Feature")
    base  = f"https://github.com/{repo}/blob/{branch}"
    idx_p = f"readiness/{_slug(release)}/index.md"
    pil_p = f"readiness/{_slug(release)}/{_slug(pillar)}/index.md"
    mod_p = f"readiness/{_slug(release)}/{_slug(pillar)}/{_slug(module)}.md"
    lines: list[str] = [
        f"[📋 {release}]({base}/{idx_p}) › [{pillar.upper()}]({base}/{pil_p}) › [{module}]({base}/{mod_p})",
        "", f"# {name}", "",
    ]
    flags = []
    if detail.get("optional_uptake"):      flags.append("`🔧 Optional Uptake`")
    if detail.get("steps_to_enable"):      flags.append("`⚙️ Steps to Enable`")
    if detail.get("tips_considerations"):  flags.append("`💡 Tips & Considerations`")
    if detail.get("access_requirements"):  flags.append("`🔐 Access Requirements`")
    if flags:
        lines += [" · ".join(flags), ""]
    enab = feature.get("enablement") or detail.get("description_full", "")[:0]
    impact = feature.get("impact")
    meta_parts = []
    if impact:  meta_parts.append(f"**Impact:** {impact}")
    if enab:    meta_parts.append(f"**Enablement:** {enab}")
    if meta_parts: lines += [" · ".join(meta_parts), ""]
    oracle_url = detail.get("feature_page_url") or feature.get("html_url", "")
    if oracle_url:
        lines += [f"> 📖 [View on Oracle Help Center]({oracle_url})", ""]
    if detail.get("description_full"):
        lines += [detail["description_full"], ""]
    for key, heading, emoji in [
        ("business_benefit",    "Business Benefit",              "🎯"),
        ("steps_to_enable",     "Steps to Enable and Configure", "⚙️"),
        ("tips_considerations", "Tips and Considerations",       "💡"),
        ("access_requirements", "Access Requirements",           "🔐"),
        ("key_resources",       "Key Resources",                 "📚"),
    ]:
        content = detail.get(key)
        if content:
            lines += [f"## {emoji} {heading}", "", content, ""]
    other = detail.get("other_sections")
    if other:
        try:
            others = json.loads(other) if isinstance(other, str) else other
            for sec in others:
                lines += [f"## {sec['heading']}", "", sec.get("content", ""), ""]
        except Exception:
            pass
    lines += ["---", f"*Oracle Cloud Readiness · {release} · {pillar.upper()} · {module}*"]
    return "\n".join(lines)


def _render_module_md(release: str, pillar: str, module: str,
                      cat_features: list[dict], details: list[dict],
                      repo: str, branch: str) -> str:
    """Module index: table of all scraped feature detail pages, crosslinked.

    `details`     — rows from feature_details (one per individual feature page).
    `cat_features` — rows from features catalogue (used only for enrichment:
                      enablement, impact, html_url).  Often just 1 row (the module
                      index entry) so most enrichment comes from the detail record.
    """
    base  = f"https://github.com/{repo}/blob/{branch}"
    idx_p = f"readiness/{_slug(release)}/index.md"
    pil_p = f"readiness/{_slug(release)}/{_slug(pillar)}/index.md"
    lines: list[str] = [
        f"[📋 {release}]({base}/{idx_p}) › [{pillar.upper()}]({base}/{pil_p})",
        "", f"# {module}", f"*{release} · {pillar.upper()}*", "",
    ]

    # Catalogue lookup — keyed by lowercase feature name for enrichment
    cat_map = {c.get("feature_name", "").lower().strip(): c for c in cat_features}

    # Use detail rows as the authoritative feature list
    display_rows = details if details else cat_features

    n_steps = sum(1 for d in display_rows if d.get("steps_to_enable"))
    n_tips  = sum(1 for d in display_rows if d.get("tips_considerations"))
    n_opt   = sum(1 for d in display_rows if d.get("optional_uptake"))

    lines += [
        f"| Features | With Steps | With Tips | Optional Uptake |",
        f"|----------|-----------|-----------|-----------------|",
        f"| {len(display_rows)} | {n_steps} | {n_tips} | {n_opt} |", "",
        "| Feature | Enablement | Flags | Oracle |",
        "|---------|-----------|-------|--------|",
    ]

    for row in sorted(display_rows, key=lambda x: x.get("feature_name", "")):
        fname    = row.get("feature_name", "")
        feat_rel = row.get("release") or release
        # Internal detail-page link — always within the module subfolder
        fp   = (f"readiness/{_slug(feat_rel)}/{_slug(pillar)}/"
                f"{_slug(module)}/{_slug(fname)}.md")
        link = f"[{fname}]({base}/{fp})"

        # Enrichment from catalogue (may be absent for all-detail rows)
        cat  = cat_map.get(fname.lower().strip(), {})
        enab = cat.get("enablement") or "—"

        # Flags from detail record (most authoritative)
        fl = ("⚙️" if row.get("steps_to_enable")    else "") + \
             ("💡" if row.get("tips_considerations") else "") + \
             ("🔧" if row.get("optional_uptake")     else "")

        # Oracle source link — prefer detail page URL, fall back to catalogue html_url
        oracle_url = row.get("feature_page_url") or cat.get("html_url") or ""
        ora = f"[↗]({oracle_url})" if oracle_url else "—"

        lines.append(f"| {link} | {enab} | {fl or '—'} | {ora} |")

    lines += ["", "---", f"*Oracle Cloud Readiness · {release} · {pillar.upper()}*"]
    return "\n".join(lines)


def _render_pillar_md(release: str, pillar: str, mod_stats: dict,
                      repo: str, branch: str) -> str:
    """Pillar index: all modules for a release/pillar."""
    base  = f"https://github.com/{repo}/blob/{branch}"
    idx_p = f"readiness/{_slug(release)}/index.md"
    label = PRODUCT_LABELS.get(pillar.lower(), pillar.upper())
    lines: list[str] = [
        f"[📋 {release}]({base}/{idx_p})",
        "", f"# {label} — {release}", "",
        "| Module | Features | Steps | Tips | Optional |",
        "|--------|---------|-------|------|----------|",
    ]
    for mod in sorted(mod_stats):
        st   = mod_stats[mod]
        mp   = f"readiness/{_slug(release)}/{_slug(pillar)}/{_slug(mod)}.md"
        lines.append(
            f"| [{mod}]({base}/{mp}) | {st.get('features',0)} "
            f"| {st.get('steps',0)} | {st.get('tips',0)} | {st.get('optional',0)} |"
        )
    lines += ["", "---", f"*Oracle Cloud Readiness · {release} · {pillar.upper()}*"]
    return "\n".join(lines)


def _render_release_md(release: str, pillar_stats: dict,
                       repo: str, branch: str) -> str:
    """Release index: all pillars for a release."""
    base = f"https://github.com/{repo}/blob/{branch}"
    root = f"readiness/index.md"
    total_f = sum(p.get("features", 0) for p in pillar_stats.values())
    total_d = sum(p.get("details", 0) for p in pillar_stats.values())
    lines: list[str] = [
        f"[📋 All Releases]({base}/{root})",
        "", f"# Oracle Cloud {release} — What's New",
        "",
        f"**{total_f} features** · **{total_d} detail pages**", "",
        "| Pillar | Features | Details | Steps | Tips | Optional |",
        "|--------|---------|---------|-------|------|----------|",
    ]
    for pillar in sorted(pillar_stats):
        st  = pillar_stats[pillar]
        pp  = f"readiness/{_slug(release)}/{_slug(pillar)}/index.md"
        lbl = PRODUCT_LABELS.get(pillar.lower(), pillar.upper())
        lines.append(
            f"| [{lbl}]({base}/{pp}) | {st.get('features',0)} | {st.get('details',0)} "
            f"| {st.get('steps',0)} | {st.get('tips',0)} | {st.get('optional',0)} |"
        )
    lines += ["", "---", f"*Generated by oracle-readiness-mcp · {time.strftime('%Y-%m-%d')}*"]
    return "\n".join(lines)


def _render_root_md(release_stats: dict, repo: str, branch: str) -> str:
    """Root readiness/index.md — entry point."""
    base = f"https://github.com/{repo}/blob/{branch}"
    lines: list[str] = [
        "# Oracle Cloud Readiness — Release Index", "",
        "Automatically generated from the Oracle Cloud Readiness catalogue.", "",
        "| Release | Features | Detail Pages |",
        "|---------|---------|-------------|",
    ]
    for rel in sorted(release_stats, reverse=True):
        st  = release_stats[rel]
        rp  = f"readiness/{_slug(rel)}/index.md"
        lines.append(f"| [{rel}]({base}/{rp}) | {st.get('features',0)} | {st.get('details',0)} |")
    lines += ["", "---", f"*Generated by oracle-readiness-mcp · {time.strftime('%Y-%m-%d')}*"]
    return "\n".join(lines)


async def _gh_put(client: httpx.AsyncClient, token: str, repo: str, branch: str,
                  path: str, content: str, message: str, sha_cache: dict) -> None:
    """PUT one markdown file to GitHub; reuse existing SHA to avoid conflicts.

    Handles the case where the recursive tree fetch was truncated (>100k nodes):
    on a 422/409/500 we do a fresh GET on the specific path to obtain its SHA,
    then retry once.
    """
    gh_headers = {"Authorization": f"Bearer {token}",
                  "Accept": "application/vnd.github.v3+json"}
    encoded = _base64.b64encode(content.encode()).decode()

    async def _do_put(sha: Optional[str]) -> httpx.Response:
        body: dict = {"message": message, "content": encoded, "branch": branch}
        if sha:
            body["sha"] = sha
        return await client.put(
            f"https://api.github.com/repos/{repo}/contents/{path}",
            json=body, headers=gh_headers, timeout=30.0,
        )

    sha = sha_cache.get(path)
    r = await _do_put(sha)

    # On conflict/server-error try to fetch the current SHA and retry once
    if r.status_code in (409, 422, 500):
        try:
            gr = await client.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=gh_headers, params={"ref": branch}, timeout=15.0,
            )
            if gr.status_code == 200:
                sha = gr.json().get("sha")
                sha_cache[path] = sha
                r = await _do_put(sha)
        except Exception:
            pass  # fall through to the error raise below

    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub PUT {path} → {r.status_code}: {r.text[:300]}")
    try:
        sha_cache[path] = r.json()["content"]["sha"]
    except Exception:
        pass


async def _github_push(
    token: str,
    repo: str,
    branch: str = "main",
    file_path: str = "readiness/latest.md",  # kept for compat, ignored in tree mode
    pillars: Optional[list[str]] = None,
    modules: Optional[list[str]] = None,
    releases: Optional[list[str]] = None,
    query: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> dict:
    """Publish a structured readiness taxonomy tree to GitHub.

    Tree layout:
      readiness/index.md
      readiness/{release}/index.md
      readiness/{release}/{pillar}/index.md
      readiness/{release}/{pillar}/{module}.md
      readiness/{release}/{pillar}/{module}/{feature}.md
    All pages carry breadcrumb crosslinks.
    """
    if not token:
        raise ValueError("No GitHub token. Set it in Settings → GitHub.")
    if not repo:
        raise ValueError("No GitHub repo. Set it in Settings → GitHub.")

    effective_pillars = pillars or list(DEFAULT_PILLARS)
    commit_msg = commit_message or (
        f"Oracle Readiness taxonomy — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
    )

    # Gather all releases from DB (or filter to requested ones)
    all_releases = state.db.list_releases()
    target_releases = (
        [r.upper() for r in releases] if releases
        else [r for r in all_releases if r != "Unknown"]
    )

    files_pushed = 0
    release_stats: dict = {}

    async with httpx.AsyncClient() as client:
        # Pre-fetch existing SHAs to avoid duplicate-file conflicts
        sha_cache: dict = {}
        try:
            tree_r = await client.get(
                f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github.v3+json"},
                timeout=30.0,
            )
            if tree_r.status_code == 200:
                sha_cache = {
                    item["path"]: item["sha"]
                    for item in tree_r.json().get("tree", [])
                    if item["type"] == "blob" and item["path"].startswith("readiness/")
                }
        except Exception:
            pass

        for release in target_releases:
            pillar_stats: dict = {}

            for pillar in effective_pillars:
                features = state.db.get_entries_for_product(pillar, release=release, limit=2000)
                if not features:
                    continue
                details  = state.db.get_all_details_for_release(release, pillar)
                det_map  = {d.get("feature_name", "").lower(): d for d in details}

                # Group by module
                mods: dict = {}
                for f in features:
                    mod = f.get("module", "Unknown")
                    mods.setdefault(mod, []).append(f)

                mod_stats: dict = {}
                for module, mod_feats in mods.items():
                    # Build detail map: match by feature name (case-insensitive strip)
                    mod_det_rows = state.db.get_details_for_module(release, pillar, module)
                    det_map_local = {d.get("feature_name", "").lower().strip(): d
                                     for d in mod_det_rows}
                    mod_details = list(det_map_local.values())
                    n_steps = sum(1 for d in mod_details if d.get("steps_to_enable"))
                    n_tips  = sum(1 for d in mod_details if d.get("tips_considerations"))
                    n_opt   = sum(1 for d in mod_details if d.get("optional_uptake"))
                    mod_stats[module] = {
                        "features": len(mod_det_rows) if mod_det_rows else len(mod_feats),
                        "steps": n_steps, "tips": n_tips, "optional": n_opt,
                    }

                    # Module index page
                    mod_md   = _render_module_md(release, pillar, module,
                                                 mod_feats, mod_details, repo, branch)
                    mod_path = f"readiness/{_slug(release)}/{_slug(pillar)}/{_slug(module)}.md"
                    await _gh_put(client, token, repo, branch, mod_path,
                                  mod_md, commit_msg, sha_cache)
                    files_pushed += 1

                    # Individual feature detail pages — push ALL scraped details
                    # (includes features from previous releases carried in the doc)
                    for det in mod_det_rows:
                        fname = det.get("feature_name", "")
                        if not fname:
                            continue
                        # Find matching catalogue feature for metadata enrichment
                        cat_feat = det_map_local.get(fname.lower().strip()) or {}
                        # Fall back to catalogue features table for impact/enablement
                        cat_rows = state.db.get_features_by_module(
                            det.get("release", release), module
                        )
                        cat_feat_row = next(
                            (f for f in cat_rows
                             if f.get("feature_name","").lower().strip() == fname.lower().strip()),
                            {}
                        )
                        feat_rel  = det.get("release") or release
                        feat_md   = _render_feature_md(det, cat_feat_row,
                                                       feat_rel, pillar, module,
                                                       repo, branch)
                        feat_path = (f"readiness/{_slug(feat_rel)}/{_slug(pillar)}/"
                                     f"{_slug(module)}/{_slug(fname)}.md")
                        await _gh_put(client, token, repo, branch, feat_path,
                                      feat_md, commit_msg, sha_cache)
                        files_pushed += 1
                        await asyncio.sleep(0.15)  # respect GitHub secondary rate limit

                # Pillar index page
                pil_md   = _render_pillar_md(release, pillar, mod_stats, repo, branch)
                pil_path = f"readiness/{_slug(release)}/{_slug(pillar)}/index.md"
                await _gh_put(client, token, repo, branch, pil_path,
                              pil_md, commit_msg, sha_cache)
                files_pushed += 1

                total_steps   = sum(s.get("steps", 0) for s in mod_stats.values())
                total_tips    = sum(s.get("tips", 0) for s in mod_stats.values())
                total_opt     = sum(s.get("optional", 0) for s in mod_stats.values())
                pillar_stats[pillar] = {
                    "features": len(features),
                    "details":  len(details),
                    "steps":    total_steps,
                    "tips":     total_tips,
                    "optional": total_opt,
                }

            if not pillar_stats:
                continue

            # Release index page
            rel_md   = _render_release_md(release, pillar_stats, repo, branch)
            rel_path = f"readiness/{_slug(release)}/index.md"
            await _gh_put(client, token, repo, branch, rel_path,
                          rel_md, commit_msg, sha_cache)
            files_pushed += 1

            release_stats[release] = {
                "features": sum(p.get("features",0) for p in pillar_stats.values()),
                "details":  sum(p.get("details",0) for p in pillar_stats.values()),
            }

        # Root index
        root_md = _render_root_md(release_stats, repo, branch)
        await _gh_put(client, token, repo, branch, "readiness/index.md",
                      root_md, commit_msg, sha_cache)
        files_pushed += 1

    return {
        "ok":           True,
        "files_pushed": files_pushed,
        "releases":     target_releases,
        "pillars":      effective_pillars,
        "root_url":     f"https://github.com/{repo}/blob/{branch}/readiness/index.md",
        "commit_message": commit_msg,
    }


class GitHubPushInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: Optional[str] = Field(
        default=None,
        description="GitHub repo in owner/repo format. Falls back to the repo configured in Settings.",
    )
    path: Optional[str] = Field(
        default=None,
        description="File path inside the repo. Falls back to the path configured in Settings.",
    )
    branch: Optional[str] = Field(default=None, description="Branch to push to. Falls back to Settings.")
    commit_message: str = Field(default="", description="Commit message. Defaults to a timestamped auto-message.")
    pillars: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PILLARS),
        description="Pillars to include in the pushed report.",
    )
    modules: Optional[list[str]] = Field(default=None)
    releases: Optional[list[str]] = Field(default=None)
    query: Optional[str] = Field(default=None)
    github_token: Optional[str] = Field(
        default=None,
        description="GitHub PAT. Falls back to the token configured in Settings, then GITHUB_TOKEN env var.",
    )


@mcp.tool(name="push_report_to_github",
          annotations={"readOnlyHint": False, "destructiveHint": False,
                       "idempotentHint": False, "openWorldHint": True})
async def push_report_to_github(params: GitHubPushInput, ctx: Context) -> dict:
    """Generate a Markdown report from cached Oracle readiness data and commit it
    to GitHub using the Contents REST API (no git binary required).

    Token, repo, branch and file path all fall back to values saved in
    Settings → GitHub when not supplied explicitly.
    """
    token     = params.github_token or state.settings.github_token or os.environ.get("GITHUB_TOKEN", "")
    repo      = params.repo      or state.settings.github_repo
    branch    = params.branch    or state.settings.github_branch    or "main"
    file_path = params.path      or state.settings.github_file_path or "readiness/latest.md"

    _validate_products(params.pillars)
    await ctx.log_info(f"Pushing {len(params.pillars)}-pillar report to github.com/{repo}/{file_path}")

    try:
        result = await _github_push(
            token=token, repo=repo, branch=branch, file_path=file_path,
            pillars=params.pillars, modules=params.modules,
            releases=params.releases, query=params.query,
            commit_message=params.commit_message,
        )
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# MCP Tools — feature detail (deep-scraped sections)
# ---------------------------------------------------------------------------

class FeatureDetailInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    feature_name:   str            = Field(..., description="Exact feature name")
    release:        str            = Field(..., description="Release code, e.g. 26C")
    product_family: str            = Field(..., description="Pillar: erp, hcm, scm, service")

class FeatureDetailUrlInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    url: str = Field(..., description="feature_page_url from a feature record")

class DetailListInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    release:        Optional[str] = Field(None, description="Filter by release, e.g. 26C")
    product_family: Optional[str] = Field(None, description="Filter by pillar")
    module:         Optional[str] = Field(None, description="Filter by module (substring)")

class DetailSearchInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    query:          str            = Field(..., description="Search terms")
    release:        Optional[str] = Field(None)
    product_family: Optional[str] = Field(None)

class DeepScrapeInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    products:  Optional[list[str]] = Field(None, description="Pillars to deep-scrape; default all")
    releases:  Optional[list[str]] = Field(None, description="Releases to limit scrape to, e.g. ['26C']")


@mcp.tool(name="get_feature_detail",
          description="Return the full deep detail for one feature: description, business benefit, "
                      "steps to enable/configure, tips and considerations, access requirements, "
                      "key resources, and any other named sections scraped from the Oracle What's New page.")
async def get_feature_detail(params: FeatureDetailInput) -> dict:
    detail = state.db.get_feature_detail_by_name(
        params.feature_name, params.release, params.product_family
    )
    if not detail:
        return {
            "found": False,
            "message": (
                f"No detail scraped yet for '{params.feature_name}' ({params.release}/{params.product_family}). "
                "Run deep_scrape_feature_details to fetch it."
            ),
        }
    # Parse other_sections from JSON
    if detail.get("other_sections"):
        try:
            detail["other_sections"] = json.loads(detail["other_sections"])
        except Exception:
            pass
    detail["found"] = True
    return detail


@mcp.tool(name="get_feature_detail_by_url",
          description="Return the full deep detail for one feature by its Oracle page URL.")
async def get_feature_detail_by_url(params: FeatureDetailUrlInput) -> dict:
    detail = state.db.get_feature_detail(params.url)
    if not detail:
        return {"found": False, "message": f"No detail cached for URL: {params.url}"}
    if detail.get("other_sections"):
        try:
            detail["other_sections"] = json.loads(detail["other_sections"])
        except Exception:
            pass
    detail["found"] = True
    return detail


@mcp.tool(name="list_features_with_steps",
          description="List features that have 'Steps to enable and configure' content scraped. "
                      "Filter by release, pillar or module.")
async def list_features_with_steps(params: DetailListInput) -> dict:
    rows = state.db.list_features_with_steps(
        release=params.release,
        product_family=params.product_family,
        module=params.module,
    )
    return {"count": len(rows), "features": rows}


@mcp.tool(name="list_features_with_tips",
          description="List features that have 'Tips and considerations' content scraped. "
                      "Filter by release, pillar or module.")
async def list_features_with_tips(params: DetailListInput) -> dict:
    rows = state.db.list_features_with_tips(
        release=params.release,
        product_family=params.product_family,
        module=params.module,
    )
    return {"count": len(rows), "features": rows}


@mcp.tool(name="search_feature_details",
          description="Full-text search across all scraped detail sections: description, steps to enable, "
                      "tips, business benefit, access requirements, key resources.")
async def search_feature_details(params: DetailSearchInput) -> dict:
    rows = state.db.search_feature_details(
        query=params.query,
        release=params.release,
        product_family=params.product_family,
    )
    for row in rows:
        if row.get("other_sections"):
            try:
                row["other_sections"] = json.loads(row["other_sections"])
            except Exception:
                pass
    return {"count": len(rows), "results": rows}


@mcp.tool(name="deep_scrape_feature_details",
          description="Trigger on-demand deep-scrape of individual feature detail pages "
                      "(steps to enable, tips, access requirements, etc.) for given products/releases. "
                      "This is also run automatically during each scheduled refresh.")
async def deep_scrape_feature_details(params: DeepScrapeInput, ctx: Context) -> dict:
    products = params.products or [p for p in PRODUCT_NAMES if p != "news"]
    releases = [r.upper() for r in params.releases] if params.releases else None
    await ctx.log_info(f"Deep-scraping feature details for: {products}, releases: {releases or 'all'}")

    total_pages = 0
    total_feats = 0
    async with httpx.AsyncClient() as client:
        for product in products:
            try:
                pages, feats = await state._deep_scrape_product(client, product, releases)
                total_pages += pages
                total_feats += feats
                await ctx.log_info(f"  {product}: {pages} modules, {feats} features")
            except Exception as e:
                await ctx.log_info(f"  {product}: failed — {e}")

    # Push to GitHub if auto-push enabled
    if state.settings.github_auto_push:
        try:
            await _github_push(
                token=state.settings.github_token,
                repo=state.settings.github_repo,
                branch=state.settings.github_branch,
                file_path=state.settings.github_file_path,
                pillars=state.settings.active_pillars,
            )
        except Exception as e:
            logger.warning("Auto-push after deep scrape failed: %s", e)

    return {
        "ok": True,
        "modules_scraped": total_pages,
        "feature_details_fetched": total_feats,
        "detail_counts": state.db.detail_status(),
    }


# ---------------------------------------------------------------------------
# MCP Tool — ICA framer CSV export
# ---------------------------------------------------------------------------

class IcaCsvInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: str = Field(
        ...,
        description=(
            "Which ICA CSV to generate. One of: features, actions, modules, "
            "releases, action-types, derivation-methods, schema-changes."
        ),
    )
    release: Optional[str] = Field(
        default=None,
        description="Filter features/actions to a specific release e.g. '26C'. Omit for all.",
    )
    pillar: Optional[str] = Field(
        default=None,
        description="Filter features/actions to a product family e.g. 'HCM'. Omit for all.",
    )


@mcp.tool(name="get_ica_framer_csv",
          annotations={"readOnlyHint": True, "idempotentHint": True})
async def get_ica_framer_csv(params: IcaCsvInput) -> dict:
    """Return an ICA Context Studio 'Upload Sample Data' CSV for the requested entity type.

    Use this to populate the 26c Complete Ontology schema extensions described
    in the ICA Schema — Targeted Changes plan.  The CSV can be pasted directly
    into the Schema Builder → Upload Sample Data dialog.

    entity_type values:
      features          — Feature nodes (all 965+ with AI/Redwood/optIn flags in contextText)
      actions           — Action nodes (Steps to Enable, Business Benefit, Key Resources, Tips)
      modules           — Module enum extension (missing modules from live data)
      releases          — Release code enum extension (26D, 27A, 27B)
      action-types      — ActionType enum extension (Business Benefit, Key Resources)
      derivation-methods — DerivationMethod M017_MCP_FRAMER_INGESTION
      schema-changes    — JSON manifest of all required schema changes with status/endpoints
    """
    et = (params.entity_type or "").lower().strip()

    if et == "releases":
        rows = _ica.build_releases_csv()
        return {"entity_type": et, "row_count": len(rows), "csv": _ica.csv_response(rows)}

    if et == "action-types":
        rows = _ica.build_action_types_csv()
        return {"entity_type": et, "row_count": len(rows), "csv": _ica.csv_response(rows)}

    if et == "derivation-methods":
        rows = _ica.build_derivation_methods_csv()
        return {"entity_type": et, "row_count": len(rows), "csv": _ica.csv_response(rows)}

    if et == "modules":
        releases = state.db.list_releases()
        live_modules: list[str] = []
        for rel in releases:
            live_modules.extend(state.db.list_modules(rel))
        rows = _ica.build_modules_csv(sorted(set(live_modules)))
        return {"entity_type": et, "row_count": len(rows), "csv": _ica.csv_response(rows)}

    if et == "features":
        features = state.db.filter_entries(
            pillars=[params.pillar] if params.pillar else None,
            releases=[params.release] if params.release else None,
        )
        rows = _ica.build_features_csv(features)
        return {"entity_type": et, "row_count": len(rows), "csv": _ica.csv_response(rows)}

    if et == "actions":
        if params.release:
            details = state.db.get_all_details_for_release(params.release, params.pillar)
        else:
            details = []
            for rel in state.db.list_releases():
                details.extend(state.db.get_all_details_for_release(rel, params.pillar))
        rows = _ica.build_actions_csv(details)
        return {"entity_type": et, "row_count": len(rows), "csv": _ica.csv_response(rows)}

    if et == "schema-changes":
        return {"entity_type": et, "schema_changes": _ica.SCHEMA_CHANGES}

    valid = ["features", "actions", "modules", "releases", "action-types",
             "derivation-methods", "schema-changes"]
    return {
        "error": f"Unknown entity_type '{params.entity_type}'. Valid values: {valid}",
        "valid_types": valid,
    }


# ---------------------------------------------------------------------------
# MCP Resource
# ---------------------------------------------------------------------------

@mcp.resource("readiness://product/{product}")
async def readiness_product_resource(product: str) -> str:
    """Expose a product's cached entries as a raw JSON MCP resource."""
    _validate_products([product])
    entries = state.db.get_entries_for_product(product, limit=500)
    return json.dumps({"product": product, "entries": entries}, indent=2)


# ---------------------------------------------------------------------------
# REST API endpoints for the web UI
# ---------------------------------------------------------------------------

# ── Auth helpers ─────────────────────────────────────────────────────────────

def _get_session_token(request: Request) -> str:
    """Extract session token from Authorization header or session_token cookie."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.cookies.get("session_token", "")


def _require_auth(request: Request) -> Optional[dict]:
    """Return user dict or None if session is invalid."""
    return state.auth.get_session_user(_get_session_token(request))


def _require_admin(request: Request) -> Optional[dict]:
    """Return user dict only if user is an active admin, else None."""
    user = _require_auth(request)
    if user and user.get("role") == "admin":
        return user
    return None


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "")


# ── Auth endpoints ────────────────────────────────────────────────────────────

async def _api_login(request: Request) -> JSONResponse:
    """POST /api/auth/login  body: {username, password}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    ip = _client_ip(request)
    user = state.auth.verify_password(username, password)
    if not user:
        state.auth.audit("LOGIN_FAIL", f"username={username}", ip=ip)
        return JSONResponse({"ok": False, "error": "Invalid credentials or account disabled"}, status_code=401)
    token = state.auth.create_session(user["id"], ip=ip, user_agent=request.headers.get("user-agent", ""))
    state.auth.audit("LOGIN_OK", f"username={username}", user_id=user["id"], username=username, ip=ip)
    resp = JSONResponse({"ok": True, "token": token, "role": user["role"], "username": user["username"]})
    resp.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=86400 * 1)
    return resp


async def _api_logout(request: Request) -> JSONResponse:
    """POST /api/auth/logout"""
    token = _get_session_token(request)
    user  = state.auth.get_session_user(token)
    if user:
        state.auth.audit("LOGOUT", user_id=user["id"], username=user["username"], ip=_client_ip(request))
    state.auth.invalidate_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session_token")
    return resp


async def _api_session_check(request: Request) -> JSONResponse:
    """GET /api/auth/me — returns current user or 401."""
    user = _require_auth(request)
    if not user:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    return JSONResponse({"ok": True, "username": user["username"], "role": user["role"]})


# ── User management endpoints (admin only) ─────────────────────────────────

async def _api_list_users(request: Request) -> JSONResponse:
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse({"users": state.auth.list_users()})


async def _api_create_user(request: Request) -> JSONResponse:
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        user = state.auth.create_user(
            body.get("username", ""), body.get("password", ""), body.get("role", "user")
        )
        state.auth.audit("USER_CREATE", f"new_user={user['username']} role={user['role']}",
                         user_id=admin["id"], username=admin["username"], ip=_client_ip(request))
        return JSONResponse({"ok": True, "user": {k: user[k] for k in ("id","username","role","active","created_at")}})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def _api_update_user(request: Request) -> JSONResponse:
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = int(request.path_params["user_id"])
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        if "active" in body:
            state.auth.set_active(uid, bool(body["active"]))
            state.auth.audit("USER_ACTIVE", f"user_id={uid} active={body['active']}",
                             user_id=admin["id"], username=admin["username"], ip=_client_ip(request))
        if "role" in body:
            state.auth.set_role(uid, body["role"])
            state.auth.audit("USER_ROLE", f"user_id={uid} role={body['role']}",
                             user_id=admin["id"], username=admin["username"], ip=_client_ip(request))
        if "password" in body or "new_pass" in body or "cred" in body:
            state.auth.update_password(uid, body.get("password") or body.get("new_pass") or body.get("cred"))
            state.auth.audit("USER_PASSWORD", f"user_id={uid}",
                             user_id=admin["id"], username=admin["username"], ip=_client_ip(request))
        return JSONResponse({"ok": True})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def _api_delete_user(request: Request) -> JSONResponse:
    admin = _require_admin(request)
    if not admin:
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = int(request.path_params["user_id"])
    try:
        target = state.auth.get_user_by_id(uid)
        state.auth.delete_user(uid)
        state.auth.audit("USER_DELETE", f"user_id={uid} username={target['username'] if target else '?'}",
                         user_id=admin["id"], username=admin["username"], ip=_client_ip(request))
        return JSONResponse({"ok": True})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ── Audit log endpoint ────────────────────────────────────────────────────────

async def _api_audit_log(request: Request) -> JSONResponse:
    if not _require_admin(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    limit  = int(request.query_params.get("limit", "200"))
    offset = int(request.query_params.get("offset", "0"))
    return JSONResponse({"log": state.auth.get_audit_log(limit=limit, offset=offset)})


# ── Session-guard for all /api/* except auth endpoints and /health ────────────

_OPEN_PATHS = {"/health", "/framer-metadata", "/framer-site", "/sitemap.xml", "/api/auth/login", "/"}

def _is_open(path: str) -> bool:
    if path in _OPEN_PATHS:
        return True
    if path.startswith("/mcp"):     # MCP uses its own Bearer token guard
        return True
    if path.startswith("/api/ica/"): # ICA framer endpoints are public (read-only)
        return True
    return False


class _SessionAuthMiddleware:
    """Reject requests to protected API endpoints when not authenticated."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/api/") and not _is_open(path):
                # Extract token from cookie or Authorization header
                headers_raw = {k.lower(): v for k, v in scope.get("headers", [])}
                auth_hdr = headers_raw.get(b"authorization", b"").decode("latin-1")
                token = auth_hdr.removeprefix("Bearer ").strip()
                if not token:
                    # Try cookie
                    cookie_hdr = headers_raw.get(b"cookie", b"").decode("latin-1")
                    for part in cookie_hdr.split(";"):
                        part = part.strip()
                        if part.startswith("session_token="):
                            token = part[len("session_token="):]
                            break
                user = state.auth.get_session_user(token) if token else None
                if not user:
                    async def _send_403(send):
                        body = b'{"error":"Not authenticated"}'
                        await send({"type": "http.response.start", "status": 401,
                                    "headers": [[b"content-type", b"application/json"]]})
                        await send({"type": "http.response.body", "body": body})
                    await _send_403(send)
                    return
        await self.app(scope, receive, send)


# Canonical public URL for this deployment (set via APP_URL env var; falls back
# to the Fly.io default).  Used by /health and /framer-metadata so the ICA
# framer connector can always discover the correct values without guessing.
_APP_URL = os.environ.get("APP_URL", "https://oraclereadinesssrc-dzxnqq.fly.dev").rstrip("/")


async def _health(request: Request) -> JSONResponse:
    status = state.db.cache_status()
    total  = sum(v.get("entry_count", 0) for v in status.values())
    return JSONResponse({
        "status":        "ok",
        "server":        "oracle-readiness-mcp",
        "total_features": total,
        # Included so ICA framer connector discovery has everything in one call
        "mcp_url":       f"{_APP_URL}/mcp",
        "project_link":  f"{_APP_URL}/framer-metadata",
    })


async def _framer_metadata(request: Request) -> JSONResponse:
    """GET /framer-metadata — machine-readable connector metadata (JSON).

    Kept for backward-compat. ICA's framer crawler fetches project_link as HTML;
    use /framer-site as the Connection URL instead.
    """
    return JSONResponse({
        "project_name":  "Oracle Readiness MCP",
        "project_id":    "oraclereadinesssrc-dzxnqq",
        "project_link":  f"{_APP_URL}/framer-site",
        "mcp_url":       f"{_APP_URL}/mcp",
        "base_url":      _APP_URL,
        "source_type":   "framer",
        "token_env_var": "READINESS_TOKEN",
        "ica_form": {
            "Connection URL / project_link": f"{_APP_URL}/framer-site",
            "MCP URL":                       f"{_APP_URL}/mcp",
            "Project name":                  "Oracle Readiness MCP",
            "Project ID":                    "oraclereadinesssrc-dzxnqq",
        },
        "ica_csv_endpoints": {
            "schema_changes":    f"{_APP_URL}/api/ica/schema-changes.json",
            "features":          f"{_APP_URL}/api/ica/features.csv",
            "actions":           f"{_APP_URL}/api/ica/actions.csv",
            "modules":           f"{_APP_URL}/api/ica/modules.csv",
            "releases":          f"{_APP_URL}/api/ica/releases.csv",
            "action_types":      f"{_APP_URL}/api/ica/action-types.csv",
            "derivation_methods":f"{_APP_URL}/api/ica/derivation-methods.csv",
        },
    })


async def _framer_site(request: Request) -> HTMLResponse:
    """GET /framer-site — HTML landing page that ICA's Framer crawler accepts.

    ICA's data-ingestion backend fetches the Connection URL as an HTML page and
    validates it has a crawlable HTML structure before starting ingestion.
    This endpoint returns a rich HTML page so the crawler accepts it, while
    embedding the MCP tool list so ICA's content extractor indexes real data.
    """
    releases = state.db.list_releases()
    families = state.db.list_product_families()
    total = sum(
        state.db.cache_status().get(p, {}).get("entry_count", 0)
        for p in state.db.cache_status()
    )
    release_list = ", ".join(releases) if releases else "26C"
    family_list  = ", ".join(families) if families else "HCM, ERP, SCM"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Oracle Readiness MCP</title>
  <meta name="description" content="Oracle Fusion Cloud Readiness MCP server — {total} features indexed across {family_list} for releases {release_list}">
</head>
<body>
  <header>
    <h1>Oracle Readiness MCP</h1>
    <p>Oracle Fusion Cloud Readiness intelligence server for IBM Consulting Advantage Context Studio.</p>
  </header>
  <main>
    <section id="overview">
      <h2>Overview</h2>
      <p>This MCP server provides structured access to Oracle Fusion Cloud release notes, feature flags, and readiness data for {release_list}.</p>
      <ul>
        <li>Total features indexed: {total}</li>
        <li>Product families: {family_list}</li>
        <li>Releases available: {release_list}</li>
        <li>MCP endpoint: {_APP_URL}/mcp</li>
      </ul>
    </section>
    <section id="tools">
      <h2>Available MCP Tools</h2>
      <ul>
        <li><strong>search_release_notes</strong> — Full-text search across Oracle Fusion release features</li>
        <li><strong>get_release_notes</strong> — All features for a release and module</li>
        <li><strong>get_feature_summary</strong> — Statistical summary for a module in a release</li>
        <li><strong>compare_releases</strong> — Diff two releases for a module</li>
        <li><strong>get_ai_features</strong> — AI and Agent features in a release</li>
        <li><strong>get_opt_in_features</strong> — Features requiring Opt-In to enable</li>
        <li><strong>get_setup_required_features</strong> — Features requiring Setup configuration</li>
        <li><strong>get_high_impact_features</strong> — Large-scale impact features</li>
        <li><strong>get_auto_enabled_features</strong> — Features auto-enabling in a future update</li>
        <li><strong>get_redwood_features</strong> — Oracle Redwood UX features</li>
        <li><strong>get_feature_detail</strong> — Full detail: Steps to Enable, Business Benefit, Key Resources, Tips</li>
        <li><strong>list_releases</strong> — List all indexed Oracle releases</li>
        <li><strong>list_modules</strong> — List modules for a release</li>
        <li><strong>get_ica_framer_csv</strong> — ICA Schema Builder Upload Sample Data CSV generator</li>
      </ul>
    </section>
    <section id="sitemap">
      <h2>Sitemap</h2>
      <ul>
        <li><a href="{_APP_URL}/framer-site">Home</a></li>
        <li><a href="{_APP_URL}/health">Health</a></li>
        <li><a href="{_APP_URL}/api/ica/schema-changes.json">ICA Schema Changes</a></li>
        <li><a href="{_APP_URL}/sitemap.xml">XML Sitemap</a></li>
      </ul>
    </section>
  </main>
</body>
</html>"""
    return HTMLResponse(content=html)


async def _sitemap_xml(request: Request) -> Response:
    """GET /sitemap.xml — XML sitemap so ICA's Framer crawler can discover pages."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{_APP_URL}/framer-site</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
  <url><loc>{_APP_URL}/health</loc><changefreq>always</changefreq><priority>0.8</priority></url>
  <url><loc>{_APP_URL}/api/ica/schema-changes.json</loc><changefreq>weekly</changefreq><priority>0.6</priority></url>
</urlset>"""
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# ICA framer CSV export endpoints  (/api/ica/*)
# ---------------------------------------------------------------------------

async def _ica_releases(request: Request) -> Response:
    """GET /api/ica/releases.csv — extend enum:oracleFusion26cReleaseCode (26D,27A,27B)."""
    return Response(
        content=_ica.csv_response(_ica.build_releases_csv()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=releases.csv"},
    )


async def _ica_action_types(request: Request) -> Response:
    """GET /api/ica/action-types.csv — extend enum:oracleFusion26cActionType."""
    return Response(
        content=_ica.csv_response(_ica.build_action_types_csv()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=action-types.csv"},
    )


async def _ica_modules(request: Request) -> Response:
    """GET /api/ica/modules.csv — extend enum:oracleFusion26cModule with missing modules."""
    # Grab the full list of live modules from the DB so extras discovered from
    # real data are included automatically on top of the hardcoded schema extras.
    releases = state.db.list_releases()
    live_modules: list[str] = []
    for rel in releases:
        live_modules.extend(state.db.list_modules(rel))
    unique_modules = sorted(set(live_modules))
    return Response(
        content=_ica.csv_response(_ica.build_modules_csv(unique_modules)),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=modules.csv"},
    )


async def _ica_derivation_methods(request: Request) -> Response:
    """GET /api/ica/derivation-methods.csv — add M017_MCP_FRAMER_INGESTION."""
    return Response(
        content=_ica.csv_response(_ica.build_derivation_methods_csv()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=derivation-methods.csv"},
    )


async def _ica_features(request: Request) -> Response:
    """GET /api/ica/features.csv — Feature nodes from live DB for ICA Upload Sample Data.

    Query params:
        release   — filter to a specific release (default: all)
        pillar    — filter to a product family (default: all)
    """
    release = request.query_params.get("release")
    pillar  = request.query_params.get("pillar")
    features = state.db.filter_entries(
        pillars=[pillar] if pillar else None,
        releases=[release] if release else None,
    )
    return Response(
        content=_ica.csv_response(_ica.build_features_csv(features)),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=features.csv"},
    )


async def _ica_actions(request: Request) -> Response:
    """GET /api/ica/actions.csv — Action nodes from scraped feature detail sections.

    Query params:
        release   — filter to a specific release (default: all)
        pillar    — filter to a product family (default: all)
    """
    release = request.query_params.get("release")
    pillar  = request.query_params.get("pillar")
    details = state.db.get_all_details_for_release(
        release or "", pillar
    ) if release else []

    # If no release given, pull from all known releases
    if not release:
        all_details: list[dict] = []
        for rel in state.db.list_releases():
            all_details.extend(state.db.get_all_details_for_release(rel, pillar))
        details = all_details

    return Response(
        content=_ica.csv_response(_ica.build_actions_csv(details)),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=actions.csv"},
    )


async def _ica_schema_changes(request: Request) -> JSONResponse:
    """GET /api/ica/schema-changes.json — machine-readable schema change manifest."""
    return JSONResponse(_ica.SCHEMA_CHANGES)


async def _api_status(request: Request) -> JSONResponse:
    """Full dashboard status: cache, settings (redacted), crawl log."""
    cache = state.db.cache_status()
    content = state.db.content_status()
    log = state.db.get_crawl_log(limit=10)
    return JSONResponse({
        "cache":     cache,
        "content":   content,
        "crawl_log": log,
        "settings":  state.settings.as_dict(redact_secrets=True),
        "refresh_running": state._refresh_task is not None and not state._refresh_task.done(),
    })


async def _api_get_settings(request: Request) -> JSONResponse:
    return JSONResponse(state.settings.as_dict(redact_secrets=True))


async def _api_save_settings(request: Request) -> JSONResponse:
    user = _require_auth(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    try:
        # Merge: ignore empty-string sentinel for secret fields (means "don't change")
        patch = {}
        for k, v in body.items():
            if v == "••••••••":        # redacted placeholder — skip
                continue
            patch[k] = v
        state.settings.update(patch)
        state.settings.save()
        if user:
            state.auth.audit("SETTINGS_SAVE", f"keys={list(patch.keys())}",
                             user_id=user["id"], username=user["username"], ip=_client_ip(request))
        return JSONResponse({"ok": True, "settings": state.settings.as_dict(redact_secrets=True)})
    except KeyError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def _api_trigger_refresh(request: Request) -> JSONResponse:
    """POST /api/refresh  —  body: {"products": ["hcm"]} or {} for all."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    products = body.get("products") or None
    if products:
        bad = [p for p in products if p not in PRODUCT_NAMES]
        if bad:
            return JSONResponse({"ok": False, "error": f"Unknown products: {bad}"}, status_code=400)
    results = await state.refresh_now(products)
    return JSONResponse({"ok": True, "results": results})


async def _api_test_github(request: Request) -> JSONResponse:
    """POST /api/test-github  —  validates the stored GitHub token + repo."""
    token = state.settings.github_token
    repo  = state.settings.github_repo
    if not token:
        return JSONResponse({"ok": False, "error": "No GitHub token configured"})
    if not repo:
        return JSONResponse({"ok": False, "error": "No GitHub repo configured"})
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return JSONResponse({"ok": True, "repo": data.get("full_name"), "private": data.get("private"),
                                 "default_branch": data.get("default_branch")})
        return JSONResponse({"ok": False, "error": f"GitHub returned {resp.status_code}: {resp.text[:200]}"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Release drill-down API endpoints
# ---------------------------------------------------------------------------

async def _api_releases(request: Request) -> JSONResponse:
    """GET /api/releases — all releases with per-pillar feature counts and stats."""
    target = state.settings.get("target_releases") or []
    rows = state.db._execute("""
        SELECT release, product_family,
               COUNT(*) as features,
               COUNT(DISTINCT module) as modules,
               SUM(CASE WHEN impact LIKE '%Large%' THEN 1 ELSE 0 END) as large_scale,
               SUM(CASE WHEN setup_required=1 THEN 1 ELSE 0 END) as setup_required,
               SUM(CASE WHEN opt_in_required=1 THEN 1 ELSE 0 END) as opt_in,
               SUM(CASE WHEN is_ai=1 THEN 1 ELSE 0 END) as ai,
               SUM(CASE WHEN is_redwood=1 THEN 1 ELSE 0 END) as redwood
        FROM features WHERE release != 'Unknown'
        GROUP BY release, product_family
        ORDER BY release DESC, product_family
    """).fetchall()

    # Group by release
    release_map: dict = {}
    for r in rows:
        rel = r["release"]
        if rel not in release_map:
            release_map[rel] = {
                "release": rel,
                "is_target": rel in target if target else True,
                "pillars": {},
                "totals": {"features": 0, "modules": 0, "large_scale": 0,
                           "setup_required": 0, "opt_in": 0, "ai": 0, "redwood": 0},
            }
        pf = r["product_family"]
        release_map[rel]["pillars"][pf] = {
            "features": r["features"], "modules": r["modules"],
            "large_scale": r["large_scale"], "setup_required": r["setup_required"],
            "opt_in": r["opt_in"], "ai": r["ai"], "redwood": r["redwood"],
        }
        for k in ("features","modules","large_scale","setup_required","opt_in","ai","redwood"):
            release_map[rel]["totals"][k] = release_map[rel]["totals"].get(k, 0) + (r[k] or 0)

    return JSONResponse({
        "releases": list(release_map.values()),
        "target_releases": target,
        "all_releases": list(release_map.keys()),
    })


async def _api_release_pillar(request: Request) -> JSONResponse:
    """GET /api/releases/{release}/{pillar} — modules breakdown for a release+pillar."""
    release = request.path_params["release"].upper()
    pillar  = request.path_params["pillar"].lower()

    rows = state.db._execute("""
        SELECT module,
               COUNT(*) as features,
               SUM(CASE WHEN impact LIKE '%Large%' THEN 1 ELSE 0 END) as large_scale,
               SUM(CASE WHEN impact LIKE '%Small%' THEN 1 ELSE 0 END) as small_scale,
               SUM(CASE WHEN setup_required=1 THEN 1 ELSE 0 END) as setup_required,
               SUM(CASE WHEN opt_in_required=1 THEN 1 ELSE 0 END) as opt_in,
               SUM(CASE WHEN is_ai=1 THEN 1 ELSE 0 END) as ai,
               SUM(CASE WHEN is_redwood=1 THEN 1 ELSE 0 END) as redwood,
               SUM(CASE WHEN auto_enabled_in IS NOT NULL THEN 1 ELSE 0 END) as auto_enabled
        FROM features
        WHERE UPPER(release)=? AND product_family=?
        GROUP BY module
        ORDER BY features DESC, module
    """, (release, pillar)).fetchall()

    if not rows:
        return JSONResponse({"release": release, "pillar": pillar, "modules": [],
                             "error": f"No data for {pillar}/{release}"})

    return JSONResponse({
        "release": release,
        "pillar":  pillar,
        "label":   PRODUCT_LABELS.get(pillar, pillar),
        "modules": [dict(r) for r in rows],
        "totals": {
            "features":      sum(r["features"] for r in rows),
            "large_scale":   sum(r["large_scale"] or 0 for r in rows),
            "setup_required":sum(r["setup_required"] or 0 for r in rows),
            "opt_in":        sum(r["opt_in"] or 0 for r in rows),
            "ai":            sum(r["ai"] or 0 for r in rows),
        },
    })


async def _api_release_module_features(request: Request) -> JSONResponse:
    """GET /api/releases/{release}/{pillar}/{module} — all features for a module."""
    release = request.path_params["release"].upper()
    pillar  = request.path_params["pillar"].lower()
    module  = request.path_params["module"]

    rows = state.db._execute("""
        SELECT feature_name, description, impact, enablement,
               auto_enabled_in, is_redwood, is_ai, ai_type,
               setup_required, opt_in_required, html_url, pdf_url
        FROM features
        WHERE UPPER(release)=? AND product_family=? AND module=?
        ORDER BY feature_name
    """, (release, pillar, module)).fetchall()

    return JSONResponse({
        "release": release, "pillar": pillar, "module": module,
        "features": [dict(r) for r in rows],
        "total": len(rows),
    })


async def _api_save_target_releases(request: Request) -> JSONResponse:
    """POST /api/releases/targets — save the target releases list."""
    try:
        body = await request.json()
        releases = body.get("releases", [])
        state.settings.set("target_releases", releases)
        state.settings.save()
        return JSONResponse({"ok": True, "target_releases": releases})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


async def _api_push_github(request: Request) -> JSONResponse:
    """POST /api/push-github — push report using stored settings."""
    try:
        result = await _github_push(
            token=state.settings.github_token,
            repo=state.settings.github_repo,
            branch=state.settings.github_branch,
            file_path=state.settings.github_file_path,
            pillars=state.settings.active_pillars,
        )
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


async def _api_test_oracle(request: Request) -> JSONResponse:
    """GET /api/test-oracle  —  checks reachability of Oracle readiness pages."""
    results = {}
    async with httpx.AsyncClient() as client:
        for product, url in list(PRODUCTS.items())[:2]:   # sample 2 to keep it fast
            try:
                r = await client.head(url, headers={"User-Agent": "oracle-readiness-mcp/2.0"},
                                      follow_redirects=True, timeout=10)
                results[product] = {"ok": r.status_code < 400, "status": r.status_code, "url": url}
            except Exception as e:
                results[product] = {"ok": False, "error": str(e), "url": url}
    return JSONResponse({"ok": all(v["ok"] for v in results.values()), "products": results})


async def _ui(request: Request) -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


# ---------------------------------------------------------------------------
# Starlette app assembly
# ---------------------------------------------------------------------------

class _McpAcceptMiddleware:
    """ASGI middleware that injects the Accept header required by FastMCP's
    Streamable HTTP transport on /mcp requests that omit it.

    The MCP spec requires clients to send:
        Accept: application/json, text/event-stream
    Some MCP clients do not send this header, causing FastMCP to reject
    the request with -32600 Not Acceptable.  This middleware injects
    the header on any /mcp request that is missing it.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            headers = list(scope.get("headers", []))
            has_accept = any(k.lower() == b"accept" for k, v in headers)
            if not has_accept:
                headers.append((b"accept", b"application/json, text/event-stream"))
                scope = dict(scope, headers=headers)
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _McpBearerMiddleware:
    """ASGI middleware that enforces Bearer token auth on /mcp requests.

    When state.settings.mcp_token is non-empty, every request whose path
    starts with /mcp must carry a matching Authorization: Bearer <token>
    header.  All other paths (health, UI, REST API) are always allowed through.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/mcp"):
                required = state.settings.mcp_token
                if required:
                    auth = ""
                    for name, value in scope.get("headers", []):
                        if name.lower() == b"authorization":
                            auth = value.decode("latin-1")
                            break
                    provided = auth.removeprefix("Bearer ").strip()
                    if provided != required:
                        async def _send_401(send):
                            await send({
                                "type": "http.response.start",
                                "status": 401,
                                "headers": [
                                    [b"content-type", b"application/json"],
                                    [b"www-authenticate", b'Bearer realm="oracle-readiness-mcp"'],
                                ],
                            })
                            await send({
                                "type": "http.response.body",
                                "body": b'{"error":"Unauthorized: valid Bearer token required"}',
                            })
                        await _send_401(send)
                        return
        await self.app(scope, receive, send)


def _build_starlette_app() -> Starlette:
    """Combine the web UI, REST API, health check, and MCP transport.

    FastMCP's streamable_http_app() registers a route at /mcp and needs its
    own session_manager.run() lifespan to initialise the task group.
    We merge the route list AND combine the two lifespans so both background
    refresh AND MCP session manager start correctly.
    """
    mcp_app = mcp.streamable_http_app()
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)

    @asynccontextmanager
    async def combined_lifespan(app: Starlette):
        # 1) start background refresh loop (our lifespan)
        if AUTOSTART:
            await state.start_background_refresh()
        else:
            logger.info("Background refresh disabled (READINESS_AUTOSTART_REFRESH=0)")
        # 2) start MCP session manager task group (FastMCP lifespan)
        async with mcp.session_manager.run():
            yield

    routes = [
        Route("/",                   _ui,                   methods=["GET"]),
        Route("/health",             _health,               methods=["GET"]),
        Route("/framer-metadata",    _framer_metadata,      methods=["GET"]),
        Route("/framer-site",        _framer_site,          methods=["GET"]),
        Route("/sitemap.xml",        _sitemap_xml,          methods=["GET"]),
        # Auth
        Route("/api/auth/login",     _api_login,            methods=["POST"]),
        Route("/api/auth/logout",    _api_logout,           methods=["POST"]),
        Route("/api/auth/me",        _api_session_check,    methods=["GET"]),
        # User management (admin only)
        Route("/api/users",          _api_list_users,       methods=["GET"]),
        Route("/api/users",          _api_create_user,      methods=["POST"]),
        Route("/api/users/{user_id}", _api_update_user,     methods=["PATCH"]),
        Route("/api/users/{user_id}", _api_delete_user,     methods=["DELETE"]),
        # Audit log
        Route("/api/audit",          _api_audit_log,        methods=["GET"]),
        # Status / settings
        Route("/api/status",         _api_status,           methods=["GET"]),
        Route("/api/settings",       _api_get_settings,     methods=["GET"]),
        Route("/api/settings",       _api_save_settings,    methods=["POST"]),
        Route("/api/refresh",               _api_trigger_refresh,          methods=["POST"]),
        Route("/api/releases",              _api_releases,                 methods=["GET"]),
        Route("/api/releases/targets",      _api_save_target_releases,     methods=["POST"]),
        Route("/api/releases/{release}/{pillar}/{module:path}", _api_release_module_features, methods=["GET"]),
        Route("/api/releases/{release}/{pillar}", _api_release_pillar,     methods=["GET"]),
        Route("/api/test-github",           _api_test_github,              methods=["POST"]),
        Route("/api/push-github",           _api_push_github,              methods=["POST"]),
        Route("/api/test-oracle",           _api_test_oracle,              methods=["GET"]),
        # ICA framer CSV export (public, no auth required)
        Route("/api/ica/releases.csv",          _ica_releases,           methods=["GET"]),
        Route("/api/ica/action-types.csv",      _ica_action_types,       methods=["GET"]),
        Route("/api/ica/modules.csv",           _ica_modules,            methods=["GET"]),
        Route("/api/ica/derivation-methods.csv", _ica_derivation_methods, methods=["GET"]),
        Route("/api/ica/features.csv",          _ica_features,           methods=["GET"]),
        Route("/api/ica/actions.csv",           _ica_actions,            methods=["GET"]),
        Route("/api/ica/schema-changes.json",   _ica_schema_changes,     methods=["GET"]),
        *mcp_app.routes,
    ]
    app = Starlette(routes=routes, lifespan=combined_lifespan)
    # Stack (inner → outer): MCP Bearer auth → session auth → Accept injector
    return _McpAcceptMiddleware(_SessionAuthMiddleware(_McpBearerMiddleware(app)))

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if "--http" in sys.argv:
        import uvicorn
        app = _build_starlette_app()
        uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")
    else:
        # stdio mode: attach app_lifespan so background refresh starts
        mcp.settings.lifespan = app_lifespan  # type: ignore[assignment]
        mcp.run()


if __name__ == "__main__":
    main()
