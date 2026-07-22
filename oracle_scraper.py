"""
oracle_scraper.py
-----------------
Scraper + persistent SQLite cache for Oracle Cloud Applications Readiness data.

Sources:
  - Oracle Readiness pages (HTML, docs.oracle.com)
  - Oracle Readiness Reports Centre XLSX JSON dump
  - Direct HTML readiness pages (oracle.com/cloud/saas/…/whats-new/)

Feature schema mirrors the TypeScript oracle-readiness-mcp model (rich, with
impact/enablement/AI/Redwood/opt-in/auto-enabled flags) whilst keeping the
ClaudeCode scraper's proven HTML→markdown parse approach for the
docs.oracle.com readiness catalogue.

Two scraping strategies are used:
  1. docs.oracle.com readiness catalogue pages (erp-all.html etc.) — parsed
     with markdownify + regex (ClaudeCode approach) — gives title/module/release/links.
  2. oracle.com/cloud/saas/…/whats-new/ pages — parsed with node-html-parser
     equivalent (html-parser) — gives full feature rows with impact/enablement/AI flags.

Both routes converge on the same Feature dataclass stored in SQLite.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import httpx
from markdownify import markdownify as html_to_md
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
    _HAVE_PYPDF = True
except ImportError:
    _HAVE_PYPDF = False

logger = logging.getLogger("oracle_readiness_mcp.scraper")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

READINESS_APP_URL = (
    "https://www.oracle.com/webfolder/technetwork/tutorials/tutorial/readiness/app/index.html"
)
_DOCS_BASE = "https://docs.oracle.com/en/cloud/saas/readiness"
_OC_BASE   = "https://www.oracle.com"

# docs.oracle.com catalogue pages (all-releases preferred, fallback to latest)
_CATALOGUE_PAGES: dict[str, tuple[str, str]] = {
    "erp":     (f"{_DOCS_BASE}/erp-all.html",     f"{_DOCS_BASE}/erp.html"),
    "scm":     (f"{_DOCS_BASE}/scm-all.html",     f"{_DOCS_BASE}/scm.html"),
    "hcm":     (f"{_DOCS_BASE}/hcm-all.html",     f"{_DOCS_BASE}/hcm.html"),
    "service": (f"{_DOCS_BASE}/service-all.html",  f"{_DOCS_BASE}/service.html"),
    "news":    (f"{_DOCS_BASE}/news.html",          f"{_DOCS_BASE}/news.html"),
}

# oracle.com what's-new pages (rich feature tables with impact/enablement)
_WHATS_NEW_PAGES: dict[str, str] = {
    "erp":     f"{_OC_BASE}/cloud/saas/erp/whats-new/",
    "hcm":     f"{_OC_BASE}/cloud/saas/human-resources/whats-new/",
    "scm":     f"{_OC_BASE}/cloud/saas/supply-chain-management/whats-new/",
    "service": f"{_OC_BASE}/cloud/saas/service/whats-new/",
}

PRODUCT_LABELS: dict[str, str] = {
    "erp":     "Enterprise Resource Planning",
    "scm":     "Supply Chain & Manufacturing",
    "hcm":     "Human Capital Management",
    "service": "Service (CX)",
    "news":    "Cross-product Readiness News",
}

PRODUCTS = {p: pages[0] for p, pages in _CATALOGUE_PAGES.items()}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 oracle-readiness-mcp/2.0"
)

MAX_CONTENT_CHARS = 300_000

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Feature:
    release:         str
    product_family:  str            # erp / hcm / scm / service / news
    product:         str
    module:          str
    feature_name:    str
    description:     str            = ""
    impact:          Optional[str]  = None   # "Large scale" | "Small scale" | "Report"
    enablement:      Optional[str]  = None
    auto_enabled_in: Optional[str]  = None
    is_redwood:      bool           = False
    is_ai:           bool           = False
    ai_type:         Optional[str]  = None   # "Agent" | "Generative" | "Agentic App"
    setup_required:  bool           = False
    opt_in_required: bool           = False
    html_url:        Optional[str]  = None
    pdf_url:         Optional[str]  = None
    source_url:      str            = ""
    retrieved_at:    str            = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

_RELEASE_RE   = re.compile(r"\b(\d{2}[A-D])\b")
_MODULE_RE    = re.compile(r"^(?P<module>.+?)\s+What'?s New\b", re.IGNORECASE)
# Oracle catalogue pages use relative links e.g. [HTML](hcm/24a/benf-24a/index.html)
_TITLE_LX_RE  = re.compile(
    r"(?P<title>[^\n]{4,200}?)\n\s*\n"
    r"(?P<links>(?:\[(?:HTML|PDF)\]\([^\)]+\)\s*)+)",
    re.MULTILINE,
)
_LINK_RE = re.compile(r"\[(HTML|PDF)\]\(([^\)]+)\)")


def _extract_module(title: str) -> str:
    m = _MODULE_RE.match(title)
    return m.group("module").strip() if m else title.strip()


def _parse_impact(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    v = val.lower()
    if "large" in v: return "Large scale"
    if "small" in v: return "Small scale"
    if "report" in v: return "Report"
    return val.strip() or None


def _parse_enablement(val: Optional[str]) -> dict:
    if not val:
        return {"enablement": None, "setup_required": False, "opt_in_required": False}
    v = val.lower().strip()
    return {
        "enablement":      val.strip() or None,
        "setup_required":  "setup required" in v or "setup-required" in v,
        "opt_in_required": "opt in" in v or "opt-in" in v,
    }


def _parse_ai(val: Optional[str]) -> dict:
    if not val or not val.strip():
        return {"is_ai": False, "ai_type": None}
    v = val.lower().strip()
    if "agentic" in v: return {"is_ai": True, "ai_type": "Agentic App"}
    if "agent"   in v: return {"is_ai": True, "ai_type": "Agent"}
    if "generat" in v: return {"is_ai": True, "ai_type": "Generative"}
    return {"is_ai": True, "ai_type": val.strip()}


# ---------------------------------------------------------------------------
# Strategy 1: docs.oracle.com markdown catalogue parser (ClaudeCode approach)
# ---------------------------------------------------------------------------

def _resolve_url(href: str, base_url: str) -> str:
    """Resolve a relative href against the catalogue page URL."""
    if href.startswith("http"):
        return href
    base_dir = base_url.rsplit("/", 1)[0]
    return f"{base_dir}/{href.lstrip('/')}"


def _parse_catalogue_markdown(md: str, product: str, source_url: str) -> list[Feature]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    features: list[Feature] = []
    seen: set[str] = set()
    matches = list(_TITLE_LX_RE.finditer(md))

    for idx, m in enumerate(matches):
        title = m.group("title").strip(" *#-\t")
        if not title or len(title) < 4 or title.startswith("[") or title.lower() in seen:
            continue

        raw_links = dict(_LINK_RE.findall(m.group("links")))
        html_url  = _resolve_url(raw_links["HTML"], source_url) if "HTML" in raw_links else None
        pdf_url   = _resolve_url(raw_links["PDF"],  source_url) if "PDF"  in raw_links else None
        if not html_url and not pdf_url:
            continue

        end        = m.end()
        start_next = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
        desc       = md[end:start_next].strip().split("\n\n")[0].strip()

        rm = _RELEASE_RE.search(title) or (html_url and _RELEASE_RE.search(html_url)) or (pdf_url and _RELEASE_RE.search(pdf_url or ""))
        release = rm.group(1) if rm else "Unknown"

        seen.add(title.lower())
        features.append(Feature(
            release=release,
            product_family=product,
            product=PRODUCT_LABELS.get(product, product),
            module=_extract_module(title),
            feature_name=title,
            description=desc,
            html_url=html_url,
            pdf_url=pdf_url,
            source_url=source_url,
            retrieved_at=now,
        ))
    return features


# ---------------------------------------------------------------------------
# Strategy 2: oracle.com what's-new HTML table parser (TS oracle-readiness approach)
# ---------------------------------------------------------------------------

def _parse_whats_new_html(html: str, product: str, source_url: str) -> list[Feature]:
    now   = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    soup  = BeautifulSoup(html, "lxml")
    features: list[Feature] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

        def _idx(*names):
            for n in names:
                for i, h in enumerate(headers):
                    if n in h:
                        return i
            return -1

        i_feature    = _idx("feature")
        i_module     = _idx("module")
        i_product    = _idx("product")
        i_impact     = _idx("impact")
        i_enablement = _idx("action", "enable")
        i_auto_in    = _idx("auto", "expires")
        i_redwood    = _idx("redwood")
        i_ai         = _idx("ai")
        i_desc       = _idx("description", "short")
        i_update     = _idx("update")

        if i_feature == -1 and i_module == -1:
            continue

        for row in rows[1:]:
            cells = row.find_all("td")
            def get(i): return cells[i].get_text(strip=True) if 0 <= i < len(cells) else None

            feature_name = get(i_feature) or get(i_module) or ""
            if not feature_name or len(feature_name) < 3:
                continue

            release_raw  = get(i_update)
            rm           = _RELEASE_RE.search(release_raw or "") or _RELEASE_RE.search(source_url)
            release      = rm.group(1) if rm else "Unknown"

            enab = _parse_enablement(get(i_enablement))
            ai   = _parse_ai(get(i_ai))
            rw   = get(i_redwood)
            auto_in = get(i_auto_in)

            features.append(Feature(
                release=release,
                product_family=product,
                product=get(i_product) or PRODUCT_LABELS.get(product, product),
                module=get(i_module) or PRODUCT_LABELS.get(product, product),
                feature_name=feature_name,
                description=get(i_desc) or "",
                impact=_parse_impact(get(i_impact)),
                enablement=enab["enablement"],
                auto_enabled_in=None if auto_in == "Does not expire" else auto_in,
                is_redwood=bool(rw and rw.strip()),
                is_ai=ai["is_ai"],
                ai_type=ai["ai_type"],
                setup_required=enab["setup_required"],
                opt_in_required=enab["opt_in_required"],
                source_url=source_url,
                retrieved_at=now,
            ))
    return features


# ---------------------------------------------------------------------------
# XLSX dump ingestion (from oracle-readiness-mcp TS parser)
# ---------------------------------------------------------------------------

def parse_features_from_xlsx_dump(rows: list, headers: list[str], source_url: str = READINESS_APP_URL) -> list[Feature]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def h(*names):
        for n in names:
            for i, hdr in enumerate(headers):
                if n.lower() in hdr.lower():
                    return i
        return -1

    i_feature    = h("Feature")
    i_module     = h("Module")
    i_product    = h("Product")
    i_pillar     = h("Pillar")
    i_update     = next((i for i, hdr in enumerate(headers) if hdr.strip() == "Update"), -1)
    i_redwood    = h("Redwood")
    i_ai         = h("AI")
    i_impact     = h("Impact")
    i_action     = h("Action")
    i_auto_in    = h("Auto Enabled")
    i_desc       = h("Short Description")

    features: list[Feature] = []
    for row in rows:
        def get(i):
            if i < 0 or i >= len(row): return None
            v = row[i]
            if v is None or v == "" or str(v).strip() in ("#UNCALCULATED",): return None
            return str(v).strip()

        release = get(i_update)
        if not release:
            continue

        pillar = get(i_pillar) or "Unknown"
        pf = (
            "ERP" if "ERP" in pillar else
            "HCM" if "HCM" in pillar else
            "SCM" if "SCM" in pillar else
            "CX Sales" if "Sales" in pillar else
            "CX Service" if "Service" in pillar else
            re.sub(r"\(.*\)", "", pillar).strip()
        )

        enab = _parse_enablement(get(i_action))
        ai   = _parse_ai(get(i_ai))
        rw   = get(i_redwood)
        auto_in = get(i_auto_in)

        features.append(Feature(
            release=release,
            product_family=pf,
            product=get(i_product) or pf,
            module=get(i_module) or pf,
            feature_name=get(i_feature) or get(i_module) or "Unknown Feature",
            description=get(i_desc) or "",
            impact=_parse_impact(get(i_impact)),
            enablement=enab["enablement"],
            auto_enabled_in=None if auto_in == "Does not expire" else auto_in,
            is_redwood=bool(rw and rw.strip()),
            is_ai=ai["is_ai"],
            ai_type=ai["ai_type"],
            setup_required=enab["setup_required"],
            opt_in_required=enab["opt_in_required"],
            source_url=source_url,
            retrieved_at=now,
        ))
    return features


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    r = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30.0)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Fetch a single product (catalogue strategy, with what's-new enrichment)
# ---------------------------------------------------------------------------

async def fetch_product(client: httpx.AsyncClient, product: str) -> tuple[list[Feature], str]:
    all_url, fallback_url = _CATALOGUE_PAGES[product]
    try:
        resp     = await _get(client, all_url)
        used_url = all_url
    except httpx.HTTPStatusError:
        resp     = await _get(client, fallback_url)
        used_url = fallback_url

    md       = html_to_md(resp.text, heading_style="ATX")
    features = _parse_catalogue_markdown(md, product, used_url)

    # Enrich with what's-new HTML table data where available
    wn_url = _WHATS_NEW_PAGES.get(product)
    if wn_url:
        try:
            wn_resp  = await _get(client, wn_url)
            wn_feats = _parse_whats_new_html(wn_resp.text, product, wn_url)
            # Merge: update existing by feature_name key, add new
            existing = {f.feature_name.lower(): f for f in features}
            for wf in wn_feats:
                key = wf.feature_name.lower()
                if key in existing:
                    ef = existing[key]
                    ef.impact          = wf.impact or ef.impact
                    ef.enablement      = wf.enablement or ef.enablement
                    ef.auto_enabled_in = wf.auto_enabled_in or ef.auto_enabled_in
                    ef.is_redwood      = wf.is_redwood or ef.is_redwood
                    ef.is_ai           = wf.is_ai or ef.is_ai
                    ef.ai_type         = wf.ai_type or ef.ai_type
                    ef.setup_required  = wf.setup_required or ef.setup_required
                    ef.opt_in_required = wf.opt_in_required or ef.opt_in_required
                else:
                    existing[key] = wf
            features = list(existing.values())
        except Exception as e:
            logger.debug("What's-new enrichment failed for %s: %s", product, e)

    return features, used_url


# ---------------------------------------------------------------------------
# Document content fetching (ClaudeCode ContentCache concept, kept separate)
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    if not _HAVE_PYPDF:
        return "[PDF text extraction unavailable: install pypdf]"
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n\n".join(p.extract_text() or "" for p in reader.pages).strip()
    except Exception as e:
        return f"[PDF extraction failed: {e}]"


async def fetch_document_content(client: httpx.AsyncClient, url: str) -> dict:
    resp = await _get(client, url)
    ct   = resp.headers.get("content-type", "")
    if "pdf" in ct or url.lower().endswith(".pdf"):
        text     = _extract_pdf_text(resp.content)
        doc_type = "pdf"
    else:
        text     = html_to_md(resp.text, heading_style="ATX").strip()
        doc_type = "html"
    truncated = len(text) > MAX_CONTENT_CHARS
    return {
        "url":           url,
        "doc_type":      doc_type,
        "content":       text[:MAX_CONTENT_CHARS],
        "content_chars": min(len(text), MAX_CONTENT_CHARS),
        "truncated":     truncated,
        "fetched_at":    time.time(),
    }


# ---------------------------------------------------------------------------
# Deep detail scraper — individual feature .htm pages
# ---------------------------------------------------------------------------

_KNOWN_SECTIONS = {
    # normalised heading → storage key
    "steps to enable":                "steps_to_enable",
    "steps to enable and configure":  "steps_to_enable",
    "tips and considerations":        "tips_considerations",
    "tips & considerations":          "tips_considerations",
    "access requirements":            "access_requirements",
    "key resources":                  "key_resources",
    "business benefit":               "business_benefit",
    "business benefits":              "business_benefit",
}

_OPTIONAL_UPTAKE_RE = re.compile(
    r"optional\s+uptake|opt[\s-]*in\s+required|administrator.*must.*enable",
    re.IGNORECASE,
)


def _node_to_md(node, _depth: int = 0) -> str:
    """Recursively convert a BeautifulSoup node to clean Markdown-ish text.

    - <ol> items → "1. " "2. " …
    - <ul>/<li> → "• "
    - <strong>/<b> → **text**
    - <em>/<i> → *text*
    - <a> → text (URL dropped; Oracle links are mostly internal)
    - <img> → (skipped)
    - <br> → newline
    - <table> → kept as indented text rows
    - Block elements (p, div, li, tr) → separated by newline
    """
    from bs4 import NavigableString, Tag
    if isinstance(node, NavigableString):
        return str(node)

    tag = node.name
    if tag is None:
        return ""
    if tag in ("script", "style", "img", "noscript"):
        return ""
    if tag == "br":
        return "\n"

    # Recurse into children
    def _children_text(n, depth=_depth) -> str:
        return "".join(_node_to_md(c, depth) for c in n.children)

    if tag in ("strong", "b"):
        inner = _children_text(node).strip()
        return f"**{inner}**" if inner else ""
    if tag in ("em", "i"):
        inner = _children_text(node).strip()
        return f"*{inner}*" if inner else ""
    if tag == "a":
        return _children_text(node)  # keep link text, drop href
    if tag == "code":
        inner = _children_text(node).strip()
        return f"`{inner}`" if inner else ""

    if tag == "li":
        inner = _children_text(node).strip()
        return inner  # prefix added by ol/ul handler below

    if tag in ("ul",):
        items = [_node_to_md(li, _depth) for li in node.find_all("li", recursive=False)]
        return "\n".join(f"• {item.strip()}" for item in items if item.strip())

    if tag in ("ol",):
        items = [_node_to_md(li, _depth) for li in node.find_all("li", recursive=False)]
        return "\n".join(
            f"{i}. {item.strip()}" for i, item in enumerate(items, 1) if item.strip()
        )

    if tag == "p":
        return _children_text(node).strip()

    if tag == "table":
        # Flatten table rows as plain text lines
        rows_text = []
        for tr in node.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            rows_text.append(" | ".join(cells))
        return "\n".join(rows_text)

    # Block containers — just recurse with block separation
    if tag in ("div", "section", "article", "main", "header", "footer", "aside",
               "nav", "figure", "figcaption", "blockquote", "pre"):
        return _children_text(node)

    # Headings inside content (h3, h4 …)
    if tag in ("h3", "h4", "h5", "h6"):
        return f"### {_children_text(node).strip()}"

    # Default: recurse
    return _children_text(node)


def _section_to_text(nodes: list) -> str:
    """Render a list of BeautifulSoup nodes to clean text, joining blocks with blank lines."""
    parts: list[str] = []
    for node in nodes:
        txt = _node_to_md(node).strip()
        if txt:
            parts.append(txt)
    return "\n\n".join(parts)


def parse_feature_detail_page(
    html: str,
    page_url: str,
    release: str,
    product_family: str,
    module: str,
) -> dict:
    """Parse one Oracle feature detail .htm page and return a feature_details dict.

    Sections recognised:
        description_full    — content before the first <h2>
        business_benefit    — <h2>Business Benefit(s)</h2>
        steps_to_enable     — <h2>Steps to enable [and configure]</h2>
        tips_considerations — <h2>Tips and considerations</h2>
        access_requirements — <h2>Access requirements</h2>
        key_resources       — <h2>Key resources</h2>
        other_sections      — JSON list [{heading, content}] for any other h2 sections
    """
    soup = BeautifulSoup(html, "lxml")

    # Strip noise elements before any parsing
    for noise in soup.find_all(["script", "style", "noscript"]):
        noise.decompose()
    for div in soup.find_all("div", id="copyright"):
        div.decompose()
    for div in soup.find_all("div", class_="noscript"):
        div.decompose()

    # Feature name from <h1>
    h1 = soup.find("h1")
    feature_name = h1.get_text(strip=True) if h1 else ""

    # Locate main content container
    main = (soup.find("section")
            or soup.find("main")
            or soup.find("article")
            or soup.body)
    if not main:
        return {}

    # Walk direct children of main, splitting at <h2> boundaries
    desc_nodes:    list = []
    sections:      dict[str, list] = {}   # storage_key → [nodes]
    other:         list[dict]      = []
    cur_key:       Optional[str]   = None
    cur_heading:   str             = ""
    cur_nodes:     list            = []

    def _flush():
        nonlocal cur_nodes
        if cur_key:
            sections.setdefault(cur_key, []).extend(cur_nodes)
        elif cur_heading and cur_nodes:
            other.append({"heading": cur_heading,
                          "content": _section_to_text(cur_nodes)})
        cur_nodes = []

    for el in main.children:
        tag = getattr(el, "name", None)
        if tag is None:
            continue  # NavigableString whitespace
        if tag in ("script", "style", "noscript"):
            continue
        if tag == "h1":
            continue  # already captured

        if tag == "h2":
            _flush()
            heading_text = el.get_text(strip=True)
            normalised   = heading_text.lower().strip()
            cur_key      = _KNOWN_SECTIONS.get(normalised)
            cur_heading  = heading_text
            cur_nodes    = []
            continue

        if cur_key is None and cur_heading == "":
            # Pre-h2 description zone — skip echo of feature name
            txt_preview = el.get_text(strip=True)
            if txt_preview == feature_name:
                continue
            desc_nodes.append(el)
        else:
            cur_nodes.append(el)

    _flush()

    description_full = _section_to_text(desc_nodes)

    # Handle "Business Benefit" appearing inline as a bold <p> inside description
    if "business_benefit" not in sections:
        # Try to split description at the bold "Business Benefit" paragraph
        new_desc_nodes: list = []
        bb_nodes:       list = []
        in_bb = False
        for node in desc_nodes:
            tag = getattr(node, "name", None)
            if tag == "p":
                txt = node.get_text(strip=True)
                if re.match(r"^Business Benefits?$", txt, re.IGNORECASE):
                    in_bb = True
                    continue
            if in_bb:
                bb_nodes.append(node)
            else:
                new_desc_nodes.append(node)
        if bb_nodes:
            sections["business_benefit"] = bb_nodes
            description_full = _section_to_text(new_desc_nodes)

    def _sec(key: str) -> Optional[str]:
        nodes = sections.get(key)
        if not nodes:
            return None
        text = _section_to_text(nodes).strip()
        return text or None

    optional_uptake = bool(
        _OPTIONAL_UPTAKE_RE.search(_sec("steps_to_enable") or "")
        or _OPTIONAL_UPTAKE_RE.search(description_full)
        or _OPTIONAL_UPTAKE_RE.search(_sec("tips_considerations") or "")
    )

    return {
        "feature_page_url":    page_url,
        "feature_name":        feature_name,
        "release":             release,
        "product_family":      product_family,
        "module":              module,
        "description_full":    description_full,
        "business_benefit":    _sec("business_benefit"),
        "steps_to_enable":     _sec("steps_to_enable"),
        "tips_considerations": _sec("tips_considerations"),
        "access_requirements": _sec("access_requirements"),
        "key_resources":       _sec("key_resources"),
        "other_sections":      json.dumps(other) if other else None,
        "optional_uptake":     optional_uptake,
        "fetched_at":          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def _collect_feature_page_links(
    client: httpx.AsyncClient, index_html: str, index_url: str,
    max_pages: int = 300,
) -> list[str]:
    """Walk the full rel="next" chain from the module index and return ALL feature page URLs.

    Oracle's What's New docs are a chain of .htm pages linked by <link rel="next">.
    The chain includes:
      - Structural pages (*-wn-t<N>.htm): revision history, overview, intro — skipped
      - Feature pages (*-wn-f<N>.htm): one page per feature — collected

    We walk the entire chain so we get ALL features across ALL releases in this
    module's cumulative What's New document (not just features new in the current release).
    """
    base_dir  = index_url.rsplit("/", 1)[0]
    seen_urls: set[str] = set()
    feature_urls: list[str] = []

    # Start: follow the index's rel="next" to enter the chain
    soup_idx  = BeautifulSoup(index_html, "lxml")
    next_link = soup_idx.find("link", rel="next")
    if not next_link or not next_link.get("href"):
        return []
    next_href = next_link["href"].split("#")[0]
    current   = next_href if next_href.startswith("http") else f"{base_dir}/{next_href}"

    hops = 0
    while current and hops < max_pages:
        if current in seen_urls:
            break
        seen_urls.add(current)

        fname = current.split("/")[-1].split("#")[0]
        if re.search(r"-wn-f\d+\.htm", fname, re.IGNORECASE):
            feature_urls.append(current)

        try:
            r    = await _get(client, current)
            soup = BeautifulSoup(r.text, "lxml")
            nxt  = soup.find("link", rel="next")
            if not nxt or not nxt.get("href"):
                break
            nxt_href = nxt["href"].split("#")[0]
            current  = nxt_href if nxt_href.startswith("http") else f"{base_dir}/{nxt_href}"
        except Exception as e:
            logger.debug("Chain walk stopped at %s: %s", current, e)
            break
        hops += 1

    return feature_urls


async def fetch_feature_details_for_module(
    client: httpx.AsyncClient,
    index_url: str,
    release: str,
    product_family: str,
    module: str,
    concurrency: int = 4,
) -> list[dict]:
    """Fetch the module index page, collect all feature .htm links, parse each one.

    Returns a list of feature_details dicts ready for DB upsert.
    """
    try:
        index_resp = await _get(client, index_url)
    except Exception as e:
        logger.warning("Cannot fetch index %s: %s", index_url, e)
        return []

    links = await _collect_feature_page_links(client, index_resp.text, index_url)
    if not links:
        logger.debug("No feature links found in %s", index_url)
        return []

    logger.info("Deep-scraping %d features from %s (%s/%s/%s)",
                len(links), index_url, release, product_family, module)

    sem = asyncio.Semaphore(concurrency)
    details: list[dict] = []

    async def _fetch_one(url: str) -> Optional[dict]:
        async with sem:
            try:
                resp = await _get(client, url)
                return parse_feature_detail_page(
                    resp.text, url, release, product_family, module
                )
            except Exception as e:
                logger.debug("Feature detail fetch failed %s: %s", url, e)
                return None

    results = await asyncio.gather(*[_fetch_one(u) for u in links])
    for r in results:
        if r and r.get("feature_name"):
            details.append(r)

    return details


async def deep_scrape_product(
    client: httpx.AsyncClient,
    product: str,
    releases: Optional[list[str]] = None,
) -> tuple[int, int]:
    """Drive deep-scrape for all module index pages of a product.

    If `releases` is given only those releases are scraped (e.g. ["26C","26B"]).
    Returns (pages_scraped, features_fetched).
    """
    from db import ReadinessDB  # imported here to avoid circular at module load
    import os
    from pathlib import Path

    data_dir = Path(os.environ.get("READINESS_DATA_DIR", "/data")).resolve()
    db = ReadinessDB(data_dir / "readiness.db")

    # Get all module index urls for this product
    rows = db._execute(
        """
        SELECT DISTINCT html_url, module, release, product_family
        FROM features
        WHERE product_family = ?
          AND html_url LIKE '%index.html'
          AND html_url NOT LIKE '%www.oracle.com%'
        ORDER BY release DESC
        """,
        (product,),
    ).fetchall()

    if releases:
        releases_upper = [r.upper() for r in releases]
        rows = [r for r in rows if r["release"].upper() in releases_upper]

    pages = 0
    features = 0
    for row in rows:
        dets = await fetch_feature_details_for_module(
            client,
            row["html_url"],
            row["release"],
            row["product_family"],
            row["module"],
        )
        for d in dets:
            await db.upsert_feature_detail(d)
        pages  += 1
        features += len(dets)

    return pages, features
