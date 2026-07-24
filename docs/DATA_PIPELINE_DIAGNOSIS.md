# Data Pipeline Diagnosis Guide

Use this to diagnose why features.csv is empty, flags are 0, or row counts are wrong.

---

## Quick DB health check (run on Fly)

```powershell
& "$env:USERPROFILE\.fly\bin\fly.exe" ssh console `
    --app oraclereadinesssrc-dzxnqq `
    -C "python -c \"
import sqlite3
db = sqlite3.connect('/data/readiness.db')
for table in ['features', 'feature_details']:
    count = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    print(f'{table}: {count} rows')

# Check flag population in feature_details
flagged = db.execute('''
  SELECT 
    COUNT(*) total,
    SUM(is_ai) is_ai_count,
    SUM(is_redwood) is_redwood_count,
    SUM(opt_in_required) opt_in_count
  FROM feature_details WHERE release=\'26C\'
''').fetchone()
print(f'26C feature_details: total={flagged[0]}, is_ai={flagged[1]}, is_redwood={flagged[2]}, opt_in={flagged[3]}')
\""
```

Expected output:
```
features: 685 rows
feature_details: 1725 rows
26C feature_details: total=1725, is_ai=0, is_redwood=0, opt_in=0
```

> Note: is_ai=0 is **expected** — flag backfill is a known outstanding issue.

---

## Why features.csv returned only 42 rows (historical root cause — FIXED)

**Root cause:** `_ica_features()` in `server.py` was calling `filter_entries()` which
queries the `features` table. The `features` table contains **module-level catalogue
entries** (e.g. "Absence Management What's New 26C") — one row per module section,
not per individual feature.

**Fix applied (commit affc3b3):** `_ica_features()` now calls `get_details_with_flags()`
which queries `feature_details` (1,725 individual features). `filter_entries()` is only
used as a fallback if `feature_details` is empty.

**If you see 42 rows again:** Check that `feature_details` has rows and that the code
in `server.py` `_ica_features()` is using `get_details_with_flags()` as the primary path.

---

## Why flags are all 0 in feature_details

Three root causes, all present simultaneously:

### 1. XLSX backfill produces 0 matches

`backfill_flags_from_features()` tries to match:
- `features.feature_name` = "Absence Management What's New 26C"  (module stub name)
- `feature_details.feature_name` = "AI-Driven Absence Prediction" (individual feature)

These **never match**. The JOIN is by name, but the names are at different hierarchy levels.

### 2. The XLSX itself has `#UNCALCULATED` in the Feature column

The `Feature_Summary.json` dump has `"Feature": "#UNCALCULATED"` for every row.
Excel formulas were not recalculated before export. So even if we could match by name,
the source data is corrupt.

**To fix the XLSX for future exports:**
1. Open XLSX in Excel
2. `Ctrl+Alt+F9` to force recalculate
3. Save
4. Re-export to JSON

### 3. `parse_feature_detail_page()` doesn't extract flags

The deep scraper populates `description_full`, `steps_to_enable`, `business_benefit` etc.
but does NOT extract `is_ai`, `is_redwood`, `opt_in_required` flags from the Oracle HTML.

**The fix (not yet implemented):** Extend `parse_feature_detail_page()` in
`oracle_scraper.py` to detect flag indicators in the Oracle What's New page HTML
(look for badge/tag elements, table rows labelled "Opt In", "AI Feature", etc.)

---

## Row count reference table

| Source | Table written | Expected 26C row count |
|---|---|---|
| Catalogue scrape (`refresh_readiness_data`) | `features` | ~685 |
| XLSX ingest (`ingest_xlsx_dump`) | `features` (updates) | same |
| Deep scrape (`deep_scrape_feature_details`) | `feature_details` | ~1,725 |
| ICA features CSV | from `feature_details` | 1,726 (includes header) |
| ICA actions CSV | from `feature_details` | 1,716 (includes header) |

---

## Checking what the ICA CSV actually contains

```bash
# Count rows
curl -s "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" | wc -l

# Sample first 5 feature names
curl -s "https://oraclereadinesssrc-dzxnqq.fly.dev/api/ica/features.csv?release=26C" \
  | python -c "import sys,csv; r=csv.DictReader(sys.stdin); [print(row['name']) for i,row in zip(range(5),r)]"
```

If the names look like "Absence Management What's New 26C" → you're getting the wrong table.
If they look like "AI-Driven Absence Prediction" → correct.
