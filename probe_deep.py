"""
Probe the deep-scrape chain walk for one 25B module and one 26C module.
Shows exactly how many feature pages are found via rel="next" walking.
"""
import asyncio, httpx, sys
sys.path.insert(0, ".")
from oracle_scraper import _collect_feature_page_links, USER_AGENT, _get

TEST_CASES = [
    ("25B", "hcm", "Human Resources",   "https://docs.oracle.com/en/cloud/saas/readiness/hcm/25b/hure-25b/index.html"),
    ("25B", "hcm", "Payroll",           "https://docs.oracle.com/en/cloud/saas/readiness/hcm/25b/payr-25b/index.html"),
    ("26B", "hcm", "Human Resources",   "https://docs.oracle.com/en/cloud/saas/readiness/hcm/26b/hure-26b/index.html"),
    ("26C", "hcm", "Human Resources",   "https://docs.oracle.com/en/cloud/saas/readiness/hcm/26c/hure-26c/index.html"),
    ("26C", "hcm", "Payroll",           "https://docs.oracle.com/en/cloud/saas/readiness/hcm/26c/payr-26c/index.html"),
]

async def main():
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30) as client:
        for rel, pf, module, url in TEST_CASES:
            print(f"\n=== {rel}/{pf}/{module} ===")
            print(f"  index: {url}")
            try:
                resp = await _get(client, url)
                print(f"  index HTTP: {resp.status_code}")
                # Check for rel=next link
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "lxml")
                nxt = soup.find("link", rel="next")
                if nxt:
                    print(f"  first rel=next: {nxt.get('href', 'NONE')}")
                else:
                    print("  NO rel=next found in index page!")
                    # Show all <link> tags
                    links = soup.find_all("link")
                    print(f"  <link> tags present: {len(links)}")
                    for lk in links[:5]:
                        print(f"    {lk}")
                
                links = await _collect_feature_page_links(client, resp.text, url, max_pages=500)
                print(f"  Feature pages found: {len(links)}")
                if links:
                    print(f"  First: {links[0]}")
                    print(f"  Last:  {links[-1]}")
            except Exception as e:
                print(f"  ERROR: {e}")

asyncio.run(main())
