"""Check feature_details counts per release+pillar."""
import sqlite3
db_path = "readiness_remote.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("=== feature_details count per release+product_family ===")
rows = conn.execute("""
    SELECT release, product_family, COUNT(*) as cnt
    FROM feature_details
    GROUP BY release, product_family
    ORDER BY release, product_family
""").fetchall()
for r in rows:
    print(f"  {r['release']:6} {r['product_family']:10} {r['cnt']:5}")

print("\n=== features table: feature_detail_url non-null per release+family ===")
rows = conn.execute("""
    SELECT release, product_family, 
           COUNT(*) as total,
           SUM(CASE WHEN feature_detail_url IS NOT NULL THEN 1 ELSE 0 END) as with_detail_url
    FROM features
    GROUP BY release, product_family
    ORDER BY release, product_family
""").fetchall()
for r in rows:
    print(f"  {r['release']:6} {r['product_family']:10} {r['total']:5} total, {r['with_detail_url']:5} with detail_url")

conn.close()
