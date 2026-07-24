import asyncio, json, sys, os
sys.path.insert(0, '/app')
os.environ.setdefault('READINESS_DATA_DIR', '/data')
from oracle_scraper import parse_features_from_xlsx_dump
from db import ReadinessDB
from pathlib import Path

async def main():
    raw = json.loads(Path('/data/Feature_Summary.json').read_text())
    db  = ReadinessDB(Path('/data/readiness.db'))
    features = parse_features_from_xlsx_dump(raw['rows'], raw['headers'])
    count = await db.upsert_features(features)
    print(f'Loaded {count} features')
    fd = db._execute("SELECT COUNT(*) FROM feature_details").fetchone()[0]
    ft = db._execute("SELECT COUNT(*) FROM features").fetchone()[0]
    print(f'features table: {ft}  feature_details table: {fd}')

asyncio.run(main())
