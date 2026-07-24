# ICA Context Studio — Framer Connector Reference

This file is the canonical source of truth for re-adding the Oracle Readiness MCP
connector in IBM Consulting Advantage Context Studio.  Keep it updated whenever
the Fly.io app name or token changes.

---

## Connector form values

When you click **"Add data source"** → **"Framer"** in Context Studio, fill in:

| ICA form field | Value |
|---|---|
| **Connection name** | `Oracle Readiness MCP` |
| **Connection URL / Project link** | `https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata` |
| **MCP URL** | `https://oraclereadinesssrc-dzxnqq.fly.dev/mcp` |
| **Project ID** | `oraclereadinesssrc-dzxnqq` |
| **Project name** | `Oracle Readiness MCP` |
| **Token / Bearer secret** | *(value of `READINESS_TOKEN` — see below)* |
| **Re-Ingestion Required** | ☑ checked |

> **Why `/framer-metadata` as the Connection URL?**
> ICA's framer connector validates that `project_link` is a resolvable URL.
> Pointing it at `/framer-metadata` satisfies that check and lets the connector
> self-discover its own configuration at any time.

---

## Retrieving the token

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq `
    --command "printenv READINESS_TOKEN"
```

Or from the Fly.io dashboard:
`https://fly.io/apps/oraclereadinesssrc-dzxnqq/secrets`

---

## Verification endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Quick liveness + feature count + mcp_url + project_link |
| `GET /framer-metadata` | Full ICA form field reference + ica_csv_endpoints map |
| `GET /mcp` | MCP StreamableHTTP transport (requires Bearer token) |

```bash
# Quick check — should return status: ok
curl https://oraclereadinesssrc-dzxnqq.fly.dev/health

# ICA connector form values + CSV endpoint discovery
curl https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata
```

---

## ICA Schema Builder — Upload Sample Data endpoints

These endpoints require **no authentication** and return CSV files ready for
the ICA Schema Builder → **Upload Sample Data** button.

| Endpoint | ICA schema target | Description |
|---|---|---|
| `GET /api/ica/schema-changes.json` | — | Machine-readable manifest of all required changes + status |
| `GET /api/ica/releases.csv` | `enum:oracleFusion26cReleaseCode` | Extend with 26D, 27A, 27B |
| `GET /api/ica/action-types.csv` | `enum:oracleFusion26cActionType` | Add Business Benefit, Key Resources |
| `GET /api/ica/modules.csv` | `enum:oracleFusion26cModule` | Add missing modules from live data |
| `GET /api/ica/derivation-methods.csv` | `enum:oracleFusion26cMethod` | Add M017_MCP_FRAMER_INGESTION |
| `GET /api/ica/features.csv` | `custom:feature` nodes | All 965+ features with AI/Redwood/optIn flags in contextText |
| `GET /api/ica/actions.csv` | `custom:action` nodes | Steps to Enable, Business Benefit, Key Resources, Tips per feature |

Query parameters supported on `features.csv` and `actions.csv`:
- `?release=26C` — filter to a specific release
- `?pillar=HCM` — filter to a product family

```bash
# Download the full features CSV for ICA upload
curl "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" \
     -o features_26C.csv

# Check which schema changes still need manual UI action
curl https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/schema-changes.json | python -m json.tool
```

### Manual steps still required in ICA Schema Builder UI

The following changes **cannot be done via CSV upload** — they require the
Schema Builder properties/edge panels:

1. **Add 5 properties to Feature node** (`custom:feature` → Properties):
   - `isAiFeature` (boolean, optional)
   - `aiType` (string, optional)
   - `isRedwood` (boolean, optional)
   - `autoEnabledIn` (string, optional)
   - `optInRequired` (boolean, optional)

2. **Extend REQUIRES_ACTION domain** → add `custom:section` to domain list

3. **Set featureCode optional** on abstract parent `oracleFusionGraphEntity`
   (change `required: true` → `required: false`)

### MCP tool for programmatic access

```
get_ica_framer_csv(entity_type="schema-changes")   # see what needs doing
get_ica_framer_csv(entity_type="features", release="26C")
get_ica_framer_csv(entity_type="actions", release="26C", pillar="HCM")
get_ica_framer_csv(entity_type="modules")
get_ica_framer_csv(entity_type="releases")
get_ica_framer_csv(entity_type="action-types")
get_ica_framer_csv(entity_type="derivation-methods")
```

---

## Context Studio details

| Field | Value |
|---|---|
| **Context ID** | `ctx_9baeb72e480b` |
| **Context name** | `26c Complete Ontology` |
| **Owner team** | `MattStocker` (teamId `69aaae8a8482bc71f1c4af52`) |
| **Source ID** | `src_ef55df5d25d1` *(changes each time source is deleted/re-added)* |
| **Context Studio URL** | `https://contextstudio.servicesessentials.ibm.com/?teamName=MattStocker&teamId=69aaae8a8482bc71f1c4af52&tab=context` |

> **Note:** The Source ID changes every time you delete and re-add the connector.
> The Context ID (`ctx_9baeb72e480b`) stays the same.

---

## Common errors and fixes

| Error | Cause | Fix |
|---|---|---|
| `missing project_link` | Connection URL field left blank or set to bare root URL | Use `https://oraclereadinesssrc-dzxnqq.fly.dev/framer-metadata` |
| `Invalid Framer project URL format` | Connection URL set to `https://oraclereadinesssrc-dzxnqq.fly.dev` (no path) | Add `/framer-metadata` path |
| `Failed to start ingestion: 401` | Wrong or missing Bearer token | Re-check `READINESS_TOKEN` via fly ssh |
| `Failed to start ingestion: 403` | Source belongs to different team | Open Context Studio under `MattStocker` team, not Oracle Practice UKI |

---

## Re-deploying

```powershell
# From GIT_ROOT/Playground:
& "$env:USERPROFILE\.fly\bin\fly.exe" deploy --remote-only
```

The `APP_URL` env var is set in `fly.toml` so `/framer-metadata` and `/health`
always return the correct public URL without any manual configuration.
