"""
backfill_xlsx_flags.py
Run on the Fly machine: python /data/backfill_xlsx_flags.py
Reads Feature_Summary.json (XLSX dump), finds matching rows in feature_details
by feature name + release, and updates the flag columns.
"""
import json, sqlite3, re
from pathlib import Path

XLSX = Path('/data/Feature_Summary.json')
DB   = Path('/data/readiness.db')

raw  = json.loads(XLSX.read_text())
headers = raw['headers']
rows    = raw['rows']

# Column indices
def h(*names):
    for n in names:
        try: return headers.index(n)
        except ValueError: pass
    return None

i_feature   = h('Feature')
i_update    = h('Update')
i_pillar    = h('Pillar')
i_redwood   = h('Redwood')
i_ai        = h('AI')
i_impact    = h('Impact to Existing Processes')
i_action    = h('Action to Enable')
i_auto      = h('Auto Enabled In')
i_module    = h('Module')

print(f"Headers found: Feature={i_feature} Update={i_update} AI={i_ai} Redwood={i_redwood} Action={i_action}")

# Pillar normalisation
PILLAR_MAP = {
    'enterprise resource planning': 'erp',
    'erp': 'erp',
    'human capital management': 'hcm',
    'hcm': 'hcm',
    'supply chain management': 'scm',
    'scm': 'scm',
    'service': 'service',
    'cx service': 'service',
    'customer experience': 'service',
}

def norm_pillar(v):
    if not v: return None
    for k, code in PILLAR_MAP.items():
        if k in str(v).lower(): return code
    return str(v).lower().split()[0][:10]

def is_true(v):
    return v and str(v).strip().upper() in ('Y', 'YES', 'TRUE', '1', 'X')

db = sqlite3.connect(str(DB))
db.row_factory = sqlite3.Row

updated = 0
skipped_no_match = 0
skipped_bad_row  = 0

for row in rows:
    def g(i): return row[i] if i is not None and i < len(row) else None

    feature_name = g(i_feature)
    release      = g(i_update)
    pillar_raw   = g(i_pillar)

    if not feature_name or not release or str(feature_name).startswith('#'):
        skipped_bad_row += 1
        continue

    pillar = norm_pillar(pillar_raw)
    release = str(release).strip().upper()

    ai_raw    = g(i_ai)
    rw_raw    = g(i_redwood)
    impact    = str(g(i_impact) or '').strip() or None
    action    = str(g(i_action) or '').strip() or None
    auto_in   = str(g(i_auto) or '').strip() or None

    # Determine boolean flags
    is_ai    = 1 if is_true(ai_raw) else 0
    is_rw    = 1 if is_true(rw_raw) else 0
    opt_in   = 1 if action and 'opt' in action.lower() else 0
    setup_r  = 1 if action and 'setup' in action.lower() else 0

    # Classify AI type
    ai_type = None
    if is_ai:
        if ai_raw and 'agent' in str(ai_raw).lower():
            ai_type = 'Agent'
        elif ai_raw and 'generative' in str(ai_raw).lower():
            ai_type = 'Generative'
        else:
            ai_type = str(ai_raw).strip() if ai_raw else 'AI'

    # Try exact match first, then case-insensitive
    cur = db.execute(
        "SELECT id FROM feature_details WHERE LOWER(feature_name)=LOWER(?) AND UPPER(release)=UPPER(?)",
        (feature_name, release)
    )
    match = cur.fetchone()

    if not match:
        skipped_no_match += 1
        continue

    db.execute(
        """UPDATE feature_details SET
            is_ai=?, ai_type=?, is_redwood=?, auto_enabled_in=?,
            opt_in_required=?, setup_required=?, impact=?, enablement=?
           WHERE id=?""",
        (is_ai, ai_type, is_rw, auto_in, opt_in, setup_r, impact, action, match['id'])
    )
    updated += 1

db.commit()

ai_count  = db.execute("SELECT COUNT(*) FROM feature_details WHERE is_ai=1 AND UPPER(release)='26C'").fetchone()[0]
rw_count  = db.execute("SELECT COUNT(*) FROM feature_details WHERE is_redwood=1 AND UPPER(release)='26C'").fetchone()[0]
opt_count = db.execute("SELECT COUNT(*) FROM feature_details WHERE opt_in_required=1 AND UPPER(release)='26C'").fetchone()[0]

print(f"\nResults:")
print(f"  Updated:          {updated}")
print(f"  Skipped (no match): {skipped_no_match}")
print(f"  Skipped (bad row):  {skipped_bad_row}")
print(f"\n26C feature_details after backfill:")
print(f"  is_ai=1:            {ai_count}")
print(f"  is_redwood=1:       {rw_count}")
print(f"  opt_in_required=1:  {opt_count}")
