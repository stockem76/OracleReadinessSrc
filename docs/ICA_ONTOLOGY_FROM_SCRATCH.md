# ICA Context Studio — Ontology From Scratch
## Complete guide: create a new context, define the schema, and load Oracle Readiness data

> **Last updated:** 2026-07-25  
> **See also:** [`FIELD_GUIDE.md`](FIELD_GUIDE.md) — hard-won debugging lessons  
> [`CONNECTOR.md`](../CONNECTOR.md) — ICA form values quick-reference

---

## Concepts

| Term | What it is |
|---|---|
| **Context** | A named knowledge graph + vector store. One context = one ontology. Ours: `26c Complete Ontology` |
| **Schema** | Node types, edge types, and enum lookups that define what can live in the graph |
| **Node / Entity** | A single record — e.g. one Oracle feature, one release, one module |
| **Enum** | Constrained list of allowed values — e.g. release codes, module names |
| **Upload Sample Data** | Schema Builder button that bulk-loads nodes via CSV. Despite the name, this is the production data load path |
| **Data Source** | A connected ingestion pipeline (Framer crawler, MCP server). Separate from Schema Builder CSV upload |
| **contextText** | Free-text field per CSV row that ICA embeds for semantic search. The most important column |
| **identifier** | Stable unique key per row. Used for deduplication on re-upload |

> **Two separate mechanisms:**
> - **CSV Upload** (Schema Builder) loads nodes directly — the reliable path
> - **Data Source / Framer connector** crawls a website — requires a populated published Framer site (currently blank → 5 nodes only)

---

## Phase 1 — Create the Context

*Do once. Skip if context already exists.*

1. Open Context Studio: `https://contextstudio.servicesessentials.ibm.com/?teamName=MattStocker&teamId=69aaae8a8482bc71f1c4af52&tab=context`
2. Confirm you're on the **MattStocker** team
3. Click **+ New Context**
4. Name: `26c Complete Ontology` | Description: `Oracle Fusion 26C release feature knowledge graph`
5. Click **Create**
6. Note the **Context ID** from the URL (e.g. `ctx_9baeb72e480b`) — it never changes

---

## Phase 2 — Build the Schema

*Manual UI steps — cannot be done via CSV. Do once per context.*

Open the context → **Schema Builder**.

### Step 1 — Set featureCode optional ⚠️ REQUIRED

> **Skip this and every upload will fail** with `featureCode is required`.

1. Find **oracleFusionGraphEntity** (the root abstract type)
2. Click its `featureCode` property
3. Change **Required**: `true` → `false`
4. Save

### Step 2 — Add Feature node type

**+ Add Node Type:**

| Field | Value |
|---|---|
| Name | `Feature` |
| Curie | `custom:feature` |
| Parent type | `oracleFusionGraphEntity` |

Then in the **Properties panel** for `custom:feature`, add these 5 optional properties:

| Display name | Curie | Type | Required |
|---|---|---|---|
| Is AI Feature | `custom:isAiFeature` | boolean | No |
| AI Type | `custom:aiType` | string | No |
| Is Redwood | `custom:isRedwood` | boolean | No |
| Auto Enabled In | `custom:autoEnabledIn` | string | No |
| Opt In Required | `custom:optInRequired` | boolean | No |

> These 5 properties cannot be uploaded via CSV — UI only. The CSV embeds these values in `contextText` for vector search regardless.

### Step 3 — Add Action node type

**+ Add Node Type:**

| Field | Value |
|---|---|
| Name | `Action` |
| Curie | `custom:action` |
| Parent type | `oracleFusionGraphEntity` |

No extra properties needed — `contextText` carries the full action content.

### Step 4 — Enum lookups

Three enums need extending — handled automatically by the CSV upload in Phase 4:
- `enum:oracleFusion26cReleaseCode` — release codes (26C, 26D, 27A…)
- `enum:oracleFusion26cModule` — Oracle module names
- `enum:oracleFusion26cActionType` — action types (Steps to Enable, Business Benefit…)

---

## Phase 3 — Prepare the CSVs

*No login required — all endpoints are public.*

### CSV column schema (all 6 files share this header)

```
schemaVersion,sourceWorkbook,domain,entityType,name,status,moduleOrCategory,identifier,startDate,contextText
```

