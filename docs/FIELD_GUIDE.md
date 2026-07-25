# Oracle Readiness MCP — Field Guide
## Hard-Won Lessons from 6+ Hours of Debugging

> **Written:** 2026-07-25 — after a full session diagnosing why ICA ingest produced only 5 nodes  
> **Purpose:** Shortcut the next session. Every trap is mapped. Every fix is proven. No repeating mistakes.  
> **Companion docs:** `KNOWLEDGEBASE.md` (full reference), `CONNECTOR.md` (ICA form values), `docs/DEPLOY_CHEATSHEET.md`

---

## The Big Picture in One Paragraph

This project is a FastMCP Python server on Fly.io that scrapes Oracle Cloud "What's New" pages and serves
feature data to IBM Consulting Advantage (ICA) Context Studio, which stores features in a knowledge graph
called "26c Complete Ontology". The critical integration point is ICA's **Framer connector** — a web crawler
that ICA calls to ingest content. We spent 6+ hours discovering that this connector is NOT an MCP client:
it is a **web crawler** that only works with a published Framer website. The Framer project we created
(`oracle-readiness-mcp`) was never populated with content, so ICA crawled 1 blank page and produced 5 nodes.
The server code is now correct. The remaining work is to populate the knowledge graph — either via CSV upload
(fastest) or by publishing content to the Framer site (harder).

---

## LESSON 1 — The ICA Framer Connector Is A Web Crawler, Not An MCP Client

**This is the single most important lesson of the entire session.**

We assumed ICA would call our `/mcp` endpoint and use our MCP tools to fetch Oracle feature data.
**That assumption was wrong.**

### What ICA's Framer connector actually does:

```
ICA "Add data source" → "Framer" connector
        ↓
  Validates project_link URL format (regex: must be https://framer.com/projects/<Name>--<ID>)
        ↓
  Goes to https://framer.com/projects/<Name>--<ID>  (the project editor page)
        ↓
  Finds the published Framer website URL
        ↓
  Crawls the published Framer website (follows sitemap, extracts page text)
        ↓
  Stores extracted text as knowledge graph nodes
```

**Our Framer project (`oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp`) was created but never populated.**
It published the default blank "My Framer Site" template. ICA found 1 page, extracted near-empty content,
and produced **5 nodes / 4 edges** — matching the blank template structure.

**The MCP URL we set in ICA forms is used separately** — it's for agents querying the knowledge graph,
not for the initial data ingest.

---

## LESSON 2 — The project_link URL Must Be Exactly Right

ICA validates the `project_link` before even fetching it. The regex accepts **only**:

```
https://framer.com/projects/<ProjectName>--<ProjectID>
```

### Dead URLs — rejected by ICA's regex:

| URL attempted | Why it fails |
|---|---|
| `https://oraclereadinesssrc-dzxnqq.fly.dev/framer-site` | Not a framer.com URL |
| `https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata` | Not a framer.com URL |
| `https://oracle-readiness-mcp.framer.app` | Published site subdomain — regex needs `/projects/` path |
| `https://oracle-readiness-mcp.framer.website` | Same — wrong domain pattern |
| `https://framer.com/m/...` | Share/preview link — wrong path pattern |

### ✅ The only URL that passes:

```
https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp
```

This is stored as `_FRAMER_PROJECT_URL` in `server.py` and returned by both `/health` and `/framer-metadata`.

---

## LESSON 3 — The Server Code Was Fixed But The ICA Record Was Not

By the end of the session, the server (`server.py`) was returning the correct `project_link` from both
`/health` and `/framer-metadata`. **But ICA stores the project_link in its own database when you first add
the source.** Fixing the server doesn't automatically update ICA's stored record.

The ICA source record needed a separate `PUT` call to update `connection_details.project_link`.

### Current confirmed state of ICA source record (as of end of last session):

- **Source ID:** `src_e157006ebcf1` (confirmed by network DevTools on Retry button click)
- **Stored connection_url/project_link:** The Retry button POST showed `connection_url: "https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp"` — **this is already correct**
- **Why ingest still gives 5 nodes:** Because the Framer site at that project URL contains blank template content

The source record's `project_link` is correct. The problem is the **published Framer site has no content**.

---

## LESSON 4 — The Console fetch() Workaround Has Limits

When trying to trigger the ICA ingest via `fetch()` in the browser console:

- Relative paths like `fetch('/data-ingest/framer/ingest', ...)` get **downgraded to `http://`** by the app's
  fetch interceptor → **Mixed Content block** (the page is HTTPS)
