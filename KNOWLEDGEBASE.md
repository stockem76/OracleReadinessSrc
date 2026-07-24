# Oracle Readiness MCP — Knowledge Base

> **Purpose:** Consolidated lessons-learned, architecture reference, and operational
> guide for anyone who has to build on, deploy, or debug this project.  Written from
> ~6 hours of live session experience across all layers of the stack.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Architecture Deep-Dive](#3-architecture-deep-dive)
4. [Deployment — Fly.io](#4-deployment--flyio)
5. [ICA Context Studio Integration](#5-ica-context-studio-integration)
6. [ICA Framer Connector — The Hard Part](#6-ica-framer-connector--the-hard-part)
7. [MCP Tools Reference](#7-mcp-tools-reference)
8. [Authentication Layers](#8-authentication-layers)
9. [Data Pipeline — How Features Get In](#9-data-pipeline--how-features-get-in)
10. [Common Errors and Fixes](#10-common-errors-and-fixes)
11. [Environment Variables](#11-environment-variables)
12. [Bob Config — Connecting Bob to the Live Server](#12-bob-config--connecting-bob-to-the-live-server)
13. [Quick-Reference Commands](#13-quick-reference-commands)

---

## 1. Project Overview

**oracle-readiness-mcp** is a Python 3.12 FastMCP server that:

- Periodically **scrapes** Oracle Cloud Applications readiness pages
  (`docs.oracle.com/en/cloud/saas/readiness` + `oracle.com` what's-new tables)
- Persists rich feature metadata to a **SQLite** database (`/data/readiness.db`)
- Exposes **20+ MCP tools** over Streamable HTTP (and optionally stdio)
- Serves a **web UI** at `/` (login-gated) for monitoring and admin
- Exports **ICA Framer CSV** endpoints so IBM Context Studio can ingest features
  directly into the *26c Complete Ontology* knowledge graph

The live instance runs on **Fly.io** at:
```
https://oraclereadinesssrc-dzxnqq.fly.dev
```

---

## 2. Repository Layout

```
Playground/
├── server.py          Main FastMCP server + all HTTP routes + web UI wiring
├── oracle_scraper.py  Dual-strategy scraper (docs catalogue + what's-new tables)
├── db.py              SQLite wrapper — features, crawl log, content cache, details
├── scheduler.py       Standalone refresh script (cron/sidecar use)
├── settings.py        JSON-persisted runtime config (survives restarts)
├── auth.py            User auth, sessions, audit log (bcrypt, SQLite)
├── ica.py             ICA Framer CSV builders — 6 entity types
├── analyse_26c.py     Ad-hoc analysis script run inside the container
├── static/
│   └── index.html     Single-page web UI (login + dashboard)
├── Dockerfile         python:3.12-slim, non-root user, /data volume
├── docker-compose.yml Podman/Docker single-service compose
├── fly.toml           Fly.io Machines V2 config (lhr region, 256 MB)
├── requirements.txt   Python deps
├── .env.example       Env var template
├── README.md          Quick-start and tool list
├── CONNECTOR.md       ICA Context Studio connection reference (keep updated)
└── oracle-readiness-mcp/
    └── mcp/           TypeScript MCP server (earlier prototype, now superseded
        └── src/       by server.py — kept for reference)
```

---

## 3. Architecture Deep-Dive

### 3.1 Python server (`server.py`)

The Starlette app is built around three layers:

```
[Starlette app]
  ├── _SessionAuthMiddleware   — cookie-based UI session gating
  ├── _McpAcceptMiddleware     — injects Accept header for ICA framer compat
  ├── _McpBearerMiddleware     — Bearer token check for /mcp
  └── routes
        /           → static/index.html  (login-gated)
        /health     → open (no auth)
        /framer-metadata → open (ICA connector discovery)
        /framer-site → Framer page spoof for ICA validator
        /sitemap.xml → open
        /mcp        → FastMCP StreamableHTTP transport
        /api/ica/*  → CSV export endpoints (open — no auth required)
        /api/*      → REST API (session-gated)
```

**Key design decision:** The `/api/ica/` endpoints are intentionally **open** (no
authentication), because the ICA framer ingest crawler does not send credentials.
The data is read-only Oracle public information, so this is acceptable.

### 3.2 `_McpAcceptMiddleware`

ICA's framer connector sends requests without the `application/json` Accept header
that FastMCP's Streamable HTTP requires. This middleware injects it:

```python
if scope["path"] == "/mcp" and b"accept" not in [h[0] for h in headers]:
    headers.append((b"accept", b"application/json, text/event-stream"))
```

Without this middleware the MCP transport returns 406 and ICA logs a 500.

### 3.3 `_McpBearerMiddleware`

Sits **inside** the Starlette router, only protecting `/mcp`. Reads
`READINESS_TOKEN` / settings `mcp_token` at request time (not startup) so token
rotation takes effect without a restart.

### 3.4 SQLite schema

Three logical groups of tables in `readiness.db`:

**Feature tables (db.py)**
```sql
features           — headline feature rows (release, module, flags, URLs)
crawl_log          — per-product crawl history
content_cache      — HTML/PDF full-text cache keyed by URL
feature_details    — rich detail pages (steps_to_enable, business_benefit,
                     key_resources, tips_considerations, optional_uptake,
                     description_full, access_requirements, other_sections)
```

**Auth tables (auth.py)**
```sql
users              — username, bcrypt hash, role, active flag
ui_sessions        — browser session tokens (24 h TTL)
audit_log          — every login/logout/user-change event
```

The DB uses WAL journal mode and foreign keys.

---

## 4. Deployment — Fly.io

### App details

| Property | Value |
|---|---|
| App name | `oraclereadinesssrc-dzxnqq` |
| Region | `lhr` (London) |
| Memory | 256 MB |
| CPU | 1 shared |
| Persistent volume | `oracle_readiness_data` → `/data` |
| Public URL | `https://oraclereadinesssrc-dzxnqq.fly.dev` |

### Deploy

```powershell
# From GIT_ROOT/Playground:
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only
```

`--remote-only` is **required** — the Docker build must run on Fly's builders
because the local environment is Windows and the image is Linux.

### Common deployment gotchas

| Problem | Fix |
|---|---|
| `legacy [[services]] block` | Use `[http_service]` (Machines V2 format) — done in fly.toml |
| `force_https = "true"` (string) | Must be boolean `true` — done |
| Image not pushed | Use `build: dockerfile: Dockerfile` not `image:` — done |
| Data lost between deploys | Volume `oracle_readiness_data` persists — data is safe |

### SSH into the running machine

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq
```

### Get the READINESS_TOKEN

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq `
    --command "printenv READINESS_TOKEN"
```

Or: `https://fly.io/apps/oraclereadinesssrc-dzxnqq/secrets`

### Run the analysis script inside the container

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq `
    --command "python analyse_26c.py"
```

---

## 5. ICA Context Studio Integration

### Context details

| Field | Value |
|---|---|
| Context name | `26c Complete Ontology` |
| Context ID | `ctx_9baeb72e480b` |
| Owner team | `MattStocker` (teamId `69aaae8a8482bc71f1c4af52`) |
| Source ID | `src_ef55df5d25d1` *(changes on delete/re-add)* |
| Context Studio URL | `https://contextstudio.servicesessentials.ibm.com/?teamName=MattStocker&teamId=69aaae8a8482bc71f1c4af52&tab=context` |

### MCP Gateway (Bob integration)

Add to Bob's MCP config:

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

The `x-api-key` is the context ID — it is **not** a secret and does not need rotation.
The `Authorization` Bearer token starts with `orm-` and is obtained from IBM
Services Essentials → API Keys.

---

## 6. ICA Framer Connector — The Hard Part

This section captures all the pain we went through to get ICA's framer ingest
working. Read this **before** touching the connector config.

### 6.1 What ICA's framer connector actually validates

ICA's `data-ingest` Python service validates `project_link` with a regex that
**only** accepts the canonical Framer project editor URL format:

```
https://framer.com/projects/<ProjectName>--<ProjectID>
```

**Confirmed working URL (project created 2026-07-24):**
```
https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp
```

**These formats are ALL rejected:**
- `https://oracle-readiness-mcp.framer.app` — published site subdomain → REJECTED
- `https://oraclereadinesssrc-dzxnqq.fly.dev/framer-site` — Fly.io URL → REJECTED
- `https://framer.com/m/...` — share/preview links → REJECTED

### 6.2 Form values for "Add data source" → "Framer"

| ICA form field | Value |
|---|---|
| Connection name | `Oracle Readiness MCP` |
| Connection URL / Project link | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
| MCP URL | `https://oraclereadinesssrc-dzxnqq.fly.dev/mcp` |
| Project ID | `oraclereadinesssrc-dzxnqq` |
| Project name | `Oracle Readiness MCP` |
| Token | `<READINESS_TOKEN>` |
| Re-Ingestion Required | ☑ checked |

> **Why `/framer-metadata` was previously tried as the URL:**
> We added `/framer-metadata` as a fallback discovery endpoint.  ICA's validator
> rejected it because it does not match the `framer.com/projects/` regex.
> The real Framer project URL must be used.

### 6.3 Updating the source record via console (if connector is stuck)

```javascript
// Update project_link on an existing source
await fetch('/data-ingest/sources/src_e157006ebcf1', {
  method:'PUT', credentials:'include',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({connection_details:{
    project_link: 'https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp'
  }})
});
```

```javascript
// Trigger ingest manually
const r = await fetch('/data-ingest/framer/ingest', {
  method:'POST', credentials:'include',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({source_id:'src_e157006ebcf1', context_id:'ctx_9baeb72e480b'})
});
console.log(r.status, await r.text());
// Expect: 202 {"status":"accepted","message":"Framer ingestion started..."}
```

```javascript
// Check ingest progress
const p = await (await fetch(
  '/data-ingest/framer-ingest/source/src_e157006ebcf1', {credentials:'include'}
)).json();
console.log(JSON.stringify(p, null, 2));
```

### 6.4 Why the `/framer-site` endpoint exists

ICA's crawler visits the `project_link` URL and checks the HTML for Framer
markers (`data-framer-hydrate-v2`, `__framer__` JS object, specific headers).
The `/framer-site` route on our server spoofs a full Framer page with these
markers so that ICA's validation passes even though the URL points at
`framer.com`, not our server.

This is only needed when the `project_link` still points to our Fly.io server.
With the real Framer project URL (`framer.com/projects/...`) ICA validates
against the real Framer CDN and `/framer-site` is not involved.

### 6.5 Why `/framer-metadata` still exists

`/framer-metadata` is a machine-readable JSON endpoint that documents the connector
form values. It is **not** the `project_link` — it is a convenience for humans
who need to re-add the connector without looking it up.

```bash
curl https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata
```

### 6.6 ICA Schema Builder — CSV upload flow

The `/api/ica/` endpoints return CSV files for each entity type.  They require
**no authentication** so ICA's crawler can read them.

| Endpoint | ICA target | Notes |
|---|---|---|
| `/api/ica/releases.csv` | `enum:oracleFusion26cReleaseCode` | 26D, 27A, 27B |
| `/api/ica/action-types.csv` | `enum:oracleFusion26cActionType` | Business Benefit, Key Resources |
| `/api/ica/modules.csv` | `enum:oracleFusion26cModule` | live modules + extras |
| `/api/ica/derivation-methods.csv` | `enum:oracleFusion26cMethod` | M017_MCP_FRAMER_INGESTION |
| `/api/ica/features.csv` | `custom:feature` nodes | 965+ features |
| `/api/ica/actions.csv` | `custom:action` nodes | steps, benefit, resources, tips |
| `/api/ica/schema-changes.json` | — | machine-readable change manifest |

Query params: `?release=26C`, `?pillar=HCM`

### 6.7 Manual schema changes still required in ICA UI

These **cannot** be done via CSV upload — they require the Schema Builder UI:

1. Add 5 properties to `custom:feature` node:
   - `isAiFeature` (boolean, optional)
   - `aiType` (string, optional)
   - `isRedwood` (boolean, optional)
   - `autoEnabledIn` (string, optional)
   - `optInRequired` (boolean, optional)

2. Set `featureCode` **optional** on abstract parent `oracleFusionGraphEntity`
   (change `required: true` → `required: false`) to prevent validation failures.

---

## 7. MCP Tools Reference

### Foundational / navigation

| Tool | Key params | Description |
|---|---|---|
| `list_products` | — | Lists tracked Oracle pillars (erp, hcm, scm, service) |
| `get_cache_status` | — | Per-product refresh times, entry counts |
| `list_releases` | — | All indexed release codes (26C, 26B…) |
| `list_product_families` | `release?` | Product families, optionally per release |
| `list_modules` | `release`, `product_family?` | Modules for a release |

### Feature retrieval

| Tool | Key params | Description |
|---|---|---|
| `get_release_notes` | `product`, `release?` | All entries for a pillar |
| `search_release_notes` | `query`, `product?`, `release?`, `module?` | Full-text search |
| `get_features_by_module` | `release`, `module` | All features for release + module |
| `get_feature_summary` | `release`, `module?` | Statistical summary with counts |

### Filtered views

| Tool | Key params | Description |
|---|---|---|
| `get_opt_in_features` | `release`, `module?` | Features requiring Opt-In |
| `get_setup_required_features` | `release`, `module?` | Setup-required features |
| `get_high_impact_features` | `release`, `module?` | Large-scale impact features |
| `get_auto_enabled_features` | `release`, `module?` | Will auto-enable in future update |
| `get_ai_features` | `release`, `module?` | AI/Agent/Generative features |
| `get_redwood_features` | `release`, `module?` | Redwood UI features |

### Deep detail

| Tool | Key params | Description |
|---|---|---|
| `get_feature_detail` | `feature_name`, `release`, `product_family` | Steps to Enable, Business Benefit, Tips, Key Resources |
| `get_feature_detail_by_url` | `url` | Lookup by feature_page_url |
| `list_features_with_steps` | `release?`, `product_family?`, `module?` | Features that have Steps to Enable |
| `list_features_with_tips` | same | Features with Tips and Considerations |
| `search_feature_details` | `query`, `release?`, `product_family?` | Search within detail sections |
| `deep_scrape_feature_details` | `products?`, `releases?` | Trigger deep scrape of detail pages |

### Reporting

| Tool | Key params | Description |
|---|---|---|
| `compare_releases` | `module`, `old_release`, `new_release` | Diff two releases |
| `generate_report` | `filters`, `include_content?`, `save_report?` | Full filtered report |
| `push_report_to_github` | `repo`, `pillars`, etc. | Push Markdown report to GitHub |
| `get_document_content` | `url` | Download + cache full HTML/PDF text |

### Data loading

| Tool | Key params | Description |
|---|---|---|
| `refresh_readiness_data` | `products?` | Trigger immediate scrape |
| `ingest_xlsx_dump` | `json_path`, `source_url?` | Load from XLSX JSON dump |

### ICA export

| Tool | Key params | Description |
|---|---|---|
| `get_ica_framer_csv` | `entity_type`, `release?`, `pillar?` | Returns CSV for ICA Schema Builder |

---

## 8. Authentication Layers

The server has **three independent auth mechanisms**:

### 8.1 Web UI (session cookie)

- Login at `/api/auth/login` with `{username, password}`
- Default admin: `admin` / `ReadinessAdmin1!` (first-boot only)
- Override with `READINESS_ADMIN_PASS` env var before first start
- Sessions expire after 24 h (configurable via `READINESS_SESSION_HOURS`)
- `_SessionAuthMiddleware` blocks all non-open paths without a valid cookie
- **Open paths** (no auth needed): `/health`, `/framer-metadata`, `/framer-site`,
  `/sitemap.xml`, `/api/auth/login`, `/`, `/api/ica/*`

### 8.2 MCP Bearer token (`READINESS_TOKEN`)

- Applies only to `/mcp`
- `_McpBearerMiddleware` reads the token from settings at request time
- Clients send: `Authorization: Bearer <READINESS_TOKEN>`
- If `READINESS_TOKEN` is empty, `/mcp` is open to anyone

### 8.3 ICA MCP Gateway token

- Separate from the above — managed by IBM Services Essentials
- `orm-` prefixed token obtained from IBM Services Essentials → API Keys
- Used only when accessing the context via the MCP Gateway URL

---

## 9. Data Pipeline — How Features Get In

### Path A: Automatic scraper

```
scheduler.py / _do_refresh() in AppState
  └── fetch_product(client, product)  [oracle_scraper.py]
        ├── fetch_product_catalogue()  → docs.oracle.com/readiness
        │     HTML → markdownify → _parse_catalogue_markdown()
        └── fetch_whats_new()  → oracle.com/applications/whats-new
              HTML → BeautifulSoup → _parse_whats_new_html()
  └── db.upsert_features(features)   [db.py]
```

Runs every `READINESS_REFRESH_HOURS` (default 6 h) in a background asyncio task.

### Path B: XLSX dump ingest

Oracle provides a downloadable XLSX from the Readiness Reports Centre.
Bob's `xlsx-dump` tool converts it to JSON. Then:

```
ingest_xlsx_dump MCP tool
  └── parse_features_from_xlsx_dump(rows, headers)  [oracle_scraper.py]
  └── db.upsert_features(features)
```

The XLSX JSON dump for 26C lives at:
```
.bob/tmp/xlsx-dumps/Custom Report_7_20_2026-9fb204ec2e969ccf/Feature_Summary.json
```

### Path C: Deep scrape (feature detail pages)

After basic features are loaded, the detail pages (Steps to Enable etc.) are
fetched separately:

```
deep_scrape_feature_details MCP tool / deep_scrape_product()
  └── fetch_feature_details_for_module()  [oracle_scraper.py]
        └── parse_feature_detail_page()
  └── db.upsert_feature_detail(detail)
```

The `feature_details` table has 965 rows for 26C after a full deep scrape.

### Feature data shape

```python
@dataclass
class Feature:
    release:         str
    product_family:  str       # erp / hcm / scm / service / news
    product:         str
    module:          str
    feature_name:    str
    description:     str
    impact:          str | None    # "Large scale" | "Small scale" | "Report"
    enablement:      str | None
    auto_enabled_in: str | None
    is_redwood:      bool
    is_ai:           bool
    ai_type:         str | None    # "Agent" | "Generative" | "Agentic App"
    setup_required:  bool
    opt_in_required: bool
    html_url:        str | None
    pdf_url:         str | None
    source_url:      str
    retrieved_at:    str
```

---

## 10. Common Errors and Fixes

### Fly.io / deployment

| Error | Fix |
|---|---|
| `Error: No machines in group` | `fly deploy --remote-only` — never omit this flag on Windows |
| `Error: app config is incompatible` | The old `[[services]]` block in fly.toml — replaced with `[http_service]` |
| `force_https type mismatch` | Must be boolean `true`, not string `"true"` |
| Container crashes on start | Check `fly logs --app oraclereadinesssrc-dzxnqq` |

### ICA Framer connector

| Error | Cause | Fix |
|---|---|---|
| `missing project_link` | `connection_details` empty | PUT the source record with `project_link` set |
| `Invalid Framer project URL format` | URL not matching `framer.com/projects/<Name>--<ID>` | Use the exact Framer project editor URL |
| `Failed to start ingestion: 401` | Wrong or missing Bearer token | Check `READINESS_TOKEN` via fly ssh |
| `Failed to start ingestion: 403` | Source belongs to different team | Open Context Studio under `MattStocker` team |
| MCP returns 406 | Missing `Accept` header from ICA crawler | `_McpAcceptMiddleware` handles this — check it is registered |
| ICA shows 500 on ingest | Usually the project_link validation failure | Use real `framer.com/projects/` URL |

### Server / MCP

| Error | Fix |
|---|---|
| `/mcp` returns 401 | `READINESS_TOKEN` not matching — confirm value with fly ssh |
| `/mcp` returns 403 | Session cookie auth blocking it — check `_is_open()` path list includes `/mcp` logic |
| `No features indexed yet` | Run `refresh_readiness_data` or `ingest_xlsx_dump` |
| `ica.py not found in container` | Ensure `ica.py` is in `COPY` list in Dockerfile — fixed in commit `821ed70` |

### Auth / web UI

| Error | Fix |
|---|---|
| Default password rejected | Override `READINESS_ADMIN_PASS` env var before first boot |
| Session not persisting | Check cookies are not blocked; sessions stored in `readiness.db` |
| Admin user locked out | Connect via fly ssh and DELETE from users table, then restart to re-seed |

---

## 11. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `READINESS_DATA_DIR` | `/data` | SQLite DB and reports directory |
| `READINESS_REFRESH_HOURS` | `6` | Hours between background refreshes |
| `READINESS_AUTOSTART_REFRESH` | `1` | Set to `0` to disable background refresh |
| `READINESS_HTTP_HOST` | `0.0.0.0` | HTTP bind address |
| `READINESS_HTTP_PORT` | `8080` | HTTP port |
| `READINESS_TOKEN` | *(empty)* | Bearer token for `/mcp`. Empty = open |
| `READINESS_ADMIN_PASS` | `ReadinessAdmin1!` | Bootstrap admin password (first boot) |
| `READINESS_SESSION_HOURS` | `24` | Web UI session TTL |
| `APP_URL` | `https://oraclereadinesssrc-dzxnqq.fly.dev` | Self-referential URL for metadata endpoints |
| `GITHUB_TOKEN` | *(empty)* | For push_report_to_github |
| `PYTHONUNBUFFERED` | `1` | Ensure logs are not buffered in container |

Settings stored in `settings.json` override environment variables after first boot.

---

## 12. Bob Config — Connecting Bob to the Live Server

### Direct MCP connection (recommended for development)

Add to `.bob/mcp.json`:

```json
{
  "oracle-readiness": {
    "type": "streamable-http",
    "url": "https://oraclereadinesssrc-dzxnqq.fly.dev/mcp",
    "headers": {
      "Authorization": "Bearer <READINESS_TOKEN>"
    }
  }
}
```

### Via ICA MCP Gateway (for production, context-enriched responses)

```json
{
  "context-studio": {
    "type": "streamable-http",
    "url": "https://servicesessentials.ibm.com/mcp-gateway/service/gateway/servers/8ccdd203bdee4014b08e82eedb6046e2/mcp",
    "headers": {
      "Authorization": "Bearer <ICA_GATEWAY_TOKEN>",
      "x-api-key": "<CONTEXT_ID>"
    }
  }
}
```

---

## 13. Quick-Reference Commands

### Deploy

```powershell
# Deploy to Fly.io (from Playground/ directory)
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only

# Check logs
& "$env:USERPROFILE\.fly\bin\fly.exe" logs --app oraclereadinesssrc-dzxnqq

# SSH into container
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq
```

### Health checks

```bash
# Quick liveness
curl https://oraclereadinesssrc-dzxnqq.fly.dev/health

# ICA connector reference
curl https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata

# Schema change manifest
curl https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/schema-changes.json | python -m json.tool
```

### Data loading

```bash
# Download features CSV for ICA
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" -o features_26C.csv

# Trigger immediate refresh (all pillars)
curl -X POST https://oraclereadinesssrc-dzxnqq.fly.dev/api/refresh \
     -H "Cookie: session=<token>" \
     -H "Content-Type: application/json" \
     -d '{}'
```

### Local podman (if not using Fly.io)

```bash
# Build and start
podman-compose up -d --build

# Check health
curl http://localhost:8080/health

# Manual refresh
podman exec oracle-readiness-mcp python scheduler.py
podman exec oracle-readiness-mcp python scheduler.py hcm erp
```

### Analysis inside container

```powershell
# Run the 26C analysis script
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq `
    --command "python analyse_26c.py 2>&1 | head -100"
```

---

## Appendix A — Git Commit History Summary

The commit history tells the story of the problems we solved:

| Commit | What it fixed |
|---|---|
| `921ab3a` | Initial release v1.0.0 |
| `104cd6f` | `force_https` must be boolean not string in fly.toml |
| `17db223` | Migrate from legacy `[[services]]` to `[http_service]` (Machines V2) |
| `a5abcea` | Build from Dockerfile instead of missing Docker Hub image |
| `b4e1eee` | Missing `target_releases` property in Settings class |
| `65f08e5` | Inject `Accept` header for ICA framer compatibility |
| `01ade78` | MCP-PROBE debug logging to diagnose ICA 500s |
| `d3683a1` | Remove debug logging after diagnosis done |
| `ec2322d` | Add auth layer (users/sessions/audit, login UI) |
| `e4b42c2` | Add TypeScript MCP server (oracle-readiness-mcp/ directory) |
| `9579ae3` | Add `/framer-metadata` endpoint + CONNECTOR.md |
| `c153024` | Allow `/framer-metadata` without session auth |
| `db86aa3` | Register `/framer-metadata` route in Starlette routes list |
| `0955070` | ICA Framer CSV export endpoints + `get_ica_framer_csv` MCP tool |
| `821ed70` | Add `ica.py` to Dockerfile COPY step (was missing!) |
| `c6b4fdc` | Drop REQUIRES_ACTION domain extension from schema changes |
| `53a8c49` | Add `/framer-site` page + `/sitemap.xml` for ICA Framer crawler |
| `1bfbeff` | Document ICA Context Studio MCP Gateway in CONNECTOR.md |
| `7cf11fd` | Spoof Framer response headers + `__framer__` JS markers |
| `83b21d1` | Add `data-framer-hydrate-v2` and full Framer DOM signatures |
| `f4f8c7d` | Document correct `framer.com/projects/<Name>--<ID>` URL format |
| `6b74779` | Record confirmed working Framer project URL |

---

## Appendix B — ICA CSV Column Order

Every CSV exported by `ica.py` must have these columns in this exact order:

```
schemaVersion, sourceWorkbook, domain, entityType, name, status,
moduleOrCategory, identifier, startDate, contextText
```

Values:
- `schemaVersion`: `"1"`
- `sourceWorkbook`: `"OracleReadinessMCP"`
- `domain`: `"OracleFusion26C"`
- `status`: `"active"`

---

*Last updated: 2026-07-24. Update this file whenever a significant problem is
solved or a new pattern is discovered.*
