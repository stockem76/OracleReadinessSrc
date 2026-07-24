import sqlite3
db = sqlite3.connect('/data/readiness.db')
print('features:', db.execute('SELECT COUNT(*) FROM features').fetchone()[0])
print('feature_details:', db.execute('SELECT COUNT(*) FROM feature_details').fetchone()[0])
print('26C feature_details:', db.execute("SELECT COUNT(*) FROM feature_details WHERE UPPER(release)='26C'").fetchone()[0])
print('26C features:', db.execute("SELECT COUNT(*) FROM features WHERE UPPER(release)='26C'").fetchone()[0])

# Check join matches on 26C
joins = db.execute("""
SELECT COUNT(*) FROM feature_details fd
LEFT JOIN features f
  ON  UPPER(f.release)        = UPPER(fd.release)
  AND LOWER(f.product_family) = LOWER(fd.product_family)
  AND LOWER(f.feature_name)   = LOWER(fd.feature_name)
WHERE UPPER(fd.release)='26C' AND f.id IS NOT NULL
""").fetchone()[0]
print('26C join matches (feature_details -> features):', joins)

# Sample a mismatched pair to understand why
sample = db.execute("""
SELECT fd.feature_name, fd.product_family, fd.release,
       f.feature_name as f_name
FROM feature_details fd
LEFT JOIN features f
  ON  UPPER(f.release)        = UPPER(fd.release)
  AND LOWER(f.product_family) = LOWER(fd.product_family)
  AND LOWER(f.feature_name)   = LOWER(fd.feature_name)
WHERE UPPER(fd.release)='26C' AND f.id IS NULL
LIMIT 5
""").fetchall()
print('\nUnmatched fd rows:')
for r in sample:
    print(' ', r)

# Check what release values are in feature_details
rels = db.execute("SELECT DISTINCT release FROM feature_details ORDER BY release").fetchall()
print('\nDistinct releases in feature_details:', [r[0] for r in rels])

# Check is_ai distribution in features
ai_count = db.execute("SELECT COUNT(*) FROM features WHERE is_ai=1 AND UPPER(release)='26C'").fetchone()[0]
print('is_ai=1 in features 26C:', ai_count)
