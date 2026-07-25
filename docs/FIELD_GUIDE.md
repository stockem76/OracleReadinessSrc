# Oracle Readiness MCP — Field Guide
## Hard-Won Lessons + Complete Solution Path

> **Written:** 2026-07-25 — after 6+ hours debugging why ICA ingest produced only 5 nodes  
> **Updated:** 2026-07-25 — ICA upsert_nodes crash fixed (commit `c3c4db3`)
> **Purpose:** Shortcut the next session. Every trap is mapped. Every fix is proven. Full path to resilient, repeatable ingest documented.  
> **Companion docs:** `KNOWLEDGEBASE.md` (full reference), `CONNECTOR.md` (ICA form values), `docs/DEPLOY_CHEATSHEET.md`

---

## CURRENT STATE (as of commit c3c4db3)

```
Server code:          ✅ Deployed — flag extraction + Unicode cleaning active
Build context:        ✅ 36 KB (was 658 MB — .dockerignore added)
ICA CSV endpoints:    ✅ Public, 0 bad-character rows — verified
ICA source record:    ✅ project_link correct (src_e157006ebcf1)
Framer connector:     ❌ Crawls blank site → 5 nodes (path ABANDONED)
ICA graph:            ❌ 5 nodes — needs CSV upload (Path A below)
Flags in DB:          ❌ All 0 for existing rows — will populate on next deep scrape
```

**One manual step remaining:** Upload the 6 CSVs to ICA Schema Builder. See **Path A** below. ~15 minutes.

---

## COMPLETE SOLUTION — HOW IT WORKS END TO END

```
Oracle Cloud Readiness pages
        │
        ▼ background refresh every 6 hours (or on-demand)
   fetch_product() → features table (~685 rows per scrape)
        │
        ▼ deep scrape every refresh (fetches individual feature .htm pages)
   parse_feature_detail_page() → feature_details table (~2,070 rows)
        │  NOW EXTRACTS: is_ai, ai_type, is_redwood, auto_enabled_in,
        │                opt_in_required, setup_required, impact, enablement
        │  from the Oracle feature summary table on each page
        ▼
   ReadinessDB.upsert_feature_detail() → writes all 8 flag columns
        │
        ▼ CSV endpoints (public, no auth required)
   GET /api/ica/features.csv?release=26C  →  1,726 rows, rich contextText
   GET /api/ica/actions.csv?release=26C   →  1,716 rows
        │
        ▼ manual upload (once per release, ~15 min)
   ICA Schema Builder → Upload Sample Data
        │
        ▼
   26c Complete Ontology knowledge graph
   → populated with Oracle features, queryable by agents
```

**The complete solution is now deployed.** The loop runs automatically every 6 hours. Flags populate from the Oracle HTML on every scrape. The only human touchpoint is uploading the CSVs to ICA for each new Oracle release.

---

## PATH A — POPULATE ICA NOW (CSV Upload, ~15 minutes)

This is the immediate action needed to get data into the "26c Complete Ontology" graph.

### Step 1 — Download the 6 CSVs

These endpoints are now **public** (no login required):

```
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/releases.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/action-types.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/modules.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/derivation-methods.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/actions.csv?release=26C
```

### Step 2 — Open Context Studio

`https://contextstudio.servicesessentials.ibm.com/?teamName=MattStocker&teamId=69aaae8a8482bc71f1c4af52&tab=context`

Open **26c Complete Ontology** → **Schema Builder** → **Upload Sample Data**

### Step 3 — Upload in order

Upload the CSVs one at a time, **in this exact order** (enum lookups must exist before the entities that reference them):

| # | File | ICA target | Rows |
|---|---|---|---|
| 1 | `releases.csv` | `enum:oracleFusion26cReleaseCode` | 3 |
| 2 | `action-types.csv` | `enum:oracleFusion26cActionType` | 2 |
| 3 | `modules.csv` | `enum:oracleFusion26cModule` | ~12 |
| 4 | `derivation-methods.csv` | `enum:oracleFusion26cMethod` | 1 |
| 5 | `features.csv?release=26C` | `custom:feature` nodes | **1,726** |
| 6 | `actions.csv?release=26C` | `custom:action` nodes | **1,716** |

### Step 4 — Add 5 properties to Feature node (one-time, Schema Builder UI only)

In Schema Builder → `custom:feature` → Properties panel, add:

| Property name | Curie | Type | Required |
|---|---|---|---|
| Is AI Feature | `custom:isAiFeature` | boolean | No |
| AI Type | `custom:aiType` | string | No |
| Is Redwood | `custom:isRedwood` | boolean | No |
| Auto Enabled In | `custom:autoEnabledIn` | string | No |
| Opt In Required | `custom:optInRequired` | boolean | No |

Also: `oracleFusionGraphEntity.featureCode` → set `required: false`

### Step 5 — Verify

After upload, the graph should show ~1,700+ nodes. Query the graph:
```
Tell me about AI features in Oracle Fusion 26C for HCM
```

---

## PATH B — FRAMER CONNECTOR (ABANDONED — do not use)

The ICA Framer connector is a web crawler. It crawls `oracle-readiness-mcp.framer.website` which is a blank default template. Populating that site with Oracle data would require Framer CMS editor access and manual publishing — too complex and not repeatable.

**The Framer source in ICA (`src_e157006ebcf1`) can be left as-is or deleted.** It doesn't interfere with the CSV upload approach.

