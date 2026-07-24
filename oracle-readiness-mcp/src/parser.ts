/**
 * Oracle Readiness HTML/table parser.
 * Handles both the Oracle Readiness Reports Centre custom-report XLSX format
 * (already downloaded) and direct HTML parsing from Oracle's readiness pages.
 */
import { parse as parseHtml, type HTMLElement } from "node-html-parser";
import type { Feature, FeatureDetail } from "./types.js";

// Known Oracle readiness page paths
export const READINESS_BASE = "https://www.oracle.com";
export const READINESS_CATALOGUE_PAGES = [
  "/cloud/saas/erp/whats-new/",
  "/cloud/saas/human-resources/whats-new/",
  "/cloud/saas/supply-chain-management/whats-new/",
  "/cloud/saas/sales/whats-new/",
  "/cloud/saas/service/whats-new/",
  "/cloud/saas/marketing/whats-new/",
  "/cloud/saas/common-technologies/whats-new/",
];

// Map product URL path to product family label
export const URL_TO_FAMILY: Record<string, string> = {
  "erp":             "ERP",
  "human-resources": "HCM",
  "supply-chain":    "SCM",
  "sales":           "CX Sales",
  "service":         "CX Service",
  "marketing":       "CX Marketing",
  "common":          "Common",
};

// Detect impact level from a cell value
function parseImpact(val: string | null): string | null {
  if (!val) return null;
  const v = val.toLowerCase();
  if (v.includes("large")) return "Large scale";
  if (v.includes("small")) return "Small scale";
  if (v.includes("report")) return "Report";
  return val.trim() || null;
}

// Detect enablement type
function parseEnablement(val: string | null): { enablement: string | null; setup_required: boolean; opt_in_required: boolean } {
  if (!val) return { enablement: null, setup_required: false, opt_in_required: false };
  const v = val.toLowerCase().trim();
  const setup_required = v.includes("setup required") || v.includes("setup-required");
  const opt_in_required = v.includes("opt in") || v.includes("opt-in");
  return { enablement: val.trim() || null, setup_required, opt_in_required };
}

// Detect AI classification
function parseAi(val: string | null): { is_ai: boolean; ai_type: string | null } {
  if (!val) return { is_ai: false, ai_type: null };
  const v = val.toLowerCase().trim();
  if (!v) return { is_ai: false, ai_type: null };
  const is_ai = true;
  if (v.includes("agentic")) return { is_ai, ai_type: "Agentic App" };
  if (v.includes("agent")) return { is_ai, ai_type: "Agent" };
  if (v.includes("generative")) return { is_ai, ai_type: "Generative" };
  return { is_ai, ai_type: val.trim() };
}

// Parse an Oracle readiness HTML page and extract features from <table> tags.
// Oracle's What's New pages use consistent table layouts across releases.
export function parseFeaturesFromHtml(
  html: string,
  release: string,
  productFamily: string,
  sourceUrl: string
): Feature[] {
  const root = parseHtml(html);
  const features: Feature[] = [];
  const now = new Date().toISOString();

  // Find all tables that look like feature tables
  const tables = root.querySelectorAll("table");
  for (const table of tables) {
    const rows = table.querySelectorAll("tr");
    if (rows.length < 2) continue;

    // Read header row to detect column positions
    const headerCells = rows[0].querySelectorAll("th, td").map(c => c.text.trim().toLowerCase());
    const idx = {
      feature:     headerCells.findIndex(h => h.includes("feature")),
      module:      headerCells.findIndex(h => h.includes("module")),
      product:     headerCells.findIndex(h => h.includes("product")),
      impact:      headerCells.findIndex(h => h.includes("impact")),
      enablement:  headerCells.findIndex(h => h.includes("action") || h.includes("enable")),
      auto_in:     headerCells.findIndex(h => h.includes("auto") || h.includes("expires")),
      redwood:     headerCells.findIndex(h => h.includes("redwood")),
      ai:          headerCells.findIndex(h => h === "ai"),
      description: headerCells.findIndex(h => h.includes("description") || h.includes("short")),
    };

    // Skip tables that don't look like feature tables
    if (idx.feature === -1 && idx.module === -1) continue;

    for (let i = 1; i < rows.length; i++) {
      const cells = rows[i].querySelectorAll("td");
      const get = (n: number): string | null => n >= 0 && cells[n] ? cells[n].text.trim() || null : null;

      const featureName = get(idx.feature) ?? get(idx.module) ?? `Row ${i}`;
      if (!featureName || featureName.length < 3) continue;

      const enablementRaw = get(idx.enablement);
      const { enablement, setup_required, opt_in_required } = parseEnablement(enablementRaw);
      const aiRaw = get(idx.ai);
      const { is_ai, ai_type } = parseAi(aiRaw);
      const redwoodRaw = get(idx.redwood);
      const is_redwood = !!(redwoodRaw && redwoodRaw.trim() !== "");
      const auto_enabled_in = get(idx.auto_in);

      features.push({
        release,
        product_family: productFamily,
        product: get(idx.product) ?? productFamily,
        module: get(idx.module) ?? productFamily,
        feature_name: featureName,
        description: get(idx.description) ?? "",
        impact: parseImpact(get(idx.impact)),
        enablement,
        auto_enabled_in: auto_enabled_in === "Does not expire" ? null : auto_enabled_in,
        is_redwood,
        is_ai,
        ai_type,
        setup_required,
        opt_in_required,
        source_url: sourceUrl,
        retrieved_at: now,
      });
    }
  }
  return features;
}

