# Oracle Readiness MCP Server — Unified Podman Solution

A single Podman container that:
1. **Periodically scrapes** Oracle Cloud Applications readiness pages (HCM, ERP, SCM, Service, News)
2. **Persists** rich feature data to a SQLite database
3. **Exposes an MCP server** over Streamable HTTP (or stdio)

## What's inside

| File | Role |
|------|------|
| `oracle_scraper.py` | Dual-strategy scraper: docs.oracle.com catalogue (markdownify) + oracle.com what's-new tables (BeautifulSoup). Merges both into a rich feature schema. |
| `db.py` | SQLite persistence — features, crawl log, document content cache |
| `server.py` | FastMCP server — 18 tools covering list/search/filter/compare/report/content |
| `scheduler.py` | Standalone refresh script for cron / sidecar use |
| `Dockerfile` | Slim Python 3.12 image (OCI-compatible) |
| `docker-compose.yml` | Single-service podman-compose file with named volume |

## MCP Tools provided

| Tool | Description |
|------|-------------|
| `list_products` | List tracked Oracle Cloud pillars |
| `get_cache_status` | Per-product refresh times, entry counts, content cache stats |
| `list_releases` | All indexed release codes (26C, 26B…) |
| `list_product_families` | Product families, optionally filtered to a release |
| `list_modules` | Modules for a release / product family |
| `get_release_notes` | All entries for a pillar, optionally filtered by release |
| `search_release_notes` | Full-text search across titles, descriptions, modules |
| `get_features_by_module` | All features for a release + module |
| `get_feature_summary` | Statistical summary (impact, setup, AI, Redwood counts) |
| `get_opt_in_features` | Features requiring Opt-In action |
| `get_setup_required_features` | Features requiring Setup configuration |
| `get_high_impact_features` | Large-scale impact features |
| `get_auto_enabled_features` | Features auto-enabling in a future update |
| `get_ai_features` | AI / Agent / Generative features |
| `get_redwood_features` | Features using Oracle Redwood UI |
| `compare_releases` | Diff two releases for a module (added/changed/removed) |
| `generate_report` | Filtered report (Pillar/Module/Update/query) with optional full document text |
| `get_document_content` | Download + cache full text of a readiness document (HTML→MD or PDF text) |
| `refresh_readiness_data` | Trigger an immediate refresh instead of waiting for the schedule |
| `ingest_xlsx_dump` | Load features from an Oracle Readiness Reports Centre XLSX JSON dump |

## Quick start

```bash
# 1. Clone / copy this directory
cd unified-docker

# 2. Copy and (optionally) edit the env file
cp .env.example .env

# 3. Build and start
podman-compose up -d --build

# 4. Check logs
podman-compose logs -f oracle-readiness-mcp

# 5. Test the health endpoint
curl http://localhost:8080/health
```

## Connecting an MCP client

### Claude Desktop / Claude Code (stdio via Podman)

```json
{
  "mcpServers": {
    "oracle-readiness": {
      "command": "podman",
      "args": ["exec", "-i", "oracle-readiness-mcp", "python", "server.py"]
    }
  }
}
```

### HTTP (Streamable MCP)

```
MCP URL: http://localhost:8080/mcp
```

If `READINESS_TOKEN` is set:
```
Authorization: Bearer <your-token>
```

## ICA (Context Studio) Connector Configuration

| Field | Value |
|-------|-------|
| **Name the connection** | `Oracle Readiness MCP` |
| **Choose a data source** | `Others` |
| **DataSource Type** | `framer` |
| **Connection URL** | *(leave blank — not used for MCP)* |
| **Connection Token** | `<your READINESS_TOKEN>` (or leave blank if no auth) |
| **MCP URL** | `http://<podman-host>:8080/mcp` |
| **Re-Ingestion Required** | ☐ (unchecked — server keeps its own refresh schedule) |

> Replace `<podman-host>` with your Podman host IP or hostname.
> On a local machine: `http://localhost:8080/mcp` (Podman runs rootless — no special hostname needed).

## Data persistence

Data is stored in a named Podman volume `oracle-readiness-data` at `/data` inside the container:
- `/data/readiness.db` — SQLite feature database
- `/data/reports/` — generated report files (JSON + Markdown)

## Manual refresh

```bash
# Refresh all products immediately
podman exec oracle-readiness-mcp python scheduler.py

# Refresh only HCM and ERP
podman exec oracle-readiness-mcp python scheduler.py hcm erp
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `READINESS_DATA_DIR` | `/data` | Data directory for SQLite DB and reports |
| `READINESS_REFRESH_HOURS` | `6` | Hours between background refreshes |
| `READINESS_AUTOSTART_REFRESH` | `1` | Set to `0` to disable background refresh |
| `READINESS_HTTP_PORT` | `8080` | HTTP port to bind |
| `READINESS_TOKEN` | *(empty)* | Bearer token for HTTP auth |