- Full absolute HTTPS URLs work for POST requests **if credentials are included** — but the app intercepts
  and may still downgrade in some browsers
- **The Retry button in the ICA UI is the most reliable way to trigger ingest** — it uses the stored
  source record directly and bypasses the fetch interceptor

---

## LESSON 5 — There Were Two Code Bugs Hiding The Problem

Both were fixed in commit `93cd7c7` and `affc3b3`. Documented here so they're never accidentally re-introduced.

### Bug 1: `_health()` returned wrong `project_link` (commit 93cd7c7)

```python
# BROKEN (was in server.py):
"project_link": f"{_APP_URL}/framer-metadata"   # Fly.io URL — rejected by ICA

# FIXED:
"project_link": _FRAMER_PROJECT_URL              # framer.com/projects/...
```

This bug meant that for hours, every time we checked `/health`, the URL looked wrong — but because the
ICA source record was manually set correctly, it didn't affect ingest directly. It was misleading though.

### Bug 2: `_framer_metadata()` returned wrong `project_link` (commit 93cd7c7)

```python
# BROKEN:
"project_link": f"{_APP_URL}/framer-site"       # Fly.io URL — rejected by ICA

# FIXED:
"project_link": _FRAMER_PROJECT_URL
```

### Bug 3: `_ica_features()` queried wrong table (commit affc3b3)

```python
# BROKEN: queried features table (module-level stubs, ~42 rows for 26C)
entries = db.filter_entries(release=release, product_family=pillar, limit=10000)

# FIXED: queries feature_details table (individual features, 1,725 rows for 26C)
details = db.get_details_with_flags(release=release, product_family=pillar)
```

This was the reason `features.csv?release=26C` was returning 42 rows instead of 1,726.

### Bug 4: `_ica_actions()` had a falsy bug on release parameter (commit affc3b3)

```python
# BROKEN: "26C" or "" evaluates to "" when release="26C" is a truthy string... 
# actually more subtle: `release or ""` is fine for strings, but the original
# code had a logic inversion that caused release filter to be skipped

# FIXED: clean if/else branching for release/pillar filters
```

---

## LESSON 6 — The `features` vs `feature_details` Table Hierarchy

This caused a lot of confusion. There are TWO tables storing Oracle data:

| Table | What it stores | Row count | Flags? |
|---|---|---|---|
| `features` | Module-level section titles ("Absence Management What's New 26C") | ~685 | Yes (extracted from Oracle catalogue HTML) |
| `feature_details` | Individual features ("AI-Driven Absence Prediction") | ~2,070 (1,725 for 26C) | No (all 0) |

**They cannot be joined by name.** The names are at completely different levels of the Oracle hierarchy.

**ICA CSV uses `feature_details`** — it has the rich individual feature data including `description_full`,
`steps_to_enable`, `business_benefit`, `key_resources`, `tips`.

**Flags are all 0 in `feature_details`** because:
1. The deep scraper (`oracle_scraper.py → parse_feature_detail_page()`) doesn't extract flag badges from HTML
2. Backfilling from `features` table produces 0 matches (different hierarchy level)
3. The XLSX dump has `#UNCALCULATED` in the Feature column (Excel formulas not evaluated before export)

**This is NOT a blocking problem for ICA ingest.** The `contextText` field in the CSV contains
the full `description_full` text, which ICA's vector store can use for semantic search even without flags.

---

## LESSON 7 — Deploy Verification Must Use diag3.py, Not curl

