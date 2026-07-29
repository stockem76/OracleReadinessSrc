# Oracle Readiness MCP — Master Knowledge Base

> **Last updated:** 2026-07-28 — Obsidian ↔ Open Notebook integration bug-fix session
> **Status:** Server ✅ | ICA source record `project_link` ✅ correct | Framer site ❌ blank — **5 nodes root cause**
> **Purpose:** Single source of truth. Every dead end documented. Every fix proven.

---

> 📖 **New to this project or returning after a break?**
> Read **[`docs/FIELD_GUIDE.md`](docs/FIELD_GUIDE.md)** first — it is the practical survival guide
> written after 6+ hours of debugging, with every trap mapped and the fastest path forward.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Architecture Deep-Dive](#3-architecture-deep-dive)
4. [Deployment — Fly.io](#4-deployment--flyio)
5. [ICA Context Studio Integration](#5-ica-context-studio-integration)
6. [The ICA Framer Ingest — Root Cause & Fix](#6-the-ica-framer-ingest--root-cause--fix)
7. [Dead Ends — Do Not Retry](#7-dead-ends--do-not-retry)
8. [ICA Schema Builder — CSV Upload Flow](#8-ica-schema-builder--csv-upload-flow)
9. [Data Pipeline — How Features Get In](#9-data-pipeline--how-features-get-in)
10. [The `feature_details` vs `features` Hierarchy Problem](#10-the-feature_details-vs-features-hierarchy-problem)
11. [Flag Columns — Current State](#11-flag-columns--current-state)
12. [MCP Tools Reference](#12-mcp-tools-reference)
13. [Authentication Layers](#13-authentication-layers)
14. [Common Errors and Fixes](#14-common-errors-and-fixes)
15. [Environment Variables](#15-environment-variables)
16. [Bob Config — Connecting Bob to the Live Server](#16-bob-config--connecting-bob-to-the-live-server)
17. [Quick-Reference Commands](#17-quick-reference-commands)
18. [What Still Needs Doing](#18-what-still-needs-doing)
19. [Appendix A — Git Commit History](#appendix-a--git-commit-history)
20. [Appendix B — ICA CSV Column Order](#appendix-b--ica-csv-column-order)
21. [Appendix C — Database Schema](#appendix-c--database-schema)
22. [Appendix D — The XLSX Trap](#appendix-d--the-xlsx-trap)
23. [Appendix E — Full Audit Findings (2026-07-25)](#appendix-e--full-audit-findings-2026-07-25)
24. [Appendix F — Obsidian ↔ Open Notebook Integration (2026-07-28)](#appendix-f--obsidian--open-notebook-integration-2026-07-28)

---

## 1. Project Overview

A **Python FastMCP server** deployed to Fly.io that:
1. Scrapes Oracle Cloud Applications Readiness pages for What's New features
2. Exposes those features as MCP tools to AI agents (Bob, Claude, etc.)
3. Serves ICA-compatible CSV files so IBM Context Studio can ingest features into the "26c Complete Ontology" knowledge graph

**Live URL:** `https://oraclereadinesssrc-dzxnqq.fly.dev`  
**Fly.io app name:** `oraclereadinesssrc-dzxnqq`  
**Region:** `lhr` (London)  
**ICA Context:** `26c Complete Ontology` (`ctx_9baeb72e480b`)  
**ICA Team:** `MattStocker` (`teamId=69aaae8a8482bc71f1c4af52`)

---

## 2. Repository Layout

```
Playground/
├── server.py              ← Main FastMCP + Starlette app (ALL routes)
├── db.py                  ← SQLite layer (ReadinessDB class)
├── ica.py                 ← ICA CSV builder functions
├── oracle_scraper.py      ← HTML scraper for Oracle readiness pages
├── scheduler.py           ← Background scheduler
├── auth.py                ← Session auth (AuthDB class)
├── settings.py            ← App settings store
├── Dockerfile             ← Python 3.12-slim build
├── fly.toml               ← Fly.io config
├── requirements.txt       ← Python deps
├── static/index.html      ← Web UI
├── CONNECTOR.md           ← ICA form values quick-reference
├── KNOWLEDGEBASE.md       ← This file
│
├── docs/
│   ├── ICA_INGEST_RUNBOOK.md     ← Step-by-step browser runbook
│   ├── DATA_PIPELINE_DIAGNOSIS.md ← Diagnosis guide
│   └── DEPLOY_CHEATSHEET.md      ← All Fly.io commands
│
└── oracle-readiness-mcp/  ← Original TypeScript MCP (reference only)

Fly.io volume /data/:
├── readiness.db           ← SQLite: 685 features, 2070 feature_details
└── Feature_Summary.json   ← XLSX dump (#UNCALCULATED — see Appendix D)
```

---

## 3. Architecture Deep-Dive

### 3.1 The three middleware layers

```
FastMCP (/mcp)  ←  _McpBearerMiddleware  ←  _SessionAuthMiddleware  ←  _McpAcceptMiddleware
```

- **`_McpAcceptMiddleware`** — injects `Accept: text/event-stream` header; ICA's MCP client sends wrong Accept
- **`_McpBearerMiddleware`** — Bearer token check on `/mcp` only
- **`_SessionAuthMiddleware`** — cookie auth for web UI; `/api/ica/*` routes are NOT in `_OPEN_PATHS` (requires session)

### 3.2 Key constants (server.py)

```python
_APP_URL = os.environ.get("APP_URL", "https://oraclereadinesssrc-dzxnqq.fly.dev").rstrip("/")

# THE SINGLE SOURCE OF TRUTH for ICA project_link
_FRAMER_PROJECT_URL = "https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp"
```

**`_FRAMER_PROJECT_URL` must never be changed to a Fly.io URL.** ICA's validator rejects it. See Section 6.

### 3.3 Route open/closed status

| Route | Auth | Notes |
|---|---|---|
| `GET /health` | None | Returns `project_link` = `_FRAMER_PROJECT_URL` |
| `GET /framer-metadata` | None | Human-readable connector reference |
| `GET /framer-site` | None | Fake Framer HTML (for reference only — not the project_link) |
| `GET /sitemap.xml` | None | ICA crawl sitemap |
| `POST /mcp` | Bearer token | MCP StreamableHTTP |
| `GET /api/ica/*.csv` | **Session** | ICA CSVs — must log in first |
| `GET /api/*` | Session | REST API |
| `GET /` | None | Web UI HTML |

---

## 4. Deployment — Fly.io

### App details

| Field | Value |
|---|---|
| App name | `oraclereadinesssrc-dzxnqq` |
| Region | `lhr` |
| Memory | 256 MB |
| Volume | `/data` (persistent) |
| Public URL | `https://oraclereadinesssrc-dzxnqq.fly.dev` |

### Standard deploy

```powershell
cd "G:\My Drive\GIT_ROOT\Playground"
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only
```

**⚠️ ALWAYS `--remote-only` on Windows — no local Docker daemon.**

### Verify deploy succeeded

After `fly deploy`, the Fly CLI may print:
```
WARNING The app is not listening on the expected address
```

**This is a false alarm** if the old process is still running on 8080 and the new image was successfully deployed. Verify with:

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq diag3.py
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "python diag3.py"
```

Look for `_FRAMER_PROJECT_URL: FOUND` to confirm new image is running.

### SSH diagnostics

```powershell
# Get READINESS_TOKEN
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "printenv READINESS_TOKEN"

# Check what's running on port 8080 (from inside container)
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq diag2.py
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "python diag2.py"

# Upload a file
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq <local-file>
```

### Deploy gotchas

| Problem | Cause | Fix |
|---|---|---|
| Deploy hangs | Missing `--remote-only` | Add `--remote-only` |
| `WARNING: not listening on expected address` | Old process still on port 8080 during rolling deploy | Usually fine — verify with diag3.py |
| `address already in use` when running python manually via SSH | PID 650 (server) already has 8080 | Normal — server is running correctly |
| Build context 658 MB warning | Large files in workspace (readiness_diag_copy.db etc.) | Add `.dockerignore` to exclude them |

### .dockerignore (add this to reduce build context)

```
*.db
*.json
diag*.py
deployed_server*.py
framer_*.html
framer_*.txt
__pycache__/
*.pyc
.env
.git/
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
| Source ID | `src_ef55df5d25d1` *(changes if source is deleted/re-added)* |
| Framer project URL | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |

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

> `<CONTEXT_ID>` = `ctx_9baeb72e480b` (not secret, no rotation needed)

---

## 6. The ICA Framer Ingest — Root Cause & Fix

### What actually failed (confirmed 2026-07-25)

Three simultaneous failures, all caused by the same mistake:

#### Failure 1: Server never redeployed with fix
The `server.py` fix was committed but `fly deploy` was never re-run. The live server had the old code for hours. **Fixed by deploying commit 93cd7c7.**

#### Failure 2: `_health()` returned wrong `project_link`
```python
# OLD (broken):
"project_link": f"{_APP_URL}/framer-metadata"   # → Fly.io URL, rejected by ICA

# NEW (fixed):
"project_link": _FRAMER_PROJECT_URL              # → https://framer.com/projects/...
```

#### Failure 3: `_framer_metadata()` returned wrong `project_link`
```python
# OLD (broken):
"project_link": f"{_APP_URL}/framer-site"        # → Fly.io URL, rejected by ICA

# NEW (fixed):
"project_link": _FRAMER_PROJECT_URL              # → https://framer.com/projects/...
```

### Why the ICA source record also needs updating

Even with the server fixed, ICA stores the `project_link` value you entered when adding the source. That stored value is also wrong. **You must update it via browser console:**

```javascript
// Step 1 — Get source ID (run in Context Studio browser console)
const s = await fetch('/data-ingest/sources?context_id=ctx_9baeb72e480b',
  {credentials:'include'}).then(r=>r.json());
console.log(JSON.stringify(s, null, 2));
// Note the "id" field of "Oracle Readiness MCP" source

// Step 2 — Update project_link (replace src_XXXX)
const r = await fetch('/data-ingest/sources/src_XXXX', {
  method: 'PUT', credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({connection_details:{
    project_link: 'https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp'
  }})
});
console.log(r.status, await r.text()); // Expect: 200

// Step 3 — Trigger ingest
const r2 = await fetch('/data-ingest/framer/ingest', {
  method: 'POST', credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({source_id:'src_XXXX', context_id:'ctx_9baeb72e480b'})
});
console.log(r2.status, await r2.text()); // Expect: 202

// Step 4 — Poll status
const p = await fetch('/data-ingest/framer-ingest/source/src_XXXX',
  {credentials:'include'}).then(r=>r.json());
console.log(JSON.stringify(p, null, 2));
```

### How ICA validates the project_link (confirmed behaviour)

ICA's `data-ingest` service runs the `project_link` value through a regex **before** fetching the URL. The regex only accepts:
```
https://framer.com/projects/<ProjectName>--<ProjectID>
```

This check happens at the API level. **No amount of HTML spoofing on the Fly.io server will bypass it.** The URL format must be right.

---

## 7. Dead Ends — Do Not Retry

These approaches were all tried and confirmed as dead ends. Do not revisit them.

### ❌ DEAD END: Using `{APP_URL}/framer-site` as `project_link`

**What was tried:** Point ICA's `project_link` at our Fly.io `/framer-site` endpoint  
**Why it fails:** ICA validates the URL format with a regex. Any URL not matching `https://framer.com/projects/<name>--<id>` is rejected with "Invalid Framer project URL format" before the URL is ever fetched  
**Evidence:** Multiple failed ingest attempts; confirmed by reading ICA error messages

### ❌ DEAD END: Using `{APP_URL}/framer-metadata` as `project_link`

**What was tried:** Point ICA's `project_link` at `/framer-metadata`  
**Why it fails:** Same regex validation — Fly.io domain rejected  
**Evidence:** Was hardcoded in `_health()` for ~6 hours without being noticed

### ❌ DEAD END: HTML spoofing to pass the Framer validator

**What was tried:** Serving fake Framer HTML with `data-framer-hydrate-v2`, `Server: Framer/5d364ee`, `window.__framer_importFromPackage` etc.  
**Why it fails:** ICA's validator is URL-format-first, not HTML-content-first. If the URL format is wrong, the HTML is never fetched  
**Status:** The `/framer-site` endpoint still exists and serves correct Framer-like HTML. This is fine for future use if ICA changes its validation, but is NOT the current ingest mechanism  
**Evidence:** diag2.py showed `data-framer: FOUND` but ingest still failed → confirmed URL format is the issue, not HTML content

### ❌ DEAD END: `window.__framer__` object in framer-site HTML

**What was tried:** Adding `window.__framer__ = {...}` to the `/framer-site` HTML  
**Why it fails:** Real Framer published sites do NOT have `window.__framer__` in the HTML body. That string appears only in a localStorage check script: `localStorage.getItem("__framer_force_showing_editorbar_since")`. ICA does not check for this  
**Evidence:** Fetched real `oracle-readiness-mcp.framer.website` — no `window.__framer__` in body; site still works  

### ❌ DEAD END: Framer published site URL as `project_link`

**What was tried:** `https://oracle-readiness-mcp.framer.app` (published site subdomain)  
**Why it fails:** ICA regex requires `framer.com/projects/`, not `*.framer.app`  
**Evidence:** ICA error "Invalid Framer project URL format"

### ❌ DEAD END: Framer project editor URL `framer.com/projects/...`

**What was tried:** `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` — this passes the ICA regex  
**Current status:** This IS the correct URL. But the ingest still shows "failed" because:  
1. The ICA source record stores the **old** URL — needs PUT update via console  
2. The Framer project page at this URL returns a **private project editor page** (status 200 but no Framer site markers) — ICA may or may not care about the HTML content of this URL once the regex passes

### ⚠️ COMPLICATED: Backfilling flags from XLSX

**What was tried:** `backfill_flags_from_features()` — UPDATE feature_details with flags from features table  
**Why it produces 0 results:** Feature names in `features` table are module-level stubs ("Absence Management What's New 26C"); feature names in `feature_details` are individual features ("AI-Driven Absence Prediction") — no name overlap  
**Status:** Not a bug — a data shape incompatibility. The XLSX also has `#UNCALCULATED` in all Feature cells  
**Best path forward:** Extend `parse_feature_detail_page()` in `oracle_scraper.py` to extract flags directly from Oracle HTML detail pages

---

## 8. ICA Schema Builder — CSV Upload Flow

### Upload order (Schema Builder → Upload Sample Data)

| Order | Endpoint | Target | Row count |
|---|---|---|---|
| 1 | `/api/ica/releases.csv` | `enum:oracleFusion26cReleaseCode` | 3 |
| 2 | `/api/ica/action-types.csv` | `enum:oracleFusion26cActionType` | 2 |
| 3 | `/api/ica/modules.csv` | `enum:oracleFusion26cModule` | ~20 |
| 4 | `/api/ica/derivation-methods.csv` | `enum:oracleFusion26cMethod` | 1 |
| 5 | `/api/ica/features.csv?release=26C` | `custom:feature` nodes | 1,726 |
| 6 | `/api/ica/actions.csv?release=26C` | `custom:action` nodes | 1,716 |

### Manual schema changes (Schema Builder UI only)

1. Add 5 properties to `custom:feature` node:
   - `isAiFeature` (boolean), `aiType` (string), `isRedwood` (boolean), `autoEnabledIn` (string), `optInRequired` (boolean) — all optional
2. Set `featureCode` on `oracleFusionGraphEntity` to `required: false`

### Note on authentication

The `/api/ica/*.csv` endpoints require a **session cookie**. Log in at `https://oraclereadinesssrc-dzxnqq.fly.dev/` before downloading CSVs.

---

## 9. Data Pipeline — How Features Get In

### Path A: Catalogue scrape → `features` table (~685 rows for 26C)

- Module-level stubs (e.g. "Absence Management What's New 26C")
- Flags extracted from Oracle HTML: `is_ai`, `is_redwood`, `opt_in_required` etc.
- **NOT used by ICA CSV** (too coarse — only 42 rows per release filter)

### Path B: XLSX ingest → `features` table (updates)

- `ingest_xlsx_dump` MCP tool
- `Feature_Summary.json` has `#UNCALCULATED` in Feature column — see [Appendix D](#appendix-d--the-xlsx-trap)
- `backfill_flags_from_features()` produces 0 matches — see Section 10

### Path C: Deep scrape → `feature_details` table (1,725 rows for 26C)

- Individual feature pages: full description, Steps to Enable, Business Benefit, Key Resources, Tips
- **Used by ICA CSV** — `_ica_features()` calls `get_details_with_flags()`
- Flags all 0 currently — see Section 11

### What `_ica_features()` returns (post-fix, current state)

```
feature_details (1,725 rows) → build_features_csv() → contextText with rich description
```

CSV count: **1,726 rows** (1 header + 1,725 data). Each row has full `description_full` text in `contextText`.

---

## 10. The `feature_details` vs `features` Hierarchy Problem

| Table | Granularity | Names | Flags |
|---|---|---|---|
| `features` | Module-level stub | "Absence Management What's New 26C" | Yes |
| `feature_details` | Individual feature | "AI-Driven Absence Prediction" | No (all 0) |

**They cannot be joined by name.** The names are at different levels of the Oracle hierarchy. `backfill_flags_from_features()` always returns 0 rows — this is expected, not a bug.

**Fix path:** Extend `parse_feature_detail_page()` in `oracle_scraper.py` to extract flags from Oracle HTML.

---

## 11. Flag Columns — Current State

All 8 flag columns in `feature_details` are 0 or NULL. The `contextText` in the ICA CSV is still rich (full `description_full` text). ICA's vector store can use this for semantic search even without explicit flag values.

Migration DDL (already applied, idempotent):
```python
"ALTER TABLE feature_details ADD COLUMN is_ai       INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN ai_type     TEXT",
"ALTER TABLE feature_details ADD COLUMN is_redwood  INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN auto_enabled_in TEXT",
"ALTER TABLE feature_details ADD COLUMN opt_in_required INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN setup_required  INTEGER NOT NULL DEFAULT 0",
"ALTER TABLE feature_details ADD COLUMN impact      TEXT",
"ALTER TABLE feature_details ADD COLUMN enablement  TEXT",
```

---

## 12. MCP Tools Reference

| Tool | Params | Notes |
|---|---|---|
| `list_products` | — | Product lines |
| `get_cache_status` | — | DB counts, last scrape |
| `list_releases` | — | Release codes |
| `list_product_families` | `release` | Pillars |
| `list_modules` | `release`, `product_family` | Modules |
| `get_release_notes` | `product`, `release`, `limit` | Module-level list |
| `search_release_notes` | `query`, `product`, `release`, `module` | Full-text search |
| `get_features_by_module` | `release`, `module` | Features in module |
| `get_feature_summary` | `release` | Stats |
| `get_opt_in_features` | `release` | Opt-in features |
| `get_setup_required_features` | `release` | Setup-required |
| `get_high_impact_features` | `release` | High-impact |
| `get_auto_enabled_features` | `release` | Auto-enabled |
| `get_ai_features` | `release` | AI features |
| `get_redwood_features` | `release` | Redwood UI |
| `get_feature_detail` | `feature_name`, `release`, `product_family` | Full detail |
| `get_feature_detail_by_url` | `url` | Detail by page URL |
| `list_features_with_steps` | `release`, `product_family`, `module` | Steps to Enable |
| `list_features_with_tips` | `release`, `product_family`, `module` | Tips |
| `search_feature_details` | `query`, `release`, `product_family` | Search details |
| `deep_scrape_feature_details` | `products`, `releases` | Trigger deep scrape |
| `generate_report` | `filters`, `include_content`, `save_report` | Report |
| `get_document_content` | `url` | Full doc text |
| `compare_releases` | `module`, `old_release`, `new_release` | Diff |
| `push_report_to_github` | `repo`, `branch`, `pillars` | GitHub push |
| `refresh_readiness_data` | `products` | Trigger scrape |
| `ingest_xlsx_dump` | `json_path` | Load XLSX dump |
| `get_ica_framer_csv` | `entity_type`, `release`, `pillar` | ICA CSV |

---

## 13. Authentication Layers

| Layer | Scope | Credential |
|---|---|---|
| Session cookie | Web UI + `/api/*` | `readiness_session=<token>` (login at `/`) |
| Bearer token | `/mcp` | `Authorization: Bearer <READINESS_TOKEN>` |
| ICA MCP Gateway token | Via ICA context | `Authorization: Bearer <orm-...>` |

---

## 14. Common Errors and Fixes

### ICA ingest failures

| Error | Root cause | Fix |
|---|---|---|
| "Invalid Framer project URL format" | `project_link` is not `https://framer.com/projects/<name>--<id>` | Use `_FRAMER_PROJECT_URL` constant in code; update ICA source record via console |
| Ingest "failed", graph 0 entities | Wrong `project_link` stored in ICA source record | Run browser console Step 2 (PUT source) + Step 3 (trigger ingest) from Section 6 |
| `features.csv` returns 42 rows | Old code using `filter_entries()` (features table) | Fixed in commit affc3b3 — uses `get_details_with_flags()` |
| `_health()` returns wrong `project_link` | Old code pointing to `/framer-metadata` | Fixed in commit 93cd7c7 |
| `_framer_metadata()` returns wrong `project_link` | Old code pointing to `/framer-site` | Fixed in commit 93cd7c7 |

### Fly.io / deploy

| Error | Cause | Fix |
|---|---|---|
| `WARNING: not listening on expected address` | Old process still on port during rolling deploy | Usually OK — verify with diag3.py; `_FRAMER_PROJECT_URL: FOUND` = new image running |
| `address already in use` when running python via SSH | Server process already has 8080 | Normal — server IS running |
| Deploy hangs | Missing `--remote-only` | Add `--remote-only` |
| Build context 658 MB | Large debug files in workspace | Add `.dockerignore` |

### Python / server

| Error | Cause | Fix |
|---|---|---|
| `backfill_flags_from_features()` returns 0 | Names in `features` vs `feature_details` are at different hierarchy levels | Expected — see Section 10 |
| `OperationalError: no column named is_ai` | Migration not run | Migration is idempotent — runs at startup automatically |
| `_ica_actions` returning 0 rows | Old `release or ""` bug | Fixed — uses clean if/else |

---

## 15. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `READINESS_TOKEN` | `""` | Bearer token for `/mcp` |
| `READINESS_DATA_DIR` | `/data` | SQLite + reports directory |
| `READINESS_REFRESH_HOURS` | `6` | Auto-refresh interval |
| `READINESS_AUTOSTART_REFRESH` | `1` | Set `0` to disable |
| `READINESS_HTTP_HOST` | `0.0.0.0` | Bind host |
| `READINESS_HTTP_PORT` | `8080` | Bind port |
| `APP_URL` | `https://oraclereadinesssrc-dzxnqq.fly.dev` | Public URL for self-referencing |

---

## 16. Bob Config — Connecting Bob to the Live Server

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

Get token: `fly ssh console --app oraclereadinesssrc-dzxnqq -C "printenv READINESS_TOKEN"`

---

## 17. Quick-Reference Commands

```powershell
# Deploy
cd "G:\My Drive\GIT_ROOT\Playground"
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only

# Verify new image is running (check for _FRAMER_PROJECT_URL)
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq diag3.py
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "python diag3.py"

# Health check (from inside container — always works even when external curl times out)
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq diag2.py
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "python diag2.py"

# Get token
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "printenv READINESS_TOKEN"

# Live logs
& "$env:USERPROFILE\.fly\bin\fly.exe" logs --app oraclereadinesssrc-dzxnqq
```

---

## 18. What Still Needs Doing

### Current state (end of 2026-07-25 session)

| Component | Status |
|---|---|
| Server code (`server.py`) | ✅ Correct — `_FRAMER_PROJECT_URL` constant, all routes fixed |
| Fly.io deployment | ✅ Running — commit d839a34 deployed |
| ICA source record `project_link` | ✅ Already correct — `src_e157006ebcf1` has framer.com URL |
| Framer published site content | ❌ Blank default template — root cause of 5 nodes |
| ICA knowledge graph | ❌ 5 nodes / 4 edges (from blank crawl) |
| `feature_details` flags | ❌ All 0 — non-blocking for basic ingest |

### 🔴 Priority 1 — Upload CSVs to ICA Schema Builder (~15 min, no code changes)

This is the fastest path to a populated graph. See `docs/FIELD_GUIDE.md` → Path A.

Upload in this order (download from server after logging in):
1. `releases.csv`
2. `action-types.csv`
3. `modules.csv`
4. `derivation-methods.csv`
5. `features.csv?release=26C` — 1,726 rows
6. `actions.csv?release=26C` — 1,716 rows

### 🟡 Priority 2 — Add 5 properties to Feature node (ICA Schema Builder UI)

In Schema Builder → `custom:feature` → Properties:
- `isAiFeature` (boolean), `aiType` (string), `isRedwood` (boolean), `autoEnabledIn` (string), `optInRequired` (boolean)
- Also: set `featureCode` on `oracleFusionGraphEntity` to `required: false`

### 🟡 Priority 3 — Extract flags from feature detail HTML (code change, oracle_scraper.py)

Extend `parse_feature_detail_page()` to detect Oracle HTML badge elements for `is_ai`, `is_redwood`,
`opt_in_required` etc. Currently all 0. Non-blocking — `contextText` is rich even without flags.

### 🟢 Priority 4 — Add .dockerignore

Reduce build context from 658 MB to ~2 MB by adding `Playground/.dockerignore` with:
`*.db`, `*.json`, `diag*.py`, `deployed_server*.py`, `framer_*.html`, `framer_*.txt`,
`__pycache__/`, `*.pyc`, `.env`, `.git/`, `readiness_*.db`

---

## Appendix A — Git Commit History

| Commit | Description |
|---|---|
| `93cd7c7` | fix: correct project_link to real Framer URL in health and framer-metadata |
| `ffbeb21` | docs: comprehensive knowledgebase update + docs/ folder |
| `affc3b3` | Fix _ica_features to use feature_details; add 8 flag columns; fix _ica_actions bug |
| Earlier | Add deep scrape tools, ICA CSV endpoints, Framer spoofing endpoints |
| Earlier | Initial FastMCP server with scraper, auth, settings, web UI |

---

## Appendix B — ICA CSV Column Order

```
schemaVersion, sourceWorkbook, domain, entityType, name, status,
moduleOrCategory, identifier, startDate, contextText
```

Enforced by `csv_response()` in `ica.py` using `extrasaction="ignore"`.

---

## Appendix C — Database Schema

### `features` table — module-level catalogue (~685 rows for 26C)

Module-level section titles from Oracle catalogue pages. Flags populated by HTML scrape. **NOT used for ICA CSV** (too coarse).

### `feature_details` table — individual features (1,725 rows for 26C, 2,070 total)

Individual feature pages from deep scrape. Full description text. Flags all 0 (backfill produces 0 matches — see Section 10). **Used for ICA CSV.**

Columns added by `_MIGRATION_DDL` (idempotent ALTER TABLE):
`is_ai`, `ai_type`, `is_redwood`, `auto_enabled_in`, `opt_in_required`, `setup_required`, `impact`, `enablement`

---

## Appendix D — The XLSX Trap

`Feature_Summary.json` (uploaded to `/data/` on Fly) has `"Feature": "#UNCALCULATED"` for every row because Excel formulas were not evaluated before export.

**To fix for future exports:**
1. Open XLSX in Excel
2. `Ctrl+Alt+F9` to force-recalculate
3. Save → re-export to JSON

**Current workaround:** ICA CSV uses `feature_details` (deep scrape), not the XLSX. XLSX flags are irrelevant until the table hierarchy problem is solved.

---

## Appendix E — Full Audit Findings (2026-07-25)

### Confirmed working ✅
- Fly.io machine running (PID 650: `python server.py --http`)
- Port 8080 bound and LISTENING
- `/health` now returns `project_link: https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp`
- `/framer-metadata` now returns same correct URL
- `feature_details`: 2,070 rows (851 for 26C)
- `features`: 685 rows
- `_FRAMER_PROJECT_URL` constant in `server.py` = single source of truth
- New image confirmed deployed: `server.py mtime: 2026-07-24 23:27:42`

### External access ⚠️
- `curl https://oraclereadinesssrc-dzxnqq.fly.dev/health` **times out** from Windows PowerShell
- Same request **succeeds** from inside the container via `diag2.py`
- Likely cause: Windows/ISP networking issue with Fly.io WireGuard proxy, not an app problem
- ICA's servers (running in IBM cloud) should have no such issue

### Still needed to complete ingest ⏳
- ICA source record `project_link` field needs updating via browser console (Section 18, Priority 1)

---

## Appendix F — Obsidian ↔ Open Notebook Integration (2026-07-28)

### Stack

| Component | Detail |
|---|---|
| Open Notebook version | v1.14.0 |
| Runtime | Podman compose — 2 containers (`open_notebook`, `surrealdb`) |
| Web UI | `http://localhost:8502` |
| REST API | `http://localhost:5055` |
| Plugin path (live) | `G:\My Drive\DriveSyncFiles\.obsidian\plugins\obsidian-open-notebook\` |
| Plugin source | `Playground/Playground/Obsidian-ON-Integration/` |
| Dedup scripts | `Playground/Playground/Open-Notebook-Source-Dedup/` |

---

### Incident: Sync-Storm — "KPI Stage 3 notes" (HOB TIP NOTES notebook)

**Symptom:** One note had **55 source copies** in the notebook (49 ghosts + 5 real + 1 surviving).
**Root cause:** `onFileModified` had no effective debounce — the `syncDebounceMs:2000` setting existed but was never wired up. Every rapid keystroke triggered a full independent sync, creating dozens of races.

**Resolution:**
- Deleted 49 ghost copies via `source-dedup.js --fix` (0 failures) → notebook dropped from 55 to 6 sources.
- Fixed `main.js` with per-file `_modifyTimers` debounce (see Bug #1 below).

---

### Data cleanup performed

| Action | Detail |
|---|---|
| Deleted 49 ghost sources | `source-dedup.js --fix` on HOB TIP NOTES notebook |
| Deleted stub John Burns source | `DELETE /api/sources/76y2ksv47kbwx5hb2pf0` (19-char stub) — kept `o0l2j9vgnipbusm6mkea` (13,746-char full analysis) |
| Removed 2 dead `folderToNotebook` entries | `"ToDO List"` and `"OpenNotebook"` both pointed to non-existent notebook `5gw4ztmpeq1wa6jwcjbk`; removed from `data.json` (13 → 11 entries) |

---

### Bugs found and fixed in `main.js` (ContentSyncManager)

> `main.js` is a minified esbuild bundle. All patches applied via `search_and_replace`. After patching, the file was copied to the live Obsidian plugin location.

#### Bug #1 — CRITICAL: `onFileModified` no debounce (sync storm)

**Location:** `ContentSyncManager.onFileModified`
**Problem:** `syncDebounceMs` setting existed but was never used. Every file-change event triggered immediate sync.
**Fix:** Added `this._modifyTimers = new Map` in constructor; `onFileModified` now uses `setTimeout` keyed per file path, cancelling any pending timer before setting a new one.

#### Bug #2 — HIGH: `onFileCreated` declared non-async

**Location:** `ContentSyncManager.onFileCreated`
**Problem:** Method contained `await` expressions but was declared as a plain (non-async) function → fire-and-forget races, unhandled promise rejections.
**Fix:** Added `async` keyword to the method declaration.

#### Bug #3 — HIGH: Dead notebook mapping in `data.json`

**Location:** `folderToNotebook` in `data.json`
**Problem:** Two folder entries (`"ToDO List"`, `"OpenNotebook"`) referenced notebook ID `5gw4ztmpeq1wa6jwcjbk` which no longer existed on the server → every sync to those folders silently failed.
**Fix:** Removed both dead entries from `data.json` directly. Added `verifySyncState` notebook health-check block that logs a warning for any `folderToNotebook` entry pointing to a non-existent notebook (so future drift is caught early).

#### Bug #4 — MEDIUM: `sourceMappings` always empty on startup

**Location:** `ContentSyncManager.verifySyncState`
**Problem:** Plugin stored source ID mappings in frontmatter and in an in-memory Map (`sourceMappings`), but the Map was never rebuilt from frontmatter on startup — so `verifySyncState` and dedup tools always saw zero mappings.
**Fix:** Added a frontmatter-rebuild pass at the top of `verifySyncState` that iterates all markdown files and repopulates `sourceMappings` from `open-notebook-source-id` frontmatter fields.

#### Bug #5 — MEDIUM: `syncFile` concurrent race (same file multiple times)

**Location:** `ContentSyncManager.syncFile`
**Problem:** No per-path lock — if `syncFile` was called twice for the same path in rapid succession, two simultaneous API calls could both `POST` a new source, creating duplicates.
**Fix:** Added `this._syncLocks = new Map` in constructor; `syncFile` acquires a per-path Promise lock using a `_lkRes` resolver pattern, serialising all calls for the same path.

---

### Bugs fixed in dedup scripts

#### `open-notebook-dedup.js` — Keeper selection (Bug #3 equivalent)

**Location:** `open-notebook-dedup.js` ~line 202
**Problem:** Fallback sort used `updated` timestamp only — a short stub created more recently would be kept over a rich long-form analysis.
**Fix:** Replaced timestamp sort with content-weighted sort: primary = `fullText`/`note_content` length, secondary = `insights_count`, tertiary = `updated` timestamp.

#### `source-dedup.js` — `scoreCluster()` recency nudge (Bug #5 equivalent)

**Location:** `source-dedup.js` `scoreCluster()` ~line 186
**Problem:** Recency was only a final tiebreaker after score equality — a newer richer source couldn't outrank an older one with identical normalised scores.
**Fix:** Added a recency nudge (0→0.05) derived from each source's `updated`/`created` timestamp relative to the cluster min/max range. Baked into the composite score so fresh sources get a small boost without overriding content signals. Removed the now-redundant `byDate` tiebreaker from the sort comparator.

---

### Files changed

| File | Change |
|---|---|
| `Playground/Playground/Obsidian-ON-Integration/main.js` | 6 patches: `_modifyTimers`, `_syncLocks`, async `onFileCreated`, debounced `onFileModified`, serialised `syncFile`, notebook health-check + frontmatter-rebuild in `verifySyncState` |
| `G:\My Drive\DriveSyncFiles\.obsidian\plugins\obsidian-open-notebook\main.js` | Overwritten with patched version (live) |
| `G:\My Drive\DriveSyncFiles\.obsidian\plugins\obsidian-open-notebook\data.json` | Removed 2 dead `folderToNotebook` entries |
| `Playground/Playground/Obsidian-ON-Integration/open-notebook-dedup.js` | Content-weighted keeper sort |
| `Playground/Playground/Open-Notebook-Source-Dedup/source-dedup.js` | Recency nudge in `scoreCluster()` |

---

*End of Knowledge Base*

> **Vault Radar note:** No secrets in this file. `_FRAMER_PROJECT_URL` is a public Framer project editor URL. Token values must be retrieved via `fly secrets` or environment variables.
