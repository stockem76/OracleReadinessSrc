import sqlite3
db = sqlite3.connect('/data/readiness.db')
# Show sample feature names from features table for 26C to compare with feature_details
print("=== Sample features 26C ===")
rows = db.execute("SELECT feature_name, product_family, module FROM features WHERE UPPER(release)='26C' LIMIT 20").fetchall()
for r in rows:
    print(f"  [{r[1]}] {r[2][:30]:30s}  {r[0][:60]}")

print("\n=== Sample feature_details 26C ===")
rows = db.execute("SELECT feature_name, product_family, module FROM feature_details WHERE UPPER(release)='26C' LIMIT 20").fetchall()
for r in rows:
    print(f"  [{r[1]}] {r[2][:30]:30s}  {r[0][:60]}")

print("\n=== Total counts ===")
print("features 26C:", db.execute("SELECT COUNT(*) FROM features WHERE UPPER(release)='26C'").fetchone()[0])
print("feature_details 26C:", db.execute("SELECT COUNT(*) FROM feature_details WHERE UPPER(release)='26C'").fetchone()[0])

# Check if features has individual feature rows or just stubs
stubs = db.execute("SELECT COUNT(*) FROM features WHERE UPPER(release)='26C' AND feature_name LIKE \"%What's New%\"").fetchone()[0]
print(f"features 26C with 'What's New' (stubs): {stubs}")
real = db.execute("SELECT COUNT(*) FROM features WHERE UPPER(release)='26C' AND feature_name NOT LIKE \"%What's New%\"").fetchone()[0]
print(f"features 26C without 'What's New' (real): {real}")