External `curl` from Windows PowerShell **times out** on the Fly.io URL due to a networking
issue (Windows/ISP with Fly.io's WireGuard proxy). This caused false alarms about whether
deploys succeeded.

### The reliable verification method:

```powershell
# 1. Upload the diag script
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq diag3.py

# 2. Run it from inside the container (always works)
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "python diag3.py"
```

Look for `_FRAMER_PROJECT_URL: FOUND` — that confirms the new image is running.

### The false alarm: "WARNING: not listening on expected address"

This appears during rolling deploys when the old process is still on port 8080. **It is usually a false alarm.**
If `diag3.py` says `FOUND`, the new image is running correctly.

---

## LESSON 8 — The XLSX Trap

The Oracle Feature Summary spreadsheet (`Oracle_Fusion_Impact_Analyzer.xlsx`) exported to
`Feature_Summary.json` has `"Feature": "#UNCALCULATED"` for every row. This is because Excel
formulas were not evaluated before the JSON export.

**To fix for future exports:**
1. Open XLSX in Excel
2. Press `Ctrl+Alt+F9` (force recalculate all)
3. Save
4. Re-export to JSON

**This is a non-issue for current ICA ingest** — we don't use the XLSX for ICA CSVs.

---

## WHERE WE ARE RIGHT NOW

```
Server code:         ✅ Correct
Fly.io deployment:   ✅ Running (commit d839a34 deployed)
ICA source record:   ✅ project_link already correct (src_e157006ebcf1)
Framer site content: ❌ BLANK — this is why ingest gives 5 nodes
ICA graph nodes:     ❌ 5 nodes / 4 edges (blank template)
Flag data:           ❌ All 0 in feature_details (non-blocking)
```

**The only blocking problem is: the published Framer site has no Oracle content.**

---

## TWO PATHS FORWARD

### Path A — CSV Upload (RECOMMENDED, ~15 minutes)

Bypass the Framer connector entirely. Upload the 6 CSVs directly via ICA Schema Builder.
This works today with zero code changes.

**Step 1:** Log in at `https://oraclereadinesssrc-dzxnqq.fly.dev/` to get a session cookie

**Step 2:** Download the 6 CSV files:

```
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/releases.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/action-types.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/modules.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/derivation-methods.csv
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C    ← 1,726 rows
https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/actions.csv?release=26C     ← 1,716 rows
```

**Step 3:** In ICA Context Studio → 26c Complete Ontology → Schema Builder → Upload Sample Data
Upload in the order listed above (enum lookups before entity nodes).

**Step 4 (one-time):** Add 5 properties to `custom:feature` node in Schema Builder UI:

| Property name | Curie | Type | Required |
|---|---|---|---|
| Is AI Feature | `custom:isAiFeature` | boolean | No |
| AI Type | `custom:aiType` | string | No |
| Is Redwood | `custom:isRedwood` | boolean | No |
| Auto Enabled In | `custom:autoEnabledIn` | string | No |
| Opt In Required | `custom:optInRequired` | boolean | No |

Also set `featureCode` on `oracleFusionGraphEntity` → `required: false`

---

### Path B — Populate The Framer Site (HARDER, needs Framer editor access)

Publish Oracle feature data as actual pages in the Framer project. ICA will then crawl
those pages on the next ingest.

Options:
1. **Manual CMS in Framer** — add pages in the Framer editor with feature text pasted in
2. **Framer CMS API** — use Framer's CMS API to programmatically add content to the project
3. **Redirect trick** — configure the Framer project's published site to redirect ICA's crawler to our `/framer-site` endpoint (complex, may not work)

This approach is much more complex and requires Framer account access. **Path A is strongly recommended.**

---

## QUICK START FOR NEXT SESSION

If you're starting fresh, run these three checks first:

```powershell
# 1. Is the server running with correct code?
& "$env:USERPROFILE\.fly\bin\fly.exe" sftp put --app oraclereadinesssrc-dzxnqq diag3.py
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq -C "python diag3.py"
# Expected: _FRAMER_PROJECT_URL: FOUND

# 2. Does the DB have data?
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console --app oraclereadinesssrc-dzxnqq `
  -C "python -c \"import sqlite3; db=sqlite3.connect('/data/readiness.db'); [print(t, db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ['features','feature_details']]\""
# Expected: features 685, feature_details 2070

# 3. Is the ICA source configured correctly?
# Open Context Studio DevTools console and run:
# await fetch('/data-ingest/sources?context_id=ctx_9baeb72e480b', {credentials:'include'}).then(r=>r.json())
# Look for "Oracle Readiness MCP" source with project_link = framer.com/projects/...
```

---

## KEY IDs — Memorise These

| Thing | Value |
|---|---|
| Fly.io app name | `oraclereadinesssrc-dzxnqq` |
| Fly.io region | `lhr` (London) |
| Public URL | `https://oraclereadinesssrc-dzxnqq.fly.dev` |
| ICA context ID | `ctx_9baeb72e480b` |
| ICA context name | `26c Complete Ontology` |
| ICA team | `MattStocker` (`69aaae8a8482bc71f1c4af52`) |
| ICA source ID | `src_e157006ebcf1` *(changes if source deleted/re-added)* |
| Framer project URL | `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
| Framer published site | `https://oracle-readiness-mcp.framer.website` *(blank template)* |
| ICA MCP gateway server | `8ccdd203bdee4014b08e82eedb6046e2` |

---

## COMPLETE DEAD ENDS LOG — Do Not Retry

| Approach | What we tried | Why it fails | Evidence |
|---|---|---|---|
| `/framer-site` as project_link | Point ICA's `project_link` at `{APP_URL}/framer-site` | ICA regex rejects non-framer.com URLs | ICA error "Invalid Framer project URL format" |
| `/framer-metadata` as project_link | Same as above | Same regex rejection | Was hardcoded in `_health()` for hours |
| HTML spoofing with Framer markers | Serve fake HTML with `data-framer-hydrate-v2`, `Server: Framer/5d364ee` | ICA validates URL format first; HTML never checked if URL wrong | `data-framer: FOUND` in diag but ingest still failed |
| `window.__framer__` in HTML | Add `window.__framer__ = {...}` to `/framer-site` | Real Framer sites don't have this object in body HTML | Fetched real Framer site — no `window.__framer__` |
| `*.framer.app` as project_link | Published site subdomain | Regex needs `/projects/` path | ICA rejection |
| `fetch()` from console with relative paths | `fetch('/data-ingest/...', ...)` | App fetch interceptor downgrades to `http://` → Mixed Content | Browser console "Mixed Content" error |
| `backfill_flags_from_features()` | Join feature_details to features by name | Names at different hierarchy levels — zero matches always | Running backfill returns 0 rows updated |
| XLSX flags via `Feature_Summary.json` | Extract flags from JSON dump | `"Feature": "#UNCALCULATED"` — formulas not evaluated | Checked JSON file directly |

---

## WHAT SHOULD BE DONE NEXT (PRIORITY ORDER)

### ✅ Done — no action needed
- Server code returning correct `project_link` from `/health` and `/framer-metadata`
- `feature_details` table has 2,070 rows (1,725 for 26C)
- `features.csv?release=26C` returns 1,726 rows
- `actions.csv?release=26C` returns 1,716 rows
- ICA source record has correct `project_link` stored

### 🔴 Priority 1 — Upload CSVs to ICA (Path A above, ~15 min)

The fastest way to populate the graph. No code changes needed.
Refer to `docs/ICA_INGEST_RUNBOOK.md` Step 6 for the exact upload sequence.

### 🟡 Priority 2 — Extract flags from Oracle HTML (code change, `oracle_scraper.py`)

Extend `parse_feature_detail_page()` to detect flag indicators in Oracle What's New pages.
Look for badge/tag elements in the HTML:
- "Opt In" badge → `opt_in_required = 1`
- "AI" badge or "Oracle AI" label → `is_ai = 1`  
- "Redwood" badge or UI icon → `is_redwood = 1`
- "Auto Enabled" status → `auto_enabled_in = release_code`

Once extracted, flags will automatically appear in the ICA CSVs on next scrape.

### 🟢 Priority 3 — Add .dockerignore to reduce build context

```
# Add Playground/.dockerignore:
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
readiness_*.db
```

Build context is currently 658 MB. This would reduce it to ~2 MB.

### 🟢 Priority 4 — Run deep scrape refresh on Fly

If `feature_details` row count drops below expected:
```python
# Via MCP tool:
deep_scrape_feature_details(products=["HCM","ERP","SCM","CX"], releases=["26C"])
# This takes ~20-30 minutes
```

---

## APPENDIX — Network Findings (from browser DevTools, confirmed)

### The Retry button POST (confirmed working)

```
POST https://contextstudio.servicesessentials.ibm.com/data-ingest/framer/ingest
Content-Type: application/json

{
  "source_id": "src_e157006ebcf1",
  "context_id": "ctx_9baeb72e480b",
  "connection_name": "Oracle Readiness MCP",
  "connection_url": "https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp",
  "generate_embeddings": true,
  "include_components": true,
  "max_pages": 100,
  "source_type": "framer"
}

→ 202 Accepted (ingest starts, but crawls blank Framer site → 5 nodes)
```

### Key ICA API endpoints (from DevTools observation)

| Endpoint | Method | Purpose |
|---|---|---|
| `/data-ingest/sources?context_id=ctx_9baeb72e480b` | GET | List all sources for context |
| `/data-ingest/sources/{source_id}` | PUT | Update source record |
| `/data-ingest/framer/ingest` | POST | Trigger Framer ingest |
| `/data-ingest/framer-ingest/source/{source_id}` | GET | Poll ingest status |

All require `credentials: 'include'` (session cookie from being logged into Context Studio).

---

*End of Field Guide — updated 2026-07-25*
