"""
db.py
-----
SQLite persistence layer for Oracle readiness feature data.

Schema mirrors the TypeScript oracle-readiness-mcp model (features table with
full rich schema) plus a crawl_log table and a content_cache table for
downloaded document text.

All operations are synchronous SQLite via stdlib sqlite3 wrapped in a thin
async-friendly class using asyncio.Lock to prevent concurrent writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from oracle_scraper import Feature, PRODUCTS, PRODUCT_LABELS

logger = logging.getLogger("oracle_readiness_mcp.db")


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS features (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    release          TEXT NOT NULL,
    product_family   TEXT NOT NULL,
    product          TEXT NOT NULL,
    module           TEXT NOT NULL,
    feature_name     TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    impact           TEXT,
    enablement       TEXT,
    auto_enabled_in  TEXT,
    is_redwood       INTEGER NOT NULL DEFAULT 0,
    is_ai            INTEGER NOT NULL DEFAULT 0,
    ai_type          TEXT,
    setup_required   INTEGER NOT NULL DEFAULT 0,
    opt_in_required  INTEGER NOT NULL DEFAULT 0,
    html_url         TEXT,
    pdf_url          TEXT,
    source_url       TEXT NOT NULL DEFAULT '',
    retrieved_at     TEXT NOT NULL,
    UNIQUE(release, product_family, module, feature_name)
);
CREATE INDEX IF NOT EXISTS idx_f_release  ON features(release);
CREATE INDEX IF NOT EXISTS idx_f_module   ON features(module);
CREATE INDEX IF NOT EXISTS idx_f_family   ON features(product_family);
CREATE TABLE IF NOT EXISTS crawl_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url  TEXT NOT NULL,
    product     TEXT NOT NULL,
    release     TEXT,
    crawled_at  TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS content_cache (
    url          TEXT PRIMARY KEY,
    doc_type     TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_chars INTEGER NOT NULL DEFAULT 0,
    truncated    INTEGER NOT NULL DEFAULT 0,
    fetched_at   REAL NOT NULL
);
-- Per-feature deep detail scraped from individual feature .htm pages.
CREATE TABLE IF NOT EXISTS feature_details (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_page_url    TEXT NOT NULL UNIQUE,
    feature_name        TEXT NOT NULL DEFAULT '',
    release             TEXT NOT NULL DEFAULT '',
    product_family      TEXT NOT NULL DEFAULT '',
    module              TEXT NOT NULL DEFAULT '',
    description_full    TEXT NOT NULL DEFAULT '',
    business_benefit    TEXT,
    steps_to_enable     TEXT,
    tips_considerations TEXT,
    access_requirements TEXT,
    key_resources       TEXT,
    other_sections      TEXT,
    optional_uptake     INTEGER NOT NULL DEFAULT 0,
    fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fd_release ON feature_details(release);
CREATE INDEX IF NOT EXISTS idx_fd_family  ON feature_details(product_family);
CREATE INDEX IF NOT EXISTS idx_fd_module  ON feature_details(module);
"""

# Safe migration: add feature_detail_url to features table if not already present
_MIGRATION_DDL = [
    "ALTER TABLE features ADD COLUMN feature_detail_url TEXT",
]

# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class ReadinessDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock   = asyncio.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn   = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()
        # Run safe migrations (ALTER TABLE ... ADD COLUMN — idempotent via try/except)
        for stmt in _MIGRATION_DDL:
            try:
                self._conn.execute(stmt)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def _executemany(self, sql: str, params_list) -> None:
        self._conn.executemany(sql, params_list)

    def _commit(self) -> None:
        self._conn.commit()

    # -----------------------------------------------------------------------
    # Feature upsert
    # -----------------------------------------------------------------------

    async def upsert_features(self, features: list[Feature]) -> int:
        async with self._lock:
            sql = """
                INSERT OR REPLACE INTO features
                  (release, product_family, product, module, feature_name,
                   description, impact, enablement, auto_enabled_in,
                   is_redwood, is_ai, ai_type, setup_required, opt_in_required,
                   html_url, pdf_url, source_url, retrieved_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """
            rows = [
                (f.release, f.product_family, f.product, f.module, f.feature_name,
                 f.description, f.impact, f.enablement, f.auto_enabled_in,
                 int(f.is_redwood), int(f.is_ai), f.ai_type,
                 int(f.setup_required), int(f.opt_in_required),
                 f.html_url, f.pdf_url, f.source_url, f.retrieved_at)
                for f in features
            ]
            self._executemany(sql, rows)
            self._commit()
            return len(rows)

    # -----------------------------------------------------------------------
    # Crawl log
    # -----------------------------------------------------------------------

    async def log_crawl(self, product: str, source_url: str, row_count: int,
                        release: Optional[str] = None, ok: bool = True,
                        error: Optional[str] = None) -> None:
        async with self._lock:
            self._execute(
                "INSERT INTO crawl_log (source_url, product, release, crawled_at, row_count, ok, error) "
                "VALUES (?,?,?,?,?,?,?)",
                (source_url, product, release,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 row_count, int(ok), error),
            )
            self._commit()

    def get_crawl_log(self, limit: int = 50) -> list[dict]:
        rows = self._execute(
            "SELECT source_url, product, release, crawled_at, row_count, ok, error "
            "FROM crawl_log ORDER BY crawled_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Content cache
    # -----------------------------------------------------------------------

    async def cache_content(self, doc: dict) -> None:
        async with self._lock:
            self._execute(
                "INSERT OR REPLACE INTO content_cache "
                "(url, doc_type, content, content_chars, truncated, fetched_at) "
                "VALUES (?,?,?,?,?,?)",
                (doc["url"], doc["doc_type"], doc["content"],
                 doc["content_chars"], int(doc["truncated"]), doc["fetched_at"]),
            )
            self._commit()

    def get_content(self, url: str) -> Optional[dict]:
        row = self._execute(
            "SELECT * FROM content_cache WHERE url=?", (url,)
        ).fetchone()
        return dict(row) if row else None

    def content_status(self) -> dict:
        row = self._execute(
            "SELECT COUNT(*) as cnt, SUM(content_chars) as total_chars FROM content_cache"
        ).fetchone()
        return {
            "documents_cached": row["cnt"] or 0,
            "total_chars_cached": row["total_chars"] or 0,
        }

    # -----------------------------------------------------------------------
    # Feature queries
    # -----------------------------------------------------------------------

    def _row_to_feature(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["is_redwood"]      = bool(d.get("is_redwood"))
        d["is_ai"]           = bool(d.get("is_ai"))
        d["setup_required"]  = bool(d.get("setup_required"))
        d["opt_in_required"] = bool(d.get("opt_in_required"))
        return d

    def list_releases(self) -> list[str]:
        rows = self._execute(
            "SELECT DISTINCT release FROM features "
            "WHERE release != 'Unknown' ORDER BY release DESC"
        ).fetchall()
        return [r["release"] for r in rows]

    def list_product_families(self, release: Optional[str] = None) -> list[str]:
        if release:
            rows = self._execute(
                "SELECT DISTINCT product_family FROM features WHERE release=? ORDER BY product_family",
                (release,)
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT DISTINCT product_family FROM features ORDER BY product_family"
            ).fetchall()
        return [r["product_family"] for r in rows]

    def list_modules(self, release: str, product_family: Optional[str] = None) -> list[str]:
        if product_family:
            rows = self._execute(
                "SELECT DISTINCT module FROM features WHERE release=? AND product_family=? ORDER BY module",
                (release, product_family)
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT DISTINCT module FROM features WHERE release=? ORDER BY module",
                (release,)
            ).fetchall()
        return [r["module"] for r in rows]

    def get_entries_for_product(self, product: str,
                                 release: Optional[str] = None,
                                 limit: int = 500) -> list[dict]:
        params: list = [product]
        where = "product_family=?"
        if release:
            where += " AND UPPER(release)=?"
            params.append(release.upper())
        rows = self._execute(
            f"SELECT * FROM features WHERE {where} "
            f"ORDER BY release DESC, module, feature_name LIMIT ?",
            params + [limit]
        ).fetchall()
        return [self._row_to_feature(r) for r in rows]

    def search_features(self, query: str,
                        release: Optional[str] = None,
                        module: Optional[str] = None,
                        product_family: Optional[str] = None,
                        limit: int = 100) -> list[dict]:
        terms  = query.lower().split()
        conds  = []
        params: list = []

        if release:
            conds.append("UPPER(release)=?"); params.append(release.upper())
        if module:
            conds.append("LOWER(module) LIKE ?"); params.append(f"%{module.lower()}%")
        if product_family:
            conds.append("LOWER(product_family) LIKE ?"); params.append(f"%{product_family.lower()}%")
        for t in terms:
            conds.append("(LOWER(feature_name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(module) LIKE ?)")
            params += [f"%{t}%", f"%{t}%", f"%{t}%"]

        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        rows  = self._execute(
            f"SELECT * FROM features {where} "
            f"ORDER BY release DESC, module, feature_name LIMIT ?",
            params + [limit]
        ).fetchall()
        return [self._row_to_feature(r) for r in rows]

    def get_features_by_module(self, release: str, module: str) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM features WHERE UPPER(release)=? AND LOWER(module) LIKE ? "
            "ORDER BY feature_name",
            (release.upper(), f"%{module.lower()}%")
        ).fetchall()
        return [self._row_to_feature(r) for r in rows]

    def get_filtered_features(self, release: str,
                               filter_type: str,
                               module: Optional[str] = None) -> list[dict]:
        conds  = ["UPPER(release)=?"]
        params: list = [release.upper()]
        if module:
            conds.append("LOWER(module) LIKE ?"); params.append(f"%{module.lower()}%")
        match filter_type:
            case "setup_required":  conds.append("setup_required=1")
            case "opt_in":          conds.append("opt_in_required=1")
            case "large_scale":     conds.append("LOWER(impact) LIKE '%large%'")
            case "auto_enabled":    conds.append("auto_enabled_in IS NOT NULL")
            case "ai":              conds.append("is_ai=1")
            case "redwood":         conds.append("is_redwood=1")
        rows = self._execute(
            f"SELECT * FROM features WHERE {' AND '.join(conds)} "
            f"ORDER BY module, feature_name",
            params
        ).fetchall()
        return [self._row_to_feature(r) for r in rows]

    def filter_entries(self,
                       pillars: Optional[list[str]] = None,
                       modules: Optional[list[str]] = None,
                       releases: Optional[list[str]] = None,
                       query: Optional[str] = None) -> list[dict]:
        conds: list[str] = []
        params: list     = []

        if pillars:
            placeholders = ",".join("?" * len(pillars))
            conds.append(f"product_family IN ({placeholders})")
            params.extend(pillars)
        if modules:
            mod_conds = " OR ".join("LOWER(module) LIKE ?" for _ in modules)
            conds.append(f"({mod_conds})")
            params.extend(f"%{m.lower()}%" for m in modules)
        if releases:
            placeholders = ",".join("?" * len(releases))
            conds.append(f"UPPER(release) IN ({placeholders})")
            params.extend(r.upper() for r in releases)
        if query:
            conds.append("(LOWER(feature_name) LIKE ? OR LOWER(description) LIKE ?)")
            params += [f"%{query.lower()}%", f"%{query.lower()}%"]

        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        rows  = self._execute(
            f"SELECT * FROM features {where} "
            f"ORDER BY product_family, release, module, feature_name",
            params
        ).fetchall()
        return [self._row_to_feature(r) for r in rows]

    def cache_status(self) -> dict:
        """Per-product entry counts + last crawl timestamps."""
        out = {}
        for p in PRODUCTS:
            row = self._execute(
                "SELECT COUNT(*) as cnt FROM features WHERE product_family=?", (p,)
            ).fetchone()
            log = self._execute(
                "SELECT crawled_at, ok, error, row_count FROM crawl_log "
                "WHERE product=? ORDER BY crawled_at DESC LIMIT 1", (p,)
            ).fetchone()
            out[p] = {
                "label":        PRODUCT_LABELS.get(p, p),
                "entry_count":  row["cnt"] if row else 0,
                "last_refresh": log["crawled_at"] if log else None,
                "last_ok":      bool(log["ok"]) if log else None,
                "last_error":   log["error"] if log else None,
            }
        return out

    # -----------------------------------------------------------------------
    # Feature detail upsert + queries
    # -----------------------------------------------------------------------

    async def upsert_feature_detail(self, detail: dict) -> None:
        """Insert or replace a feature detail row.

        Expected keys: feature_page_url, feature_name, release, product_family,
        module, description_full, business_benefit, steps_to_enable,
        tips_considerations, access_requirements, key_resources, other_sections,
        optional_uptake, fetched_at.
        """
        async with self._lock:
            self._execute(
                """
                INSERT OR REPLACE INTO feature_details
                  (feature_page_url, feature_name, release, product_family, module,
                   description_full, business_benefit, steps_to_enable,
                   tips_considerations, access_requirements, key_resources,
                   other_sections, optional_uptake, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    detail["feature_page_url"],
                    detail.get("feature_name", ""),
                    detail.get("release", ""),
                    detail.get("product_family", ""),
                    detail.get("module", ""),
                    detail.get("description_full", ""),
                    detail.get("business_benefit"),
                    detail.get("steps_to_enable"),
                    detail.get("tips_considerations"),
                    detail.get("access_requirements"),
                    detail.get("key_resources"),
                    detail.get("other_sections"),
                    int(detail.get("optional_uptake", False)),
                    detail.get("fetched_at", ""),
                ),
            )
            self._conn.commit()
            # Also update the feature_detail_url on the matching features row
            self._execute(
                """
                UPDATE features SET feature_detail_url = ?
                WHERE LOWER(feature_name) = LOWER(?)
                  AND UPPER(release) = UPPER(?)
                  AND LOWER(product_family) = LOWER(?)
                """,
                (
                    detail["feature_page_url"],
                    detail.get("feature_name", ""),
                    detail.get("release", ""),
                    detail.get("product_family", ""),
                ),
            )
            self._conn.commit()

    def get_feature_detail(self, feature_page_url: str) -> Optional[dict]:
        """Fetch full detail for one feature by its page URL."""
        row = self._execute(
            "SELECT * FROM feature_details WHERE feature_page_url=?",
            (feature_page_url,),
        ).fetchone()
        return dict(row) if row else None

    def get_feature_detail_by_name(
        self, feature_name: str, release: str, product_family: str
    ) -> Optional[dict]:
        """Fetch detail by name + release + pillar (case-insensitive)."""
        row = self._execute(
            """
            SELECT * FROM feature_details
            WHERE LOWER(feature_name) = LOWER(?)
              AND UPPER(release) = UPPER(?)
              AND LOWER(product_family) = LOWER(?)
            LIMIT 1
            """,
            (feature_name, release, product_family),
        ).fetchone()
        return dict(row) if row else None

    def list_features_with_steps(
        self,
        release: Optional[str] = None,
        product_family: Optional[str] = None,
        module: Optional[str] = None,
    ) -> list[dict]:
        """Return summary rows for features that have steps-to-enable content."""
        conds = ["steps_to_enable IS NOT NULL AND steps_to_enable != ''"]
        params: list = []
        if release:
            conds.append("UPPER(release) = UPPER(?)")
            params.append(release)
        if product_family:
            conds.append("LOWER(product_family) = LOWER(?)")
            params.append(product_family)
        if module:
            conds.append("LOWER(module) LIKE ?")
            params.append(f"%{module.lower()}%")
        rows = self._execute(
            f"SELECT feature_page_url, feature_name, release, product_family, module, "
            f"       optional_uptake, fetched_at "
            f"FROM feature_details WHERE {' AND '.join(conds)} "
            f"ORDER BY release DESC, product_family, module, feature_name",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_features_with_tips(
        self,
        release: Optional[str] = None,
        product_family: Optional[str] = None,
        module: Optional[str] = None,
    ) -> list[dict]:
        """Return summary rows for features that have tips-and-considerations content."""
        conds = ["tips_considerations IS NOT NULL AND tips_considerations != ''"]
        params: list = []
        if release:
            conds.append("UPPER(release) = UPPER(?)")
            params.append(release)
        if product_family:
            conds.append("LOWER(product_family) = LOWER(?)")
            params.append(product_family)
        if module:
            conds.append("LOWER(module) LIKE ?")
            params.append(f"%{module.lower()}%")
        rows = self._execute(
            f"SELECT feature_page_url, feature_name, release, product_family, module, "
            f"       optional_uptake, fetched_at "
            f"FROM feature_details WHERE {' AND '.join(conds)} "
            f"ORDER BY release DESC, product_family, module, feature_name",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def search_feature_details(
        self, query: str,
        release: Optional[str] = None,
        product_family: Optional[str] = None,
    ) -> list[dict]:
        """Full-text search across all detail sections."""
        terms = query.lower().split()
        conds: list[str] = []
        params: list = []
        if release:
            conds.append("UPPER(release) = UPPER(?)")
            params.append(release)
        if product_family:
            conds.append("LOWER(product_family) = LOWER(?)")
            params.append(product_family)
        for term in terms:
            conds.append(
                "(LOWER(feature_name) LIKE ? OR LOWER(description_full) LIKE ?"
                " OR LOWER(COALESCE(steps_to_enable,'')) LIKE ?"
                " OR LOWER(COALESCE(tips_considerations,'')) LIKE ?"
                " OR LOWER(COALESCE(business_benefit,'')) LIKE ?"
                " OR LOWER(COALESCE(access_requirements,'')) LIKE ?)"
            )
            params += [f"%{term}%"] * 6
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        rows = self._execute(
            f"SELECT * FROM feature_details {where} "
            f"ORDER BY release DESC, product_family, module, feature_name LIMIT 100",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def detail_status(self) -> dict:
        """Count of scraped feature detail pages by release+pillar."""
        rows = self._execute(
            "SELECT release, product_family, COUNT(*) as cnt "
            "FROM feature_details "
            "GROUP BY release, product_family "
            "ORDER BY release DESC, product_family"
        ).fetchall()
        return {f"{r['release']}/{r['product_family']}": r["cnt"] for r in rows}

    def get_details_for_module(
        self, release: str, product_family: str, module: str
    ) -> list[dict]:
        """All feature details for a specific release/pillar/module."""
        rows = self._execute(
            """
            SELECT * FROM feature_details
            WHERE UPPER(release) = UPPER(?)
              AND LOWER(product_family) = LOWER(?)
              AND LOWER(module) LIKE ?
            ORDER BY feature_name
            """,
            (release, product_family, f"%{module.lower()}%"),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_details_for_release(
        self, release: str, product_family: Optional[str] = None
    ) -> list[dict]:
        """All feature details for a release, optionally filtered by pillar."""
        conds = ["UPPER(release) = UPPER(?)"]
        params: list = [release]
        if product_family:
            conds.append("LOWER(product_family) = LOWER(?)")
            params.append(product_family)
        rows = self._execute(
            f"SELECT * FROM feature_details WHERE {' AND '.join(conds)} "
            f"ORDER BY product_family, module, feature_name",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