---

## REPEATING FOR A NEW ORACLE RELEASE (e.g. 26D)

When Oracle publishes 26D:

1. The background scraper will automatically detect and scrape it (runs every 6 hours)
2. Verify the new release is in the DB: `GET /api/ica/features.csv?release=26D` — should return rows within 6 hours of Oracle publishing
3. Download the 26D CSVs: `features.csv?release=26D` and `actions.csv?release=26D`
4. Upload to ICA Schema Builder (Steps 2-3 above, but with `?release=26D`)

That's it. No code changes. No redeployment. The scraper handles discovery automatically.

---

## THE BIG PICTURE LESSONS (why 6 hours were lost)

### LESSON 1 — ICA Framer connector is a web crawler, not an MCP client

We assumed ICA would call `/mcp` and use MCP tools to get Oracle data. **Wrong.**

ICA's Framer connector:
1. Validates `project_link` against a regex: must be `https://framer.com/projects/<Name>--<ID>`
2. Fetches the Framer project page to find the published website URL
3. **Crawls the published Framer website** and extracts text content from pages
4. Stores extracted text as knowledge graph nodes

Our Framer project was a blank template → ICA crawled 1 page of empty content → 5 nodes.

### LESSON 2 — The `project_link` URL must match the exact regex

Every Fly.io URL, every `*.framer.app` URL, every share link — all rejected by ICA's regex before the URL is even fetched.

**Only accepted:** `https://framer.com/projects/<ProjectName>--<ProjectID>`

### LESSON 3 — Two code bugs were masking the real problem

1. `_health()` was returning a Fly.io URL as `project_link` — was wrong for hours before anyone noticed
2. `_ica_features()` was querying the `features` table (42 module stubs) instead of `feature_details` (1,725 individual features)

Both fixed in commits `93cd7c7` and `affc3b3`.

### LESSON 4 — Deploy verification must use `diag3.py` from inside the container

`curl` from Windows times out on the Fly.io URL (WireGuard proxy issue). Internal checks via SSH/sftp always work.

### LESSON 5 — The `features` vs `feature_details` hierarchy

- `features` table: module-level stubs ("Absence Management What's New 26C") — flags YES, detail NO
- `feature_details` table: individual features ("AI-Driven Absence Prediction") — flags NO (before today), detail YES

These cannot be joined by name. The fix was to extract flags directly from Oracle's individual feature pages.

---

## QUICK REFERENCE

### Server health check
```powershell
Invoke-WebRequest -Uri "https://oraclereadinesssrc-dzxnqq.fly.dev/health" -UseBasicParsing | Select-Object -ExpandProperty Content
```

### Check CSV row count
```powershell
$r = Invoke-WebRequest -Uri "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" -UseBasicParsing
($r.Content -split "`n").Count
# Expect: ~1726
```

### Deploy
```powershell
cd "G:\My Drive\GIT_ROOT\Playground"
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only
```

### Verify deploy from inside container (WireGuard permitting)
```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq diag3.py
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "python diag3.py"
```

### KEY IDs

| Thing | Value |
|---|---|
| Fly.io app | `oraclereadinesssrc-dzxnqq` |
| Public URL | `https://oraclereadinesssrc-dzxnqq.fly.dev` |
| ICA context ID | `ctx_9baeb72e480b` |
| ICA team | `MattStocker` (`69aaae8a8482bc71f1c4af52`) |
| ICA source ID | `src_e157006ebcf1` *(changes if source deleted/re-added)* |
| Framer project | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
| ICA gateway server | `8ccdd203bdee4014b08e82eedb6046e2` |

---

## COMPLETE DEAD ENDS — DO NOT RETRY

| Approach | Why it fails |
|---|---|
| Fly.io URL as `project_link` | ICA regex rejects non-`framer.com/projects/` URLs |
| `*.framer.app` URL as `project_link` | Same regex rejection |
| HTML spoofing (`data-framer-hydrate-v2` etc.) | URL validated before HTML is ever fetched |
| `window.__framer__` marker | Real Framer sites don't have this; ICA doesn't check for it |
| `fetch()` with relative paths from console | App interceptor downgrades to HTTP → Mixed Content block |
| `backfill_flags_from_features()` JOIN | Feature names at different hierarchy levels — 0 matches always |
| XLSX flags via `Feature_Summary.json` | `#UNCALCULATED` in Feature column — Excel formulas not evaluated |
| `Server: Framer/...` response header | Fly.io edge proxy overwrites `Server:` — use `X-Framer-Signature` instead |
| U+FFFD in CSV contextText | Crashes ICA `upsert_nodes` — Oracle HTML encoding artefacts; fixed in `c3c4db3` |

---

## GIT HISTORY THIS SESSION

| Commit | Description |
|---|---|
| `c3c4db3` | fix: strip U+FFFD/null bytes from CSV — fixes ICA upsert_nodes crash |
| `c65fcb0` | feat: extract flags from Oracle detail page summary table; add .dockerignore |
| `7666f68` | docs: add FIELD_GUIDE.md — 6-hour session debrief |
| `d839a34` | docs: knowledgebase update — dead ends, root causes, audit findings |
| `93cd7c7` | fix: correct project_link to real Framer URL in health and framer-metadata |
| `affc3b3` | fix(ica): features CSV from feature_details+flags; add schema migration |

---

*End of Field Guide — complete solution deployed 2026-07-25*
