# Oracle Readiness MCP — Master Knowledge Base

> **Last updated:** 2026-07-25  
> **Status:** Definitive post-6-hour-session capture. All hard-won lessons recorded.  
> **Purpose:** Single source of truth so no problem is solved twice.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Architecture Deep-Dive](#3-architecture-deep-dive)
4. [Deployment — Fly.io](#4-deployment--flyio)
5. [ICA Context Studio Integration](#5-ica-context-studio-integration)
6. [ICA Framer Connector — The Hard Part](#6-ica-framer-connector--the-hard-part)
7. [ICA Schema Builder — CSV Upload Flow](#7-ica-schema-builder--csv-upload-flow)
8. [Data Pipeline — How Features Get In](#8-data-pipeline--how-features-get-in)
9. [The `feature_details` vs `features` Hierarchy Problem](#9-the-feature_details-vs-features-hierarchy-problem)
10. [Flag Columns — The Backfill Problem](#10-flag-columns--the-backfill-problem)
11. [MCP Tools Reference](#11-mcp-tools-reference)
12. [Authentication Layers](#12-authentication-layers)
13. [Common Errors and Fixes — The War Stories](#13-common-errors-and-fixes--the-war-stories)
14. [Environment Variables](#14-environment-variables)
15. [Bob Config — Connecting Bob to the Live Server](#15-bob-config--connecting-bob-to-the-live-server)
16. [Quick-Reference Commands](#16-quick-reference-commands)
17. [What Still Needs Doing (Remaining Work)](#17-what-still-needs-doing-remaining-work)
18. [Appendix A — Git Commit History Summary](#appendix-a--git-commit-history-summary)
19. [Appendix B — ICA CSV Column Order](#appendix-b--ica-csv-column-order)
20. [Appendix C — Database Schema](#appendix-c--database-schema)
21. [Appendix D — The XLSX Trap (Feature_Summary.json)](#appendix-d--the-xlsx-trap-feature_summaryjson)

---

## 1. Project Overview

A **Python FastMCP server** deployed to Fly.io that:
1. Scrapes Oracle Cloud Applications Readiness pages for What's New features
2. Exposes those features as MCP tools to AI agents (Bob, Claude, etc.)
3. Serves the same data as ICA-compatible CSV files so IBM Context Studio can ingest it into a knowledge graph (the "26c Complete Ontology")

**Live URL:** `https://oraclereadinesssrc-dzxnqq.fly.dev`  
**Fly.io app name:** `oraclereadinesssrc-dzxnqq`  
**Region:** `lhr` (London)  
**ICA Context:** `26c Complete Ontology` (`ctx_9baeb72e480b`)  
**ICA Team:** `MattStocker` (`teamId=69aaae8a8482bc71f1c4af52`)

---

## 2. Repository Layout

```
Playground/
├── server.py              ← Main FastMCP + Starlette app (ALL routes live here)
├── db.py                  ← SQLite layer (ReadinessDB class)
├── ica.py                 ← ICA CSV builder functions (ica.py has NO web routes)
├── oracle_scraper.py      ← HTML scraper for Oracle readiness pages
├── scheduler.py           ← Background scheduler (triggers AppState._do_refresh)
├── auth.py                ← Session auth (AuthDB class, users/sessions tables)
├── settings.py            ← App settings store (Settings class)
├── Dockerfile             ← Multi-stage Python build
├── fly.toml               ← Fly.io config (app name, volume mounts, env vars)
├── requirements.txt       ← Python deps
├── docker-compose.yml     ← Local dev (maps /data to ./data)
├── static/index.html      ← Web UI
├── .env.example           ← Template for local .env
├── CONNECTOR.md           ← Quick-reference for ICA form values
├── KNOWLEDGEBASE.md       ← This file
│
├── oracle-readiness-mcp/  ← Original TypeScript MCP (reference only, not deployed)
│   └── mcp/src/
│       ├── index.ts       ← TypeScript MCP tools (reference for feature schema)
│       ├── db.ts
│       ├── crawler.ts
│       └── parser.ts
│
├── readiness_remote.db    ← Downloaded copy of Fly.io DB (should NOT be committed)
│
├── analyse_26c.py         ← One-off analysis helper
├── backfill_run.py        ← Migration/backfill runner for Fly SSH
├── backfill_xlsx_flags.py ← XLSX→feature_details flag backfill attempt
├── check_db.py            ← DB diagnostic script
├── check_names.py         ← Table comparison diagnostic
└── ingest_run.py          ← XLSX ingest runner for Fly SSH

Fly.io volume /data/:
├── readiness.db           ← Live SQLite database
├── Feature_Summary.json   ← Uploaded XLSX dump (all Feature values = #UNCALCULATED!)
└── reports/               ← Generated report files
```

---

## 3. Architecture Deep-Dive

### 3.1 Python server (`server.py`)

The server is a **Starlette ASGI app** that wraps `FastMCP`. The key architectural fact:

```
FastMCP (/mcp)
    ↓ mounted inside
Starlette app
    ↓ wrapped with
_McpAcceptMiddleware   ← fixes Accept header for ICA's MCP client
_McpBearerMiddleware   ← enforces Bearer token on /mcp routes only
_SessionAuthMiddleware ← cookie-based auth for the web UI /api/* routes
```

**Startup order** (important — gets it wrong and Bob can't connect):
```python
# lifespan runs: DB open → auth init → schema migration → background refresh
@asynccontextmanager
async def _lifespan(app):
    await state.db._ensure_tables()
    state.db._run_migrations()
    await state.auth.setup()
    asyncio.create_task(state.start_background_refresh())
    yield
    # shutdown: cancel refresh task
```

### 3.2 `_McpAcceptMiddleware`

ICA's MCP client sends `Accept: application/json` but FastMCP expects `Accept: text/event-stream`. This middleware rewrites the Accept header before it reaches FastMCP.

```python
# Located at bottom of server.py around line 2214
# Intercepts: path starts with /mcp
# Action: sets Accept header to "text/event-stream, application/json"
```

### 3.3 `_McpBearerMiddleware`

Only applies to paths starting with `/mcp`. Reads `Authorization: Bearer <token>` and validates against `AUTH_TOKEN` (env var `READINESS_TOKEN`). Returns 401 if missing or wrong.

### 3.4 `_SessionAuthMiddleware`

Applies to all routes NOT in `_OPEN_PATHS`:
```python
_OPEN_PATHS = {"/health", "/framer-metadata", "/framer-site", "/sitemap.xml", 
               "/api/auth/login", "/"}
```

Note: `/api/ica/*` endpoints are **NOT** in `_OPEN_PATHS` — they require session auth. This matters when ICA tries to call them directly.

### 3.5 Route map (key routes)

| Route | Auth required | Purpose |
|---|---|---|
| `GET /health` | None | Liveness check |
| `GET /framer-metadata` | None | ICA connector form field discovery |
| `GET /framer-site` | None | Fake Framer HTML for ICA validator |
| `GET /sitemap.xml` | None | Sitemap for ICA crawl |
| `POST /mcp` | Bearer token | MCP StreamableHTTP |
| `GET /api/ica/features.csv` | Session | ICA feature CSV |
| `GET /api/ica/actions.csv` | Session | ICA action CSV |
| `GET /api/ica/releases.csv` | Session | ICA releases CSV |
| `GET /api/ica/modules.csv` | Session | ICA modules CSV |
| `GET /api/ica/action-types.csv` | Session | ICA action types CSV |
| `GET /api/ica/derivation-methods.csv` | Session | ICA derivation methods CSV |
| `GET /api/ica/schema-changes.json` | Session | Machine-readable schema todo list |

---

## 4. Deployment — Fly.io

### App details

| Field | Value |
|---|---|
| App name | `oraclereadinesssrc-dzxnqq` |
| Region | `lhr` |
| Memory | 256 MB |
| Volume | `/data` (persistent SQLite + reports) |
| Public URL | `https://oraclereadinesssrc-dzxnqq.fly.dev` |

### Deploy

```powershell
# ALWAYS use --remote-only on Windows — local Docker socket not available
cd "G:\My Drive\GIT_ROOT\Playground"
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only
```

**⚠️ CRITICAL: Never run `fly deploy` without `--remote-only` on Windows.** It will hang waiting for a local Docker daemon.

### Common deployment gotchas

| Problem | Cause | Fix |
|---|---|---|
| `TOML parse error` | `fly.toml` syntax — check `[[services]]` vs `[http_service]` | Run `fly config show` to validate |
| `volume not found` | Volume name mismatch between `fly.toml` and actual volume | `fly volumes list` to check |
| Deploy hangs | Missing `--remote-only` | Add `--remote-only` |
| Old image cached | Remote builder used stale layer | `fly deploy --remote-only --no-cache` |
| App healthy but tools return empty | Schema migration didn't run | SSH in and run `check_db.py` |
| `403 from /mcp` | `READINESS_TOKEN` changed | Update Bob config |

### SSH into the running machine

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq
```

### Get the READINESS_TOKEN

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq `
    -C "printenv READINESS_TOKEN"
```

### Run Python scripts on Fly

```powershell
# Upload a script first
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp shell --app oraclereadinesssrc-dzxnqq
# In sftp shell:
put check_db.py /data/check_db.py
# Then SSH in and run:
# python /data/check_db.py
```

Or use `-C` for one-liners:
```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq `
    -C "python -c \"import sqlite3; db=sqlite3.connect('/data/readiness.db'); print(db.execute('SELECT COUNT(*) FROM feature_details').fetchone())\""
```

---

## 5. ICA Context Studio Integration

### Context details

| Field | Value |
|---|---|
| Context ID | `ctx_9baeb72e480b` |
| Context name | `26c Complete Ontology` |
| Owner team | `MattStocker` |
| Team ID | `69aaae8a8482bc71f1c4af52` |
| Context Studio URL | `https://contextstudio.servicesessentials.ibm.com/?teamName=MattStocker&teamId=69aaae8a8482bc71f1c4af52&tab=context` |
| Source ID | `src_ef55df5d25d1` *(changes every time the source is deleted/re-added)* |
| Framer project URL | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |

> **Important:** The Source ID changes every time you delete and re-add the connector. Always look it up fresh.

### MCP Gateway (Bob integration)

```json
"context-studio": {
  "type": "streamable-http",
  "url": "https://servicesessentials.ibm.com/mcp-gateway/service/gateway/servers/8ccdd203bdee4014b08e82eedb6046e2/mcp",
  "headers": {
    "Authorization": "Bearer <ICA_GATEWAY_TOKEN>",
    "x-api-key": "<CONTEXT_ID>"
  },
  "disabled": false
}
```

| Placeholder | Value | Where to get it |
|---|---|---|
| `<ICA_GATEWAY_TOKEN>` | token starting with `orm-` | IBM Services Essentials → API Keys |
| `x-api-key` value | `ctx_9baeb72e480b` | Context Studio — 26c Complete Ontology |
| Gateway server ID | `8ccdd203bdee4014b08e82eedb6046e2` | IBM Services Essentials → MCP Gateway |

> **Security:** Never commit the raw Bearer token. The `x-api-key` is the public context ID and does not need rotation.

---

## 6. ICA Framer Connector — The Hard Part

This section documents every trap, failed approach, and working solution discovered over many hours.

### 6.1 The validator regex — what ICA actually accepts

ICA's `data-ingest` Python service validates `project_link` against a **strict regex**. The ONLY format that passes is:

```
https://framer.com/projects/<ProjectName>--<ProjectID>
```

**ALL of these are rejected:**

| URL | Reason rejected |
|---|---|
| `https://oracle-readiness-mcp.framer.app` | Published site subdomain — wrong domain |
| `https://oraclereadinesssrc-dzxnqq.fly.dev/framer-site` | Fly.io domain — wrong domain |
| `https://framer.com/m/...` | Share/preview link format |
| `https://framer.com/projects/oracle-readiness-mcp` | Missing `--<ProjectID>` suffix |

**The one that works:**
```
https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp
```
*(This is the actual Framer project created 2026-07-24. The project editor URL contains both the slugified name AND the opaque ID.)*

### 6.2 Why we have `/framer-site` and `/framer-metadata`

Both endpoints exist but serve **different purposes**:

| Endpoint | Purpose | Used by ICA? |
|---|---|---|
| `/framer-site` | Returns fake Framer-like HTML to satisfy ICA's HTML validator during connection testing | Attempted, but URL format rejected before HTML is even fetched |
| `/framer-metadata` | Returns machine-readable JSON with all ICA form field values — for human use only | No — it's a developer convenience endpoint |

**Neither endpoint is used in the actual working connector.** The working connector uses the real Framer project URL as `project_link`.

### 6.3 Form values for "Add data source" → "Framer"

| ICA form field | Value |
|---|---|
| Connection name | `Oracle Readiness MCP` |
| Connection URL / Project link | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
| MCP URL | `https://oraclereadinesssrc-dzxnqq.fly.dev/mcp` |
| Token / Bearer secret | *(value of `READINESS_TOKEN` — get via fly ssh)* |

### 6.4 Updating the source record via browser console

When the source is in a "failed" state with a wrong `project_link`, fix it without deleting:

```javascript
// Step 1: Find the current source ID
const sources = await fetch(
  '/data-ingest/sources?context_id=ctx_9baeb72e480b',
  { credentials: 'include' }
).then(r => r.json());
console.log(JSON.stringify(sources, null, 2));
// Look for the source with name "Oracle Readiness MCP"
// Note the "id" field (e.g. "src_ef55df5d25d1")

// Step 2: Update the project_link
const r = await fetch('/data-ingest/sources/src_ef55df5d25d1', {
  method: 'PUT',
  credentials: 'include',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    connection_details: {
      project_link: 'https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp'
    }
  })
});
console.log(r.status, await r.text());
// Expect: 200

// Step 3: Trigger ingest
const ingest = await fetch('/data-ingest/framer/ingest', {
  method: 'POST',
  credentials: 'include',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    source_id: 'src_ef55df5d25d1',
    context_id: 'ctx_9baeb72e480b'
  })
});
console.log(ingest.status, await ingest.text());
// Expect: 202 {"status":"accepted","message":"Framer ingestion started..."}

// Step 4: Poll progress
const progress = await fetch(
  '/data-ingest/framer-ingest/source/src_ef55df5d25d1',
  { credentials: 'include' }
).then(r => r.json());
console.log(JSON.stringify(progress, null, 2));
```

> **Note:** Source IDs change if you delete and re-add. Always look them up fresh with Step 1.

### 6.5 Why the ingest keeps failing — root cause analysis

The ingest was showing 0 entities in the knowledge graph for these reasons (in order of discovery):

1. **Wrong `project_link` format** — validator rejects the URL before any content is fetched
2. **`_ica_features()` was querying the wrong table** — it was calling `filter_entries()` which queries the `features` table (module-level catalogue stubs, 42 rows for 26C), not `feature_details` (1,725 individual feature rows). **Fixed.**
3. **`features.csv` had 42 rows** (all module-level stubs like "Absence Management What's New 26C"), not 1,725 individual features. ICA couldn't build a meaningful graph. **Fixed — now 1,726 rows.**
4. **XLSX `Feature_Summary.json` has `#UNCALCULATED` in every Feature cell** — the Excel formulas were never evaluated before export. This means flags cannot be populated from the XLSX. (See [Section 10](#10-flag-columns--the-backfill-problem))

---

## 7. ICA Schema Builder — CSV Upload Flow

### 7.1 What you need to upload (in order)

Upload these CSVs in the **Schema Builder → Upload Sample Data** panel, in this exact order:

| Order | Endpoint | Schema target | Row count |
|---|---|---|---|
| 1 | `/api/ica/releases.csv` | `enum:oracleFusion26cReleaseCode` | 3 (26D, 27A, 27B) |
| 2 | `/api/ica/action-types.csv` | `enum:oracleFusion26cActionType` | 2 |
| 3 | `/api/ica/modules.csv` | `enum:oracleFusion26cModule` | ~20 |
| 4 | `/api/ica/derivation-methods.csv` | `enum:oracleFusion26cMethod` | 1 |
| 5 | `/api/ica/features.csv?release=26C` | `custom:feature` nodes | 1,726 |
| 6 | `/api/ica/actions.csv?release=26C` | `custom:action` nodes | 1,716 |

Query parameters on features/actions:
- `?release=26C` — filter to a specific release  
- `?pillar=HCM` — filter to product family

### 7.2 Manual schema changes (cannot be done via CSV)

These **must** be done in the ICA Schema Builder UI:

**1. Add 5 properties to `custom:feature` node (Properties panel):**

| Property name | Curie | Data type | Required |
|---|---|---|---|
| Is AI Feature | `custom:isAiFeature` | boolean | false |
| AI Type | `custom:aiType` | string | false |
| Is Redwood | `custom:isRedwood` | boolean | false |
| Auto Enabled In | `custom:autoEnabledIn` | string | false |
| Opt In Required | `custom:optInRequired` | boolean | false |

**2. Set `featureCode` optional** on `oracleFusionGraphEntity`:
- Navigate to `oracleFusionGraphEntity` in Schema Builder
- Find property `featureCode`
- Change `required: true` → `required: false`
- Without this, ICA validation fails on any Feature node without a known feature code

### 7.3 CSV column order (ICA is strict about this)

```
schemaVersion, sourceWorkbook, domain, entityType, name, status,
moduleOrCategory, identifier, startDate, contextText
```

The `csv_response()` function in `ica.py` always outputs in this order. **Do not reorder.**

---

## 8. Data Pipeline — How Features Get In

### Path A: Automatic scraper (docs.oracle.com catalogue)

**Trigger:** Background loop every 6 hours, or `refresh_readiness_data` MCP tool  
**Source:** `docs.oracle.com/en/cloud/saas/readiness/` catalogue pages  
**Parses:** Module-level feature titles, release codes, links  
**Writes to:** `features` table  
**Result:** Module-level stubs (~685 rows for 26C) — NO individual feature descriptions  
**Flags populated:** `impact`, `enablement`, `is_ai`, `is_redwood`, `opt_in_required`, `auto_enabled_in` (extracted from HTML)

### Path B: XLSX dump ingest

**Trigger:** `ingest_xlsx_dump` MCP tool  
**Source:** `Feature_Summary.json` (dumped from Oracle Reports Centre XLSX)  
**⚠️ KNOWN PROBLEM:** The XLSX Feature column contains `#UNCALCULATED` for every row — Excel formulas were not evaluated before export. This means feature names cannot be read from the XLSX. See [Appendix D](#appendix-d--the-xlsx-trap-feature_summaryjson).  
**Writes to:** `features` table (updates existing rows with XLSX metadata)  
**Then calls:** `backfill_flags_from_features()` — but this produces **0 matches** because XLSX rows have `#UNCALCULATED` names  

### Path C: Deep scrape (feature detail pages)

**Trigger:** `deep_scrape_feature_details` MCP tool  
**Source:** Individual oracle.com/cloud/saas/.../whats-new/ feature pages  
**Parses:** Full description, Steps to Enable, Business Benefit, Key Resources, Tips  
**Writes to:** `feature_details` table  
**Result:** 1,725 rows for 26C with full `description_full` text  
**Flags populated:** Currently 0 — `parse_feature_detail_page()` does NOT extract is_ai/is_redwood etc. *(future work)*

### What `_ica_features()` does now (post-fix)

```python
# server.py ~line 1906
async def _ica_features(request):
    release = request.query_params.get("release")
    pillar  = request.query_params.get("pillar")

    # PRIMARY: use feature_details (rich individual features)
    rows = state.db.get_details_with_flags(release=release, product_family=pillar)

    if not rows:
        # FALLBACK: use features table (module-level stubs)
        rows = state.db.filter_entries(release=release, product_family=pillar)

    csv_rows = build_features_csv(rows)
    return Response(csv_response(csv_rows), media_type="text/csv")
```

**Before the fix:** Only `filter_entries()` was called → 42 rows  
**After the fix:** `get_details_with_flags()` called first → 1,726 rows

---

## 9. The `feature_details` vs `features` Hierarchy Problem

This is the **core architectural confusion** that caused hours of debugging.

### Two tables, two different levels of granularity

| Table | Granularity | Row count (26C) | Feature names | Flags |
|---|---|---|---|---|
| `features` | Module-level catalogue entry | ~685 | "Absence Management What's New 26C" | Yes (from HTML scrape) |
| `feature_details` | Individual feature | 1,725 | "AI-Driven Absence Prediction" | No (backfill produces 0 matches) |

### Why they can't be joined by feature_name

- `features` rows have names like `"Absence Management What's New 26C"` (the catalogue section title)
- `feature_details` rows have names like `"AI-Driven Absence Prediction"` (the actual individual feature)
- There is **no common key** between the two tables that allows a name-based JOIN
- The `feature_detail_url` column in `features` was added but never populated reliably

### The backfill produces 0 matches — this is expected

`backfill_flags_from_features()` tries to UPDATE `feature_details` with flags from `features` using:
```sql
WHERE LOWER(f.feature_name) = LOWER(fd.feature_name)
  AND UPPER(f.release) = UPPER(fd.release)
  AND LOWER(f.product_family) = LOWER(fd.product_family)
```
This **always** produces 0 rows updated because the names are at different hierarchy levels. This is not a bug — it's a data shape incompatibility.

### The fix for flags

The right fix is to extend `parse_feature_detail_page()` in `oracle_scraper.py` to extract flags (is_ai, is_redwood, opt_in_required, etc.) directly from the feature detail HTML pages. The data IS there in the Oracle HTML — it just needs parsing. This is documented in [Section 17 — Remaining Work](#17-what-still-needs-doing-remaining-work).

---

## 10. Flag Columns — The Backfill Problem

### Current state

All 8 flag columns in `feature_details` are **0 or NULL** for every row:

| Column | Status | Why |
|---|---|---|
| `is_ai` | 0 | Backfill produces 0 matches; scraper doesn't extract it |
| `ai_type` | NULL | Same |
| `is_redwood` | 0 | Same |
| `auto_enabled_in` | NULL | Same |
| `opt_in_required` | 0 | Same |
| `setup_required` | 0 | Same |
| `impact` | NULL | Same |
| `enablement` | NULL | Same |

### Why `contextText` still has value

Even with all flags at 0, the ICA CSV's `contextText` field contains the full `description_full` text (up to 300 chars) from each feature detail page. This is much richer than the previous 42-row output and ICA's vector store can use it for semantic search.

The `build_features_csv()` function in `ica.py` gracefully handles missing flags:
```python
flags: list[str] = []
if is_ai:    flags.append(f"Is AI: true. AI type: {ai_type or 'unspecified'}.")
if is_rw:    flags.append("Is Redwood: true.")
# ... only appended when truthy, so zero-value flags add no noise
```

### Migration DDL (already applied to live DB)

```python
# db.py _MIGRATION_DDL
"ALTER TABLE feature_details ADD COLUMN is_ai       INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN ai_type     TEXT",
"ALTER TABLE feature_details ADD COLUMN is_redwood  INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN auto_enabled_in TEXT",
"ALTER TABLE feature_details ADD COLUMN opt_in_required INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN setup_required  INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN impact      TEXT",
"ALTER TABLE feature_details ADD COLUMN enablement  TEXT",
```

These migrations are **idempotent** — wrapped in try/except so re-running is safe.

---

## 11. MCP Tools Reference

### Foundational / navigation

| Tool | Key params | Notes |
|---|---|---|
| `list_products` | — | Returns list of product lines |
| `get_cache_status` | — | DB row counts, last scrape time |
| `list_releases` | — | All release codes in DB |
| `list_product_families` | `release` | Pillars for a release |
| `list_modules` | `release`, `product_family` | Modules for a release/pillar |

### Feature retrieval

| Tool | Key params | Notes |
|---|---|---|
| `get_release_notes` | `product`, `release`, `limit` | Module-level list |
| `search_release_notes` | `query`, `product`, `release`, `module` | Full-text search |
| `get_features_by_module` | `release`, `module` | Features in a module |
| `get_feature_summary` | `release` | High-level stats |

### Filtered views

| Tool | Key params | Notes |
|---|---|---|
| `get_opt_in_features` | `release` | Features requiring opt-in |
| `get_setup_required_features` | `release` | Features requiring setup |
| `get_high_impact_features` | `release` | High-impact features |
| `get_auto_enabled_features` | `release` | Auto-enabled features |
| `get_ai_features` | `release` | AI-related features |
| `get_redwood_features` | `release` | Redwood UI features |

### Deep detail

| Tool | Key params | Notes |
|---|---|---|
| `get_feature_detail` | `feature_name`, `release`, `product_family` | Full detail by name |
| `get_feature_detail_by_url` | `url` | Full detail by page URL |
| `list_features_with_steps` | `release`, `product_family`, `module` | Features with Steps to Enable |
| `list_features_with_tips` | `release`, `product_family`, `module` | Features with Tips |
| `search_feature_details` | `query`, `release`, `product_family` | Search detail text |
| `deep_scrape_feature_details` | `products`, `releases` | Trigger deep scrape |

### Reporting

| Tool | Key params | Notes |
|---|---|---|
| `generate_report` | `filters`, `include_content`, `save_report` | Markdown/JSON report |
| `get_document_content` | `url` | Fetch full doc text |
| `compare_releases` | `module`, `old_release`, `new_release` | Diff between releases |
| `push_report_to_github` | `repo`, `branch`, `pillars` | Push markdown to GitHub |

### Data loading

| Tool | Key params | Notes |
|---|---|---|
| `refresh_readiness_data` | `products` | Trigger catalogue scrape |
| `ingest_xlsx_dump` | `json_path` | Load XLSX dump → `features` table |

### ICA export

| Tool | Key params | Notes |
|---|---|---|
| `get_ica_framer_csv` | `entity_type`, `release`, `pillar` | Returns CSV for ICA upload |

`entity_type` values: `features`, `actions`, `releases`, `action-types`, `modules`, `derivation-methods`, `schema-changes`

---

## 12. Authentication Layers

### 12.1 Web UI (session cookie)

- Login via `POST /api/auth/login` with `{username, password}`
- Session stored in `auth_sessions` table (SQLite)
- Cookie: `readiness_session=<token>`
- Admin users can manage other users via `/api/users/*`

### 12.2 MCP Bearer token (`READINESS_TOKEN`)

- Set as Fly.io secret: `fly secrets set READINESS_TOKEN=<value>`
- Required on all requests to `/mcp`
- Header: `Authorization: Bearer <token>`
- Bob config: add to MCP server `headers` block

### 12.3 ICA MCP Gateway token

- Separate IBM Services Essentials token (starts with `orm-`)
- Used by Bob when connecting **via ICA** (not directly to Fly.io)
- NOT the same as `READINESS_TOKEN`

---

## 13. Common Errors and Fixes — The War Stories

This section captures every error encountered and its solution.

### ICA Framer connector errors

| Error message | Root cause | Fix |
|---|---|---|
| `Invalid Framer project URL format` | `project_link` is not `https://framer.com/projects/<Name>--<ID>` | Use the real Framer project editor URL |
| `missing project_link` | `connection_details` object is empty or missing `project_link` key | PUT source with `{connection_details:{project_link:'https://framer.com/projects/...'}}` |
| `Source connection_details missing project_link` | Object exists but key absent | Same fix as above |
| `Failed to start ingestion: 401` | Wrong or missing Bearer token in MCP URL | Re-check `READINESS_TOKEN` via fly ssh |
| `Failed to start ingestion: 403` | Source belongs to different team context | Open Context Studio under `MattStocker` team, not `Oracle Practice UKI` |
| Framer source shows "failed", graph has 0 entities | `_ica_features()` was querying `features` table (42 rows) not `feature_details` (1726 rows) | **Already fixed** — `get_details_with_flags()` now called first |
| Ingest "accepted" but graph stays at 0 | project_link accepted but ICA fetches URL and HTML fails | Ensure Framer project is published and URL resolves |

### Fly.io / deployment errors

| Error | Cause | Fix |
|---|---|---|
| `error building image: failed to solve` | Dockerfile BUILD stage fails | Check `requirements.txt` — a package may have changed API |
| `volume not found` | Volume name in fly.toml doesn't match | `fly volumes list --app oraclereadinesssrc-dzxnqq` |
| `app crashed immediately` | Startup exception in lifespan | `fly logs --app oraclereadinesssrc-dzxnqq` to see traceback |
| SSH works but app returns 502 | App is starting (give it 30 seconds) | `fly status --app oraclereadinesssrc-dzxnqq` |
| `permission denied on /data/readiness.db` | Volume not mounted or wrong permissions | Check `fly.toml` volume mount |

### Python / server errors

| Error | Cause | Fix |
|---|---|---|
| `AttributeError: 'NoneType' has no attribute 'get'` | `filter_entries()` called with `release or ""` producing empty-string release | Fixed: use `release if release else None` |
| `OperationalError: table feature_details has no column named is_ai` | Migration not run | DB is created fresh — migration auto-runs at startup |
| `csv.Error: field larger than field limit` | contextText > 131072 chars | Already handled — `desc[:300]` in `build_features_csv()` |
| `422 Unprocessable Entity` from MCP tool | Pydantic validation — wrong field name or type | Check `model_config = ConfigDict(extra="forbid")` — no extra fields |

### Data / ingest errors

| Error | Cause | Fix |
|---|---|---|
| `features.csv` returns 42 rows | `_ica_features()` using `filter_entries()` (features table) | **Fixed** — now uses `get_details_with_flags()` |
| XLSX ingest runs but flags stay 0 | `Feature_Summary.json` has `#UNCALCULATED` in Feature column | Known issue — XLSX formulas not evaluated. See Appendix D |
| `backfill_flags_from_features()` returns 0 | Names in `features` and `feature_details` are at different hierarchy levels | Expected — not a bug. See Section 9 |
| `feature_details` empty after deep scrape | `deep_scrape_feature_details` tool not run yet | Run `deep_scrape_feature_details(releases=["26C"])` |

### Auth / web UI errors

| Error | Cause | Fix |
|---|---|---|
| `403 Forbidden` on `/api/ica/*` | Session cookie missing | Log in at `/` first |
| `401 Unauthorized` on `/mcp` | Wrong or missing `Authorization: Bearer <token>` | Check `READINESS_TOKEN` env var |
| Cookie not sent | Browser blocks cross-origin cookies | Open Context Studio and the app in the same browser session |

### The `_ica_actions` bug (fixed)

The original code had:
```python
# BROKEN — "release or ''" produces empty string, not None
rows = state.db.get_all_details_for_release(release or "", product_family=pillar or None)
```
This caused an empty-string release to be passed to the DB query, returning 0 rows.

Fixed version:
```python
if release:
    rows = state.db.get_all_details_for_release(release, product_family=pillar or None)
else:
    rows = state.db.get_details_with_flags(product_family=pillar or None)
```

---

## 14. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `READINESS_TOKEN` | `""` | Bearer token for MCP endpoint authentication |
| `READINESS_DATA_DIR` | `/data` | Directory for SQLite DB and reports |
| `READINESS_REFRESH_HOURS` | `6` | Auto-refresh interval in hours |
| `READINESS_AUTOSTART_REFRESH` | `1` | Set to `0` to disable background refresh |
| `READINESS_HTTP_HOST` | `0.0.0.0` | HTTP bind host |
| `READINESS_HTTP_PORT` | `8080` | HTTP bind port |
| `APP_URL` | `https://oraclereadinesssrc-dzxnqq.fly.dev` | Public base URL (used in health/framer-metadata responses) |

Set Fly.io secrets:
```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" secrets set `
    --app oraclereadinesssrc-dzxnqq `
    READINESS_TOKEN=<new-token>
```

---

## 15. Bob Config — Connecting Bob to the Live Server

### Direct MCP connection (recommended for development)

Add to `bob-config.yaml` (or equivalent MCP config):

```json
"oracle-readiness": {
  "type": "streamable-http",
  "url": "https://oraclereadinesssrc-dzxnqq.fly.dev/mcp",
  "headers": {
    "Authorization": "Bearer <READINESS_TOKEN>"
  },
  "disabled": false
}
```

Get the token:
```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq -C "printenv READINESS_TOKEN"
```

### Via ICA MCP Gateway (for production, context-enriched responses)

```json
"context-studio": {
  "type": "streamable-http",
  "url": "https://servicesessentials.ibm.com/mcp-gateway/service/gateway/servers/8ccdd203bdee4014b08e82eedb6046e2/mcp",
  "headers": {
    "Authorization": "Bearer <ICA_GATEWAY_TOKEN>",
    "x-api-key": "<CONTEXT_ID>"
  },
  "disabled": false
}
```

---

## 16. Quick-Reference Commands

### Deploy

```powershell
cd "G:\My Drive\GIT_ROOT\Playground"
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only
```

### Health checks

```bash
# Quick liveness check
curl https://oraclereadinesssrc-dzxnqq.fly.dev/health

# ICA connector form values
curl https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata

# Check what schema changes still need manual UI action
curl https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/schema-changes.json
```

### Download CSVs for manual ICA upload

```bash
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" -o features_26C.csv
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/actions.csv?release=26C"  -o actions_26C.csv
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/releases.csv"             -o releases.csv
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/modules.csv"              -o modules.csv
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/action-types.csv"        -o action_types.csv
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/derivation-methods.csv"  -o derivation_methods.csv
```

### Fly.io operations

```powershell
# Deploy
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only

# Logs (live tail)
& "$env:USERPROFILE\.fly\bin\fly.exe" logs --app oraclereadinesssrc-dzxnqq

# App status
& "$env:USERPROFILE\.fly\bin\fly.exe" status --app oraclereadinesssrc-dzxnqq

# Get token
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "printenv READINESS_TOKEN"

# SSH shell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq

# DB row counts (quick diagnostic)
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq `
    -C "python -c \"import sqlite3; db=sqlite3.connect('/data/readiness.db'); db.row_factory=sqlite3.Row; [print(t,db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ['features','feature_details']]\""
```

### Local dev

```powershell
# Copy .env.example and fill in values
cp .env.example .env

# Run with podman
podman-compose up

# Or with Python directly (ensure venv activated)
python server.py
```

---

## 17. What Still Needs Doing (Remaining Work)

### Priority 1 — Fix the Framer ingest (manual browser steps)

These steps must be done in the browser while logged into Context Studio:

1. **Open browser console on `contextstudio.servicesessentials.ibm.com`**
2. **Find current source ID:**
   ```javascript
   const s = await fetch('/data-ingest/sources?context_id=ctx_9baeb72e480b',
     {credentials:'include'}).then(r=>r.json());
   console.log(JSON.stringify(s, null, 2));
   ```
3. **Update `project_link`** (replace `src_XXXX` with real ID from step 2):
   ```javascript
   await fetch('/data-ingest/sources/src_XXXX', {
     method:'PUT', credentials:'include',
     headers:{'Content-Type':'application/json'},
     body: JSON.stringify({connection_details:{
       project_link:'https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp'
     }})
   });
   ```
4. **Trigger ingest:**
   ```javascript
   await fetch('/data-ingest/framer/ingest', {
     method:'POST', credentials:'include',
     headers:{'Content-Type':'application/json'},
     body: JSON.stringify({source_id:'src_XXXX', context_id:'ctx_9baeb72e480b'})
   });
   ```
5. **Verify:** Poll `GET /data-ingest/framer-ingest/source/src_XXXX` until status is not "running"

### Priority 2 — Add 5 properties to Feature node in ICA Schema Builder UI

Navigate to: Context Studio → 26c Complete Ontology → Schema Builder → `custom:feature` → Properties panel

Add:
- `isAiFeature` (boolean, optional)
- `aiType` (string, optional)
- `isRedwood` (boolean, optional)
- `autoEnabledIn` (string, optional)
- `optInRequired` (boolean, optional)

Also: set `featureCode` on `oracleFusionGraphEntity` to `required: false`

### Priority 3 — Upload the 6 CSVs in Schema Builder

Use the **Upload Sample Data** button in Schema Builder, in this order:
1. releases.csv
2. action-types.csv
3. modules.csv
4. derivation-methods.csv
5. features.csv?release=26C
6. actions.csv?release=26C

### Priority 4 — Extract flags from feature detail HTML (code change)

**File to edit:** `oracle_scraper.py` → `parse_feature_detail_page()`  
**Goal:** Extract `is_ai`, `is_redwood`, `opt_in_required`, `auto_enabled_in`, `impact`, `enablement` directly from each feature's Oracle HTML page  
**How:** These are shown as tags/badges/labels in the Oracle What's New pages  
**Result:** `feature_details.is_ai` etc. will be populated by the deep scraper  
**Then:** `build_features_csv()` will automatically include them in the ICA contextText

### Priority 5 — Schema rationalisation (future)

Add proper node types to the ICA ontology:
- `OracleRelease` node
- `OraclePillar` node
- `OracleModule` node
- `EnablementClassification` node
- `ImpactLevel` node

Add 8 new edges:
- `CONTAINS_FEATURE` (OracleModule → Feature)
- `INCLUDES_MODULE` (OraclePillar → OracleModule)
- `DELIVERS_FEATURE` (OracleRelease → Feature)
- `HAS_ACTION` (Feature → Action)
- `REQUIRES_ENABLEMENT` (Feature → EnablementClassification)
- `HAS_IMPACT` (Feature → ImpactLevel)
- `NEXT_RELEASE` (OracleRelease → OracleRelease)
- `PART_OF` (Feature → OracleModule)

Add new `/api/ica/` endpoints in `server.py` and builder functions in `ica.py` for the new node types.

---

## Appendix A — Git Commit History Summary

| Commit | Description |
|---|---|
| `affc3b3` | Fix _ica_features to use feature_details; add 8 flag columns; fix _ica_actions bug; update CONNECTOR.md; create KNOWLEDGEBASE.md |
| Earlier | Initial FastMCP server with scraper, auth, settings, web UI |
| Earlier | Add deep scrape tools (get_feature_detail, list_features_with_steps, etc.) |
| Earlier | Add ICA CSV export endpoints (ica.py, _ica_features, _ica_actions) |
| Earlier | Add /framer-site and /framer-metadata endpoints |
| Earlier | Fix _McpAcceptMiddleware for ICA MCP client compatibility |

---

## Appendix B — ICA CSV Column Order

The ICA Schema Builder "Upload Sample Data" function requires **exactly** this column order:

```
schemaVersion   → Always "1"
sourceWorkbook  → Always "OracleReadinessMCP"
domain          → Always "OracleFusion26C"
entityType      → "Feature", "Action", "Module", "Release", "DerivationMethod"
name            → The display name
status          → Always "active"
moduleOrCategory → Module name or category
identifier      → Unique slug (e.g. "F-absence-prediction")
startDate       → ISO 8601 date string (e.g. "2025-06-01T00:00:00Z")
contextText     → Rich text for vector embedding (name + module + release + description + flags)
```

The `csv_response()` function in `ica.py` enforces this order. If you call it with a dict that has extra keys, `extrasaction="ignore"` silently drops them. If a required key is missing, the CSV cell is empty.

---

## Appendix C — Database Schema

### `features` table (module-level catalogue)

```sql
CREATE TABLE features (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_family  TEXT NOT NULL,
    release         TEXT NOT NULL,
    module          TEXT,
    feature_name    TEXT NOT NULL,
    description     TEXT,
    impact          TEXT,
    enablement      TEXT,
    is_ai           INTEGER DEFAULT 0,
    ai_type         TEXT,
    is_redwood      INTEGER DEFAULT 0,
    auto_enabled_in TEXT,
    opt_in_required INTEGER DEFAULT 0,
    setup_required  INTEGER DEFAULT 0,
    source_url      TEXT,
    scraped_at      TEXT,
    feature_detail_url TEXT,   -- ← added by migration (not reliably populated)
    UNIQUE(product_family, release, feature_name)
);
```

### `feature_details` table (individual features from deep scrape)

```sql
CREATE TABLE feature_details (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_page_url  TEXT UNIQUE NOT NULL,
    feature_name      TEXT,
    release           TEXT,
    product_family    TEXT,
    module            TEXT,
    description_full  TEXT,
    business_benefit  TEXT,
    steps_to_enable   TEXT,
    tips_considerations TEXT,
    key_resources     TEXT,
    other_sections    TEXT,
    optional_uptake   TEXT,
    fetched_at        TEXT,
    -- Added by _MIGRATION_DDL:
    is_ai             INTEGER NOT NULL DEFAULT 0,
    ai_type           TEXT,
    is_redwood        INTEGER NOT NULL DEFAULT 0,
    auto_enabled_in   TEXT,
    opt_in_required   INTEGER NOT NULL DEFAULT 0,
    setup_required    INTEGER NOT NULL DEFAULT 0,
    impact            TEXT,
    enablement        TEXT
);
```

### Row counts (as of 2026-07-25 on live Fly.io DB)

| Table | Row count | Notes |
|---|---|---|
| `features` | ~685 | 26C module-level stubs |
| `feature_details` | 1,725 | 26C individual features (deep scrape) |
| `feature_details` (with 26C flag) | 1,725 | All 0 flags currently |

---

## Appendix D — The XLSX Trap (`Feature_Summary.json`)

### What happened

The Oracle Reports Centre XLSX was downloaded and converted to `Feature_Summary.json` using Bob's `read_xlsx` tool with `dump:true`. The JSON was uploaded to `/data/Feature_Summary.json` on Fly.io and ingested via `ingest_xlsx_dump`.

### The problem

Every row in the XLSX `Feature` column (the main feature name column) contains the string literal `#UNCALCULATED`. This is because Excel computed columns (using formulas like `=IF(...)`) were not evaluated before export — the XLSX file was saved in a state where the formula cells had not been recalculated.

```json
// Example from Feature_Summary.json
{
  "Feature": "#UNCALCULATED",
  "Module": "Absence Management",
  "Release": "26C",
  "Is AI Feature": "No",
  ...
}
```

### Consequence

- `ingest_xlsx_dump` inserts rows with `feature_name="#UNCALCULATED"` into the `features` table
- `backfill_flags_from_features()` tries to match `feature_name="#UNCALCULATED"` against `feature_details.feature_name` = "AI-Driven Absence Prediction" → **0 matches**
- All flag columns remain 0

### How to fix (for future XLSX exports)

Before exporting from Oracle Reports Centre:
1. Open the XLSX in Excel
2. Press `Ctrl+Alt+F9` to force-recalculate all formulas
3. Save the file
4. Then export to JSON

Or use a different export format (CSV export directly from Oracle Reports Centre bypasses the formula evaluation problem).

### Workaround in place

The `_ica_features()` endpoint now reads from `feature_details` (deep scrape) instead of `features` (XLSX/catalogue). The XLSX flags problem does not affect the live CSVs — the `contextText` is rich with `description_full` from the deep scrape even without the flags.

---

*End of Knowledge Base*

> **Vault Radar note:** This file contains placeholder values only. No secrets, tokens, or API keys are stored here. Token values must be retrieved via `fly secrets` or `fly ssh console`.