// Parse features from the Oracle Readiness Report Centre XLSX export
// format that was dumped to JSON by the xlsx-dump tool.
// This handles the "Feature Summary" sheet with headers:
//   Date Added, Last Updated, Feature, Module, Product, Pillar,
//   Update, Redwood, AI, Impact to Existing Processes, Action to Enable,
//   Auto Enabled In, Short Description
export function parseFeaturesFromXlsxDump(
  rows: unknown[][],
  headers: string[],
  sourceUrl: string = "https://www.oracle.com/WEBFOLDER/TECHNETWORK/TUTORIALS/TUTORIAL/READINESS/APP/INDEX.HTML"
): Feature[] {
  const now = new Date().toISOString();
  const h = (name: string) => headers.findIndex(x => x.toLowerCase().includes(name.toLowerCase()));

  const iFeature    = h("Feature");
  const iModule     = h("Module");
  const iProduct    = h("Product");
  const iPillar     = h("Pillar");
  const iUpdate     = headers.findIndex(x => x.trim() === "Update");
  const iRedwood    = h("Redwood");
  const iAi         = h("AI");
  const iImpact     = h("Impact");
  const iAction     = h("Action");
  const iAutoIn     = h("Auto Enabled");
  const iDesc       = h("Short Description");

  const features: Feature[] = [];

  for (const row of rows) {
    const get = (i: number): string | null => {
      if (i < 0 || i >= row.length) return null;
      const v = row[i];
      if (v === null || v === undefined || v === "") return null;
      const s = String(v).trim();
      return s === "#UNCALCULATED" || s === "" ? null : s;
    };

    const release = get(iUpdate);
    if (!release) continue;

    const pillar = get(iPillar) ?? "Unknown";
    // Normalise pillar to short family name
    const productFamily = pillar.includes("ERP") ? "ERP"
      : pillar.includes("HCM") ? "HCM"
      : pillar.includes("SCM") ? "SCM"
      : pillar.includes("Sales") ? "CX Sales"
      : pillar.includes("Service") ? "CX Service"
      : pillar.includes("Marketing") ? "CX Marketing"
      : pillar.replace(/\(.*\)/, "").trim();

    const featureName = get(iFeature) ?? get(iModule) ?? "Unknown Feature";
    const enablementRaw = get(iAction);
    const { enablement, setup_required, opt_in_required } = parseEnablement(enablementRaw);
    const aiRaw = get(iAi);
    const { is_ai, ai_type } = parseAi(aiRaw);
    const redwoodRaw = get(iRedwood);
    const is_redwood = !!(redwoodRaw && redwoodRaw.trim() !== "");
    const autoIn = get(iAutoIn);

    features.push({
      release,
      product_family: productFamily,
      product: get(iProduct) ?? productFamily,
      module: get(iModule) ?? productFamily,
      feature_name: featureName,
      description: get(iDesc) ?? "",
      impact: parseImpact(get(iImpact)),
      enablement,
      auto_enabled_in: autoIn === "Does not expire" ? null : autoIn,
      is_redwood,
      is_ai,
      ai_type,
      setup_required,
      opt_in_required,
      source_url: sourceUrl,
      retrieved_at: now,
    });
  }
  return features;
}

// ÔöÇÔöÇÔöÇ Feature detail page parser ÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇ

// Section heading aliases used on Oracle's individual feature detail pages.
// Each entry is [canonicalKey, ...matchStrings (lowercase)].
const DETAIL_SECTIONS: [keyof Pick<FeatureDetail, "steps_to_enable" | "business_benefit" | "key_resources" | "tips">, string[]][] = [
  ["steps_to_enable",  ["steps to enable", "step to enable", "how to enable", "enable this feature"]],
  ["business_benefit", ["business benefit", "business benefits", "benefits"]],
  ["key_resources",    ["key resources", "key resource", "resources", "related resources"]],
  ["tips",             ["tips and considerations", "tips & considerations", "tips", "considerations"]],
];

function matchSection(heading: string): keyof Pick<FeatureDetail, "steps_to_enable" | "business_benefit" | "key_resources" | "tips"> | null {
  const h = heading.toLowerCase().trim();
  for (const [key, aliases] of DETAIL_SECTIONS) {
    if (aliases.some(a => h.includes(a))) return key;
  }
  return null;
}

/**
 * Parse an individual Oracle feature detail page.
 *
 * Oracle's detail pages present named sections as <h2>/<h3> headings followed
 * by paragraphs / lists / tables.  We walk the top-level children of the main
 * content area, collecting text under each recognised heading until the next
 * heading of the same or higher level.
 */
export function parseFeatureDetailFromHtml(
  html: string,
  featureId: number,
  release: string,
  featureName: string,
  sourceUrl: string
): FeatureDetail {
  const root = parseHtml(html);
  const now = new Date().toISOString();

  const result: FeatureDetail = {
    feature_id:       featureId,
    release,
    feature_name:     featureName,
    steps_to_enable:  null,
    business_benefit: null,
    key_resources:    null,
    tips:             null,
    source_url:       sourceUrl,
    retrieved_at:     now,
  };

  // Try to find a main content container; fall back to <body>
  const contentRoot =
    root.querySelector("main") ??
    root.querySelector('[role="main"]') ??
    root.querySelector(".oj-sm-padding-4x-horizontal") ??
    root.querySelector("article") ??
    root.querySelector("body") ??
    root;

  // Collect all block-level elements in document order
  const blocks = contentRoot.querySelectorAll("h1,h2,h3,h4,p,ul,ol,table,div.section,section");

  let currentKey: keyof Pick<FeatureDetail, "steps_to_enable" | "business_benefit" | "key_resources" | "tips"> | null = null;
  const buckets: Record<string, string[]> = {
    steps_to_enable: [], business_benefit: [], key_resources: [], tips: [],
  };

  for (const el of blocks) {
    const tag = el.tagName.toLowerCase();
    const isHeading = tag === "h1" || tag === "h2" || tag === "h3" || tag === "h4";

    if (isHeading) {
      const matched = matchSection(el.text);
      currentKey = matched;
      continue;
    }

    if (!currentKey) continue;

    // For key_resources, prefer link hrefs + labels
    if (currentKey === "key_resources") {
      const links = el.querySelectorAll("a[href]");
      if (links.length > 0) {
        for (const a of links) {
          const label = a.text.trim();
          const href  = a.getAttribute("href") ?? "";
          if (label) buckets["key_resources"].push(href ? `${label} (${href})` : label);
        }
        continue;
      }
    }

    const text = el.text.trim();
    if (text) buckets[currentKey].push(text);
  }

  // Collapse each bucket to a single string, deduplicating repeated lines
  for (const [key, lines] of Object.entries(buckets)) {
    const unique = [...new Set(lines.filter(Boolean))];
    if (unique.length > 0) {
      (result as unknown as Record<string, string | null>)[key] = unique.join("\n").trim();
    }
  }

  return result;
}

// Extract candidate release links from an Oracle readiness catalogue HTML page
export function extractReleaseLinks(html: string, baseUrl: string): { release: string; url: string }[] {
  const root = parseHtml(html);
  const results: { release: string; url: string }[] = [];
  const seen = new Set<string>();

  for (const a of root.querySelectorAll("a[href]")) {
    const href = a.getAttribute("href") ?? "";
    const text = a.text.trim();
    // Match release patterns: 26C, 25A, 27B etc.
    const releaseMatch = text.match(/\b(2[0-9][A-D])\b/) ?? href.match(/\/(2[0-9][a-d])\//i);
    if (releaseMatch) {
      const release = releaseMatch[1].toUpperCase();
      const fullUrl = href.startsWith("http") ? href : `${baseUrl}${href}`;
      const key = `${release}:${fullUrl}`;
      if (!seen.has(key)) {
        seen.add(key);
        results.push({ release, url: fullUrl });
      }
    }
  }
  return results;
}
