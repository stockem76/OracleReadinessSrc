import { readFileSync, existsSync } from "fs";
import { parseFeaturesFromHtml, parseFeaturesFromXlsxDump, parseFeatureDetailFromHtml, READINESS_BASE, READINESS_CATALOGUE_PAGES, URL_TO_FAMILY } from "./parser.js";
import { upsertFeatures, upsertFeatureDetail, logCrawl, getCrawlLog, searchFeatures } from "./db.js";
import type { Feature } from "./types.js";

async function fetchWithTimeout(url: string, timeoutMs = 15_000): Promise<string> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: ctrl.signal,
      headers: {
        "User-Agent": "OracleReadinessMCP/0.1 (research tool)",
        "Accept": "text/html,application/xhtml+xml,*/*",
      },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
    return await res.text();
  } finally {
    clearTimeout(timer);
  }
}

function familyFromUrl(url: string): string {
  for (const [key, family] of Object.entries(URL_TO_FAMILY)) {
    if (url.includes(key)) return family;
  }
  return "Unknown";
}

export async function crawlPage(url: string, release: string, productFamily: string): Promise<{ count: number; errors: string[] }> {
  const errors: string[] = [];
  try {
    const html = await fetchWithTimeout(url);
    const features = parseFeaturesFromHtml(html, release, productFamily, url);
    if (features.length > 0) {
      await upsertFeatures(features);
      await logCrawl(url, release, features.length);
    }
    return { count: features.length, errors };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    errors.push(`Failed to crawl ${url}: ${msg}`);
    return { count: 0, errors };
  }
}

export async function crawlRelease(release: string): Promise<{ total: number; pages: number; errors: string[] }> {
  const allErrors: string[] = [];
  let total = 0;
  let pages = 0;

  for (const path of READINESS_CATALOGUE_PAGES) {
    const url = `${READINESS_BASE}${path}`;
    const family = familyFromUrl(url);
    const releaseUrls = [
      url,
      `${READINESS_BASE}${path}${release.toLowerCase()}/`,
      `${READINESS_BASE}${path}?update=${release}`,
    ];
    for (const tryUrl of releaseUrls) {
      const { count, errors } = await crawlPage(tryUrl, release, family);
      allErrors.push(...errors);
      if (count > 0) { total += count; pages++; break; }
    }
  }
  return { total, pages, errors: allErrors };
}

export async function ingestXlsxDump(jsonPath: string, sourceUrl?: string): Promise<{ count: number; errors: string[] }> {
  const errors: string[] = [];
  if (!existsSync(jsonPath)) {
    return { count: 0, errors: [`File not found: ${jsonPath}`] };
  }
  try {
    const raw = JSON.parse(readFileSync(jsonPath, "utf-8")) as { headers: string[]; rows: unknown[][] };
    const features = parseFeaturesFromXlsxDump(raw.rows, raw.headers, sourceUrl ?? `file://${jsonPath}`);
    if (features.length > 0) {
      await upsertFeatures(features);
      await logCrawl(jsonPath, features[0]?.release ?? "unknown", features.length);
    }
    return { count: features.length, errors };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    errors.push(`Failed to parse ${jsonPath}: ${msg}`);
    return { count: 0, errors };
  }
}

/**
 * Crawl the detail page for a single feature and store the result.
 * `featureUrl` must be the direct URL to the Oracle feature detail page.
 * Returns true if at least one non-null section was found.
 */
export async function crawlOneFeatureDetail(
  featureId: number,
  featureName: string,
  release: string,
  featureUrl: string
): Promise<{ ok: boolean; error?: string }> {
  try {
    const html = await fetchWithTimeout(featureUrl);
    const detail = parseFeatureDetailFromHtml(html, featureId, release, featureName, featureUrl);
    await upsertFeatureDetail(detail);
    const hasContent =
      detail.steps_to_enable !== null ||
      detail.business_benefit !== null ||
      detail.key_resources !== null ||
      detail.tips !== null;
    return { ok: hasContent };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/**
 * Crawl detail pages for all features in a release that have a source_url
 * pointing to an individual feature page (not a catalogue/table page).
 * Skips features whose detail is already stored unless `force` is true.
 *
 * `concurrency` controls parallel fetch slots (default 3 ÔÇö polite crawling).
 */
export async function crawlFeatureDetails(
  release: string,
  options: { module?: string; force?: boolean; concurrency?: number } = {}
): Promise<{ total: number; succeeded: number; skipped: number; errors: string[] }> {
  const { module, force = false, concurrency = 3 } = options;

  // Load the features we want to detail-crawl
  const features = await searchFeatures("", release, module);

  const errors: string[] = [];
  let succeeded = 0;
  let skipped = 0;

  // Work through the list in batches of `concurrency`
  for (let i = 0; i < features.length; i += concurrency) {
    const batch = features.slice(i, i + concurrency);
    await Promise.all(batch.map(async (f) => {
      if (!f.id) { skipped++; return; }

      // Skip features without a real detail URL (catalogue pages aren't detail pages)
      const url = f.source_url;
      if (!url || READINESS_CATALOGUE_PAGES.some(p => url.includes(p.replace(/\/$/, "")))) {
        skipped++;
        return;
      }

      if (!force) {
        // Check if we already have a detail record for this feature
        const existing = await (await import("./db.js")).getFeatureDetail(f.id);
        if (existing) { skipped++; return; }
      }

      const { ok, error } = await crawlOneFeatureDetail(f.id, f.feature_name, f.release, url);
      if (ok) succeeded++;
      else if (error) errors.push(`[${f.feature_name}] ${error}`);
      else skipped++; // page had no recognised sections
    }));
  }

  return { total: features.length, succeeded, skipped, errors };
}

export async function getCrawlHistory(): Promise<{source_url: string; release: string; crawled_at: string; row_count: number}[]> {
  return getCrawlLog();
}