| Column | Purpose |
|---|---|
| `schemaVersion` | Always `1` |
| `sourceWorkbook` | `OracleReadinessMCP` |
| `domain` | `OracleFusion26C` |
| `entityType` | `Feature`, `Action`, `Release`, `Module`, `DerivationMethod` |
| `name` | Human-readable display name |
| `status` | Always `active` |
| `moduleOrCategory` | Oracle module name for features; enum category for lookups |
| `identifier` | Stable unique key — e.g. `F-ai-driven-absence-prediction` |
| `startDate` | ISO 8601 — e.g. `2025-06-01T00:00:00Z` |
| `contextText` | **Most important.** Free text embedded by ICA vector store for semantic search |

> ⚠️ `contextText` must not contain `U+FFFD` (Unicode replacement character), null bytes, or C0 control characters — these crash ICA's `upsert_nodes`. The server strips these (commit `c3c4db3`). If you generate your own CSVs, apply the same cleaning.

### Download the 6 files

```powershell
$base = "https://oraclereadinesssrc-dzxnqq.fly.dev"

# Enum lookups — upload BEFORE entity CSVs
Invoke-WebRequest "$base/api/ica/releases.csv"           -OutFile releases.csv
Invoke-WebRequest "$base/api/ica/action-types.csv"       -OutFile action-types.csv
Invoke-WebRequest "$base/api/ica/modules.csv"            -OutFile modules.csv
Invoke-WebRequest "$base/api/ica/derivation-methods.csv" -OutFile derivation-methods.csv

# Entity nodes — the main data
Invoke-WebRequest "$base/api/ica/features.csv?release=26C" -OutFile features-26C.csv
Invoke-WebRequest "$base/api/ica/actions.csv?release=26C"  -OutFile actions-26C.csv
```

### Verify row counts

```powershell
(Get-Content features-26C.csv | Measure-Object -Line).Lines   # expect ~1726
(Get-Content actions-26C.csv  | Measure-Object -Line).Lines   # expect ~1716
```

> If `features-26C.csv` has ~42 rows, the server is returning module stubs. Feature names should look like *"AI-Driven Absence Prediction"* not *"Absence Management What's New 26C"*.

---

## Phase 4 — Upload Sample Data

*Upload order matters — enum lookups must exist before entities that reference them.*

### Step 1 — Open Schema Builder → Upload Sample Data

1. Context Studio → open **26c Complete Ontology**
2. Click **Schema Builder**
3. Click **Upload Sample Data**

### Step 2 — Upload in strict order

Upload one file at a time. Wait for each to complete.

| # | File | ICA target | Rows |
|---|---|---|---|
| 1 | `releases.csv` | `enum:oracleFusion26cReleaseCode` | 3 |
| 2 | `action-types.csv` | `enum:oracleFusion26cActionType` | 2 |
| 3 | `modules.csv` | `enum:oracleFusion26cModule` | ~12 |
| 4 | `derivation-methods.csv` | `enum:oracleFusion26cMethod` | 1 |
| **5** | **`features-26C.csv`** | **`custom:feature` nodes** | **1,726** |
| **6** | **`actions-26C.csv`** | **`custom:action` nodes** | **1,716** |

> ⚠️ If you upload features/actions before the enum CSVs, the upload will fail or silently drop rows with unrecognised enum values.

### Step 3 — Confirm each upload

After each file, ICA shows: *"X nodes created, Y updated, Z errors"*

- **Errors = 0** → good
- **All rows = errors** → `featureCode` is still required (Phase 2 Step 1)
- **upsert_nodes crash** → Unicode characters in contextText (stripped by server since `c3c4db3`)

After all 6 files: **~3,460 nodes** in the graph.

---

## Phase 5 — Add MCP Data Source

*Wires up live MCP tools for agents. Separate from the CSV data load.*

### Step 1 — Add Framer connector

Context Studio → **Data Sources** → **+ Add data source** → **Framer**

| ICA form field | Value |
|---|---|
| Connection name | `Oracle Readiness MCP` |
| Connection URL | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
| MCP URL | `https://oraclereadinesssrc-dzxnqq.fly.dev/mcp` |
| Project ID | `oraclereadinesssrc-dzxnqq` |
| Project name | `Oracle Readiness MCP` |
| Token / Bearer secret | *(value of `READINESS_TOKEN`)* |
| Re-Ingestion Required | ☑ checked |

