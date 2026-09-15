"""
Inspect the local readiness_remote.db to understand what product_family
values the HCM catalogue stubs actually store — and why deep-scrape
might miss them.
"""
import sqlite3, sys
sys.path.insert(0, ".")

db_path = "readiness_remote.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# 1. Features count per release+product_family
print("=== features table: count per release+product_family ===")
rows = conn.execute("""
    SELECT release, product_family, COUNT(*) as cnt
    FROM features
    GROUP BY release, product_family
    ORDER BY release, product_family
""").fetchall()
for r in rows:
    print(f"  {r['release']:6} {r['product_family']:10} {r['cnt']:5} features")

# 2. What product_family do HCM catalogue stubs use?
print("\n=== HCM stubs for 25B in features table ===")
rows = conn.execute("""
    SELECT release, product_family, module, html_url
    FROM features
    WHERE release='25B'
    ORDER BY product_family, module
""").fetchall()
for r in rows:
    print(f"  [{r['product_family']:6}] {r['module'][:40]:40} url={r['html_url']}")

# 3. What the deep-scrape query would see for hcm/25B
print("\n=== Deep-scrape query result for product_family='hcm' ===")
rows = conn.execute("""
    SELECT DISTINCT html_url, module, release, product_family
    FROM features
    WHERE product_family = 'hcm'
      AND html_url LIKE '%index.html'
      AND html_url NOT LIKE '%www.oracle.com%'
    ORDER BY release DESC
""").fetchall()
for r in rows:
    print(f"  {r['release']:6} {r['module'][:40]:40}")

conn.close()
