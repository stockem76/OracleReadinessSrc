import httpx, sys, os
from collections import defaultdict
sys.path.insert(0, ".")
from oracle_scraper import _parse_catalogue_markdown, _CATALOGUE_PAGES, USER_AGENT
from markdownify import markdownify as html_to_md

all_url, fallback_url = _CATALOGUE_PAGES["hcm"]
r = httpx.get(all_url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
md = html_to_md(r.text, heading_style="ATX")
features = _parse_catalogue_markdown(md, "hcm", all_url)

by_rel = defaultdict(list)
for f in features:
    by_rel[f.release].append((f.feature_name, f.module, f.html_url))

for rel in ["25B", "26B", "26C"]:
    print(f"--- {rel} ({len(by_rel[rel])} entries) ---")
    for fname, module, url in by_rel[rel]:
        print(f"  [{module[:30]}] {fname[:60]}")
        if url: print(f"    url: {url[:120]}")
    print()
