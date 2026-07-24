import asyncio, json, sys, os
sys.path.insert(0, '/app')
os.environ.setdefault('READINESS_DATA_DIR', '/data')
from db import ReadinessDB
from pathlib import Path

db = ReadinessDB(Path('/data/readiness.db'))

# Run the migration to add new columns (idempotent — errors are silently swallowed)
print("Migration done (new columns added or already existed)")

# Check columns actually exist
cols = [r[1] for r in db._execute("PRAGMA table_info(feature_details)").fetchall()]
print("feature_details columns:", cols)

# Backfill flags from features -> feature_details
backfilled = db.backfill_flags_from_features()
print(f"Backfilled {backfilled} feature_details rows with flags")

# Verify
ai_count = db._execute("SELECT COUNT(*) FROM feature_details WHERE is_ai=1 AND UPPER(release)='26C'").fetchone()[0]
rw_count = db._execute("SELECT COUNT(*) FROM feature_details WHERE is_redwood=1 AND UPPER(release)='26C'").fetchone()[0]
opt_count = db._execute("SELECT COUNT(*) FROM feature_details WHERE opt_in_required=1 AND UPPER(release)='26C'").fetchone()[0]
total_26c = db._execute("SELECT COUNT(*) FROM feature_details WHERE UPPER(release)='26C'").fetchone()[0]
print(f"26C feature_details: {total_26c} total | {ai_count} AI | {rw_count} Redwood | {opt_count} opt-in")
