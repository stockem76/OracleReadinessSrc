# ICA Ingest Runbook — Step-by-Step

> Use this whenever the Framer ingest is broken, the graph is at 0, or you're starting fresh.

---

## Step 0 — Verify the server is healthy

```bash
curl https://oraclereadinesssrc-dzxnqq.fly.dev/health
```
Expected: `{"status":"ok","feature_count":>1000,...}`

Verify CSVs are populated:
```bash
curl -s "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" | wc -l
# Expect: 1727 (1726 data rows + 1 header)
```

---

## Step 1 — Open Context Studio in the browser

URL: `https://contextstudio.servicesessentials.ibm.com/?teamName=MattStocker&teamId=69aaae8a8482bc71f1c4af52&tab=context`

Make sure you are logged in as the **MattStocker** team. If the URL bar shows a different team, switch.

---

## Step 2 — Find the current Source ID

Open the browser DevTools Console (`F12` → Console tab) and run:

```javascript
const s = await fetch('/data-ingest/sources?context_id=ctx_9baeb72e480b',
  {credentials:'include'}).then(r=>r.json());
console.log(JSON.stringify(s, null, 2));
```

Find the source with `"name": "Oracle Readiness MCP"`. Note its `"id"` field (e.g. `src_ef55df5d25d1`).

---

## Step 3 — Fix the project_link

Replace `src_XXXX` with the real ID from Step 2:

```javascript
const r = await fetch('/data-ingest/sources/src_XXXX', {
  method: 'PUT',
  credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({connection_details:{
    project_link: 'https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp'
  }})
});
console.log(r.status, await r.text());
// Must be 200
```

---

## Step 4 — Trigger the ingest

```javascript
const r2 = await fetch('/data-ingest/framer/ingest', {
  method: 'POST',
  credentials: 'include',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({source_id:'src_XXXX', context_id:'ctx_9baeb72e480b'})
});
console.log(r2.status, await r2.text());
// Must be 202
```

---

## Step 5 — Poll until complete

```javascript
// Run this a few times, wait 30s between each
const p = await fetch('/data-ingest/framer-ingest/source/src_XXXX',
  {credentials:'include'}).then(r=>r.json());
console.log(JSON.stringify(p, null, 2));
// Wait for status to be "completed" not "running"
```

---

## Step 6 — Upload CSVs via Schema Builder

1. Open **Schema Builder** in Context Studio (26c Complete Ontology)
2. Click **Upload Sample Data**
3. Upload in this order (download links below):

```
1. https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/releases.csv
2. https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/action-types.csv
3. https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/modules.csv
4. https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/derivation-methods.csv
5. https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C
6. https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/actions.csv?release=26C
```

---

## Step 7 — Add 5 properties to Feature node (one-time only)

In Schema Builder → `custom:feature` → Properties panel, add:

| Name | Curie | Type |
|---|---|---|
| Is AI Feature | `custom:isAiFeature` | boolean |
| AI Type | `custom:aiType` | string |
| Is Redwood | `custom:isRedwood` | boolean |
| Auto Enabled In | `custom:autoEnabledIn` | string |
| Opt In Required | `custom:optInRequired` | boolean |

Also: set `featureCode` on `oracleFusionGraphEntity` → `required: false`

---

## Troubleshooting

| Symptom | Most likely cause | Fix |
|---|---|---|
| Step 2 returns empty array | Source was deleted | Re-add it via "Add data source" → Framer |
| Step 3 returns 404 | Wrong source ID | Re-run Step 2 |
| Step 3 returns 400 `Invalid URL` | project_link value wrong | Must be exactly `https://framer.com/projects/oracle-readiness-mcp--D3d8IX9Wv7mmBe1IrSwM-2cmmp` |
| Step 4 returns 401 | Not logged in | Refresh page and log in |
| Step 4 returns 403 | Wrong team context | Ensure URL has `teamName=MattStocker` |
| Step 5 shows `status: failed` | ICA couldn't fetch Framer project | Check Framer project is published: visit the URL in a browser |
| CSV upload fails at Step 6 | Server session expired | Log in at `https://oraclereadinesssrc-dzxnqq.fly.dev/` |
