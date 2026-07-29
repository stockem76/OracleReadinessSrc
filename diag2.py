import urllib.request, json, os

# Hit the local server
try:
    resp = urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=10)
    body = resp.read().decode()
    print("health:", body[:500])
except Exception as e:
    print("health FAILED:", e)

# Try framer-metadata
try:
    resp2 = urllib.request.urlopen("http://127.0.0.1:8080/framer-metadata", timeout=10)
    body2 = resp2.read().decode()
    print("framer-metadata:", body2[:300])
except Exception as e:
    print("framer-metadata FAILED:", e)

# Check APP_URL env var - this is what framer-metadata returns as the MCP URL
app_url = os.environ.get("APP_URL", "NOT SET")
print("APP_URL:", app_url)

# Check the framer-site endpoint (what ICA's validator fetches)
try:
    resp3 = urllib.request.urlopen("http://127.0.0.1:8080/framer-site", timeout=10)
    body3 = resp3.read().decode()
    print("framer-site status:", resp3.status)
    print("framer-site headers:", dict(resp3.headers))
    # Check for Framer markers ICA looks for
    markers = ["__framer__", "data-framer", "framer.com"]
    for m in markers:
        print(f"  marker '{m}': {'FOUND' if m in body3 else 'MISSING'}")
except Exception as e:
    print("framer-site FAILED:", e)

print("done")