Get the Bearer token:
```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "printenv READINESS_TOKEN"
```

> ⚠️ The Connection URL **must** be `https://framer.com/projects/<Name>--<ID>`. ICA validates this against a regex. Fly.io URLs are rejected. The Framer connector will crawl the (currently blank) Framer site and produce only 5 nodes — that's expected. Use the CSV upload for the real data.

### Step 2 — Wire up MCP Gateway for agents

Add to your Bob / agent MCP config:

```json
"context-studio": {
  "type": "streamable-http",
  "url": "https://servicesessentials.ibm.com/mcp-gateway/service/gateway/servers/8ccdd203bdee4014b08e82eedb6046e2/mcp",
  "headers": {
    "Authorization": "Bearer <ICA_GATEWAY_TOKEN>",
    "x-api-key": "ctx_9baeb72e480b"
  },
  "disabled": false
}
```

The `x-api-key` is the public Context ID. The Bearer token is your ICA API token from IBM Services Essentials → API Keys.

---

## Phase 6 — Verify

```powershell
# Server health
Invoke-WebRequest -Uri "https://oraclereadinesssrc-dzxnqq.fly.dev/health" -UseBasicParsing | Select-Object -ExpandProperty Content

# CSV row count (expect ~1726)
$r = Invoke-WebRequest "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" -UseBasicParsing
($r.Content -split "`n").Count
```

Test queries via Context Studio Chat or agent:
```
What Oracle Fusion 26C HCM features require opt-in to enable?
List all AI features in Oracle Fusion 26C for ERP.
What are the steps to enable the Payroll Costing Enhancement feature?
```

Expected: named features from the uploaded 1,726-row dataset.

---

## Repeating for New Releases (26D, 27A…)

1. Scraper auto-detects new releases every 6 hours — no action needed
2. Verify: `GET /api/ica/features.csv?release=26D` returns rows (within 6h of Oracle publishing)
3. Download: `features.csv?release=26D` and `actions.csv?release=26D`
4. Upload via Schema Builder → Upload Sample Data (steps 5 and 6 only — enums don't need re-uploading)

---

## Error Reference

| Error | Root cause | Fix |
|---|---|---|
| `featureCode is required` | `featureCode` property on abstract parent is `required: true` | Phase 2 Step 1 — set required to false |
| `upsert_nodes` crash / stack trace | `U+FFFD` or null bytes in contextText | Server strips these since `c3c4db3`. If generating own CSVs, strip `\x00`, `\ufffd`, C0 control chars |
| Unknown enum value errors | Feature/Action CSV uploaded before enum CSVs | Upload in order: releases → action-types → modules → derivation-methods → features → actions |
| ~42 rows in features CSV | Server returning module stubs not individual features | Fixed in `affc3b3`. Verify names like "AI-Driven Absence Prediction" not "Absence Management What's New 26C" |
| 5 nodes after Framer ingest | Framer published site is blank default template | Use CSV upload (Phase 4) — the Framer connector path requires populated Framer site |
| `Invalid Framer project URL format` | Connection URL is not `framer.com/projects/<Name>--<ID>` | Use: `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
| MCP Gateway 401 | Wrong/expired ICA Gateway Bearer token | Get fresh token from IBM Services Essentials → API Keys |
| Flags all 0 | Rows scraped before flag extraction (`c65fcb0`) | Re-run deep scrape via MCP tool |
| Deploy hangs | Missing `--remote-only` on Windows | `fly deploy --remote-only` |

---

## Key IDs

| Thing | Value |
|---|---|
| Context Studio URL | `https://contextstudio.servicesessentials.ibm.com/?teamName=MattStocker&teamId=69aaae8a8482bc71f1c4af52&tab=context` |
| ICA context ID | `ctx_9baeb72e480b` |
| ICA team | MattStocker (`69aaae8a8482bc71f1c4af52`) |
| ICA source ID | `src_e157006ebcf1` *(changes if deleted/re-added)* |
| ICA MCP gateway server | `8ccdd203bdee4014b08e82eedb6046e2` |
| Server URL | `https://oraclereadinesssrc-dzxnqq.fly.dev` |
| MCP endpoint | `https://oraclereadinesssrc-dzxnqq.fly.dev/mcp` |
| Framer project URL | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
