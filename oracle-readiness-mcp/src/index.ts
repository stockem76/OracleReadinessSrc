#!/usr/bin/env node
/**
 * Oracle Fusion Readiness MCP Server
 *
 * Exposes Oracle Cloud Applications release notes as searchable,
 * filterable, comparable MCP tools.
 *
 * Tools:
 *   list_releases            ÔÇô list all indexed Oracle releases
 *   list_product_families    ÔÇô list product families (HCM, ERP, SCMÔÇª)
 *   list_modules             ÔÇô list modules for a release / family
 *   search_release_notes     ÔÇô full-text search across features
 *   get_release_notes        ÔÇô all features for a release + module
 *   get_feature_summary      ÔÇô stats and breakdown for a module in a release
 *   compare_releases         ÔÇô diff two releases for a module
 *   get_opt_in_features      ÔÇô features requiring Opt-In in a release
 *   get_setup_required       ÔÇô features requiring Setup in a release
 *   get_high_impact_features ÔÇô Large-scale impact features in a release
 *   get_auto_enabled_featuresÔÇô features that auto-enable in a future update
 *   get_ai_features          ÔÇô AI/Agent features in a release
 *   crawl_release            ÔÇô fetch + index a release from Oracle's website
 *   ingest_xlsx_dump         ÔÇô load a local Oracle Readiness XLSX JSON dump
 *   get_crawl_status         ÔÇô show crawl history and cache stats
 *   get_feature_details      ÔÇô rich detail sections for a specific feature
 *   crawl_feature_details    ÔÇô fetch and store detail pages for a release
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer, type IncomingMessage, type ServerResponse } from "http";
import { z } from "zod";
import {
  listReleases,
  listProductFamilies,
  listModules,
  searchFeatures,
  getFeaturesByModule,
  getFilteredFeatures,
  getFeatureDetail,
  getFeatureDetailByName,
} from "./db.js";
import { crawlRelease, ingestXlsxDump, getCrawlHistory, crawlFeatureDetails } from "./crawler.js";
import type { Feature, FeatureSummary, ReleaseComparison } from "./types.js";

// ÔöÇÔöÇÔöÇ helpers ÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇ

async function summarise(features: Feature[], release: string, module: string, productFamily: string): Promise<FeatureSummary> {
  return {
    release,
    product_family: productFamily,
    module,
    total: features.length,
    large_scale: features.filter(f => f.impact?.toLowerCase().includes("large")).length,
    small_scale: features.filter(f => f.impact?.toLowerCase().includes("small")).length,
    setup_required: features.filter(f => f.setup_required).length,
    opt_in_required: features.filter(f => f.opt_in_required).length,
    auto_enabled: features.filter(f => f.auto_enabled_in !== null).length,
    redwood: features.filter(f => f.is_redwood).length,
    ai_features: features.filter(f => f.is_ai).length,
    features,
  };
}

function compareFeatureLists(oldFeatures: Feature[], newFeatures: Feature[], module: string, oldRelease: string, newRelease: string): ReleaseComparison {
  const oldNames = new Set(oldFeatures.map(f => f.feature_name.toLowerCase()));
  const newNames = new Set(newFeatures.map(f => f.feature_name.toLowerCase()));

  const added = newFeatures.filter(f => !oldNames.has(f.feature_name.toLowerCase()));
  const removedNames = oldFeatures.filter(f => !newNames.has(f.feature_name.toLowerCase())).map(f => f.feature_name);

  // Changed = same name but different impact/enablement
  const changed: Feature[] = [];
  for (const nf of newFeatures) {
    const of_ = oldFeatures.find(f => f.feature_name.toLowerCase() === nf.feature_name.toLowerCase());
    if (of_ && (of_.impact !== nf.impact || of_.enablement !== nf.enablement || of_.setup_required !== nf.setup_required)) {
      changed.push(nf);
    }
  }

  return {
    module,
    old_release: oldRelease,
    new_release: newRelease,
    added,
    changed,
    removed_names: removedNames,
    new_large_scale: added.filter(f => f.impact?.toLowerCase().includes("large")),
    new_setup_required: added.filter(f => f.setup_required),
    new_opt_in: added.filter(f => f.opt_in_required),
    new_auto_enabled: added.filter(f => f.auto_enabled_in !== null),
  };
}

function ok(data: unknown): { content: { type: "text"; text: string }[] } {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
}

function err(msg: string): { content: { type: "text"; text: string }[]; isError: true } {
  return { content: [{ type: "text" as const, text: msg }], isError: true as const };
}

// ÔöÇÔöÇÔöÇ server setup ÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇ

const server = new McpServer({
  name: "oracle-readiness-mcp",
  version: "0.1.0",
});

// ÔöÇÔöÇÔöÇ tools ÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇ

server.registerTool(
  "list_releases",
  {
    description: "List all Oracle Fusion releases currently indexed in the local cache (e.g. 26C, 26B, 25D). Run crawl_release or ingest_xlsx_dump first if the cache is empty.",
    inputSchema: z.object({}),
  },
  async () => {
    const releases = await listReleases();
    if (releases.length === 0) {
      return ok({ releases: [], message: "No releases indexed yet. Use crawl_release or ingest_xlsx_dump to load data." });
    }
    return ok({ releases });
  }
);

server.registerTool(
  "list_product_families",
  {
    description: "List Oracle Fusion product families (HCM, ERP, SCM, CX Sales, CX ServiceÔÇª) available in the cache, optionally filtered to a specific release.",
    inputSchema: z.object({
      release: z.string().optional().describe("Oracle release code e.g. '26C'. Omit for all releases."),
    }),
  },
  async ({ release }) => {
    const families = await listProductFamilies(release);
    return ok({ product_families: families, release: release ?? "all" });
  }
);

server.registerTool(
  "list_modules",
  {
    description: "List all modules available for a given release and optional product family.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      product_family: z.string().optional().describe("Product family e.g. 'HCM', 'ERP'. Omit for all families."),
    }),
  },
  async ({ release, product_family }) => {
    const modules = await listModules(release, product_family);
    return ok({ release, product_family: product_family ?? "all", modules });
  }
);

server.registerTool(
  "search_release_notes",
  {
    description: "Full-text search across Oracle Fusion release-note features. Returns up to 100 matching features. Supports multi-word queries.",
    inputSchema: z.object({
      query: z.string().describe("Search terms e.g. 'payroll balance groups', 'Redwood absence', 'HCM data loader'"),
      release: z.string().optional().describe("Restrict to a specific release e.g. '26C'"),
      module: z.string().optional().describe("Restrict to a module (substring match) e.g. 'Global Payroll', 'Absence'"),
      product_family: z.string().optional().describe("Restrict to a product family e.g. 'HCM', 'ERP'"),
    }),
  },
  async ({ query, release, module, product_family }) => {
    const features = await searchFeatures(query, release, module, product_family);
    return ok({ query, release, module, product_family, total: features.length, features });
  }
);

server.registerTool(
  "get_release_notes",
  {
    description: "Get all Oracle Fusion release-note features for a specific release and module.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().describe("Module name (substring match) e.g. 'Global Payroll', 'Absence Management', 'Global Human Resources'"),
    }),
  },
  async ({ release, module }) => {
    const features = await getFeaturesByModule(release, module);
    if (features.length === 0) {
      return ok({ release, module, total: 0, features: [], message: `No features found for module '${module}' in release ${release}. Try list_modules to see available modules.` });
    }
    return ok({ release, module, total: features.length, features });
  }
);

server.registerTool(
  "get_feature_summary",
  {
    description: "Get a statistical summary of Oracle Fusion features for a release and module: totals by impact level, setup requirement, AI/Redwood flags etc.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().describe("Module name (substring match) e.g. 'Global Payroll'"),
    }),
  },
  async ({ release, module }) => {
    const features = await getFeaturesByModule(release, module);
    const family = features[0]?.product_family ?? "Unknown";
    const summary = await summarise(features, release, module, family);
    return ok(summary);
  }
);

server.registerTool(
  "compare_releases",
  {
    description: "Compare Oracle Fusion features between two releases for a module. Shows added, changed, and removed features, plus new large-scale and setup-required items.",
    inputSchema: z.object({
      module: z.string().describe("Module name (substring match) e.g. 'Global Payroll', 'Absence Management'"),
      old_release: z.string().describe("Earlier release e.g. '26B'"),
      new_release: z.string().describe("Later release e.g. '26C'"),
    }),
  },
  async ({ module, old_release, new_release }) => {
    const oldFeatures = await getFeaturesByModule(old_release, module);
    const newFeatures = await getFeaturesByModule(new_release, module);
    if (oldFeatures.length === 0 && newFeatures.length === 0) {
      return err(`No features found for module '${module}' in either ${old_release} or ${new_release}. Use list_modules to check available modules.`);
    }
    const comparison = compareFeatureLists(oldFeatures, newFeatures, module, old_release, new_release);
    return ok(comparison);
  }
);

server.registerTool(
  "get_opt_in_features",
  {
    description: "Get Oracle Fusion features in a release that require an Opt-In action to enable, optionally filtered to a module.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().optional().describe("Module name (substring match). Omit for all modules."),
    }),
  },
  async ({ release, module }) => {
    const features = await getFilteredFeatures(release, "opt_in", module);
    return ok({ release, module, filter: "opt_in_required", total: features.length, features });
  }
);

server.registerTool(
  "get_setup_required_features",
  {
    description: "Get Oracle Fusion features in a release that require Setup configuration to be enabled, optionally filtered to a module.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().optional().describe("Module name (substring match). Omit for all modules."),
    }),
  },
  async ({ release, module }) => {
    const features = await getFilteredFeatures(release, "setup_required", module);
    return ok({ release, module, filter: "setup_required", total: features.length, features });
  }
);

server.registerTool(
  "get_high_impact_features",
  {
    description: "Get Oracle Fusion features flagged as 'Large scale' impact in a release, optionally filtered to a module. These are the features that most require testing and change management.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().optional().describe("Module name (substring match). Omit for all modules."),
    }),
  },
  async ({ release, module }) => {
    const features = await getFilteredFeatures(release, "large_scale", module);
    return ok({ release, module, filter: "large_scale", total: features.length, features });
  }
);

server.registerTool(
  "get_auto_enabled_features",
  {
    description: "Get Oracle Fusion features that will be automatically enabled in a future update (e.g. Redwood pages switching on by default in 26D). These require proactive review.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().optional().describe("Module name (substring match). Omit for all modules."),
    }),
  },
  async ({ release, module }) => {
    const features = await getFilteredFeatures(release, "auto_enabled", module);
    return ok({ release, module, filter: "auto_enabled_future", total: features.length, features });
  }
);

server.registerTool(
  "get_ai_features",
  {
    description: "Get Oracle Fusion AI and Agent features in a release, optionally filtered to a module. Includes Agent, Agentic App, and Generative AI features.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().optional().describe("Module name (substring match). Omit for all modules."),
    }),
  },
  async ({ release, module }) => {
    const features = await getFilteredFeatures(release, "ai", module);
    return ok({ release, module, filter: "ai_features", total: features.length, features });
  }
);

server.registerTool(
  "crawl_release",
  {
    description: "Crawl Oracle's public Cloud Applications readiness website for a specific release and store the features in the local SQLite cache. Requires internet access to oracle.com.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C', '27A'"),
    }),
  },
  async ({ release }) => {
    const result = await crawlRelease(release.toUpperCase());
    return ok({
      release: release.toUpperCase(),
      features_indexed: result.total,
      pages_crawled: result.pages,
      errors: result.errors,
      message: result.total > 0
        ? `Successfully indexed ${result.total} features from ${result.pages} pages for release ${release.toUpperCase()}.`
        : `No features found. Oracle's readiness pages may require authentication or the URL structure may have changed. Use ingest_xlsx_dump to load a manually downloaded report instead.`,
    });
  }
);

server.registerTool(
  "ingest_xlsx_dump",
  {
    description: "Load Oracle Fusion release features from a local XLSX JSON dump file (produced by the Oracle Readiness Reports Centre download). The file must be the Feature_Summary.json produced by the xlsx-dump tool, or any JSON with {headers: string[], rows: unknown[][]} shape matching the Oracle readiness report format.",
    inputSchema: z.object({
      json_path: z.string().describe("Absolute path to the Feature_Summary.json dump file e.g. 'G:/My Drive/GIT_ROOT/.bob/tmp/xlsx-dumps/Custom Report_7_20_2026-9fb204ec2e969ccf/Feature_Summary.json'"),
      source_url: z.string().optional().describe("Optional source URL to record. Defaults to the Oracle Readiness Reports Centre URL."),
    }),
  },
  async ({ json_path, source_url }) => {
    const result = await ingestXlsxDump(json_path, source_url);
    if (result.errors.length > 0 && result.count === 0) {
      return err(result.errors.join("\n"));
    }
    return ok({
      features_loaded: result.count,
      errors: result.errors,
      message: `Successfully loaded ${result.count} features from ${json_path}`,
    });
  }
);

server.registerTool(
  "get_feature_details",
  {
    description: "Get the rich detail sections for a specific Oracle Fusion feature: Steps to Enable, Business Benefit, Key Resources, and Tips & Considerations. These are scraped from the individual feature detail page. Returns null sections if the detail page has not yet been crawled ÔÇö use crawl_feature_details to populate them.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      feature_name: z.string().describe("Exact or approximate feature name. Case-insensitive exact match is tried first."),
      feature_id: z.number().optional().describe("Feature DB id (from search_release_notes or get_release_notes). Preferred over name when available."),
    }),
  },
  async ({ release, feature_name, feature_id }) => {
    let detail = null;
    if (feature_id !== undefined) {
      detail = await getFeatureDetail(feature_id);
    }
    if (!detail) {
      detail = await getFeatureDetailByName(release, feature_name);
    }
    if (!detail) {
      return ok({
        release,
        feature_name,
        detail: null,
        message: `No detail record found for '${feature_name}' in ${release}. Run crawl_feature_details to populate detail pages, or ensure the feature was indexed with a direct detail-page URL.`,
      });
    }
    return ok({ release, feature_name, detail });
  }
);

server.registerTool(
  "crawl_feature_details",
  {
    description: "Crawl the individual Oracle feature detail pages for a release to populate Steps to Enable, Business Benefit, Key Resources, and Tips sections. Only fetches features whose source_url points to a direct feature page (not catalogue tables). Skips already-crawled features unless force=true. Runs up to 3 fetches in parallel.",
    inputSchema: z.object({
      release: z.string().describe("Oracle release code e.g. '26C'"),
      module: z.string().optional().describe("Restrict to a module (substring match). Omit for all modules."),
      force: z.boolean().optional().describe("Re-crawl features that already have stored detail. Default false."),
      concurrency: z.number().optional().describe("Parallel fetch slots (1ÔÇô10). Default 3."),
    }),
  },
  async ({ release, module, force, concurrency }) => {
    const safeConc = Math.min(Math.max(concurrency ?? 3, 1), 10);
    const result = await crawlFeatureDetails(release, { module, force, concurrency: safeConc });
    return ok({
      release,
      module: module ?? "all",
      features_attempted: result.total,
      details_stored: result.succeeded,
      skipped: result.skipped,
      errors: result.errors,
      message: result.succeeded > 0
        ? `Stored detail sections for ${result.succeeded} features. ${result.skipped} skipped (already cached or no detail URL). ${result.errors.length} errors.`
        : `No detail sections stored. ${result.skipped} features skipped (already cached, no detail URL, or page had no recognised sections). ${result.errors.length} errors.`,
    });
  }
);

server.registerTool(
  "get_crawl_status",
  {
    description: "Show the crawl/ingestion history and a count of features in the local cache by release and product family.",
    inputSchema: z.object({}),
  },
  async () => {
    const history = await getCrawlHistory();
    const releases = await listReleases();
    const families = await listProductFamilies();
    return ok({
      cached_releases: releases,
      cached_product_families: families,
      crawl_history: history,
      message: releases.length === 0
        ? "Cache is empty. Use ingest_xlsx_dump or crawl_release to load data."
        : `Cache contains data for ${releases.length} release(s): ${releases.join(", ")}`,
    });
  }
);

// ÔöÇÔöÇÔöÇ main ÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇÔöÇ

const MODE   = process.env["MCP_MODE"]  ?? "stdio";   // "stdio" | "http"
const PORT   = parseInt(process.env["MCP_PORT"]  ?? "3741", 10);
const TOKEN  = process.env["MCP_TOKEN"] ?? "";        // bearer token for HTTP mode

async function main(): Promise<void> {
  if (MODE === "http") {
    // One persistent transport instance for stateless HTTP mode
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    await server.connect(transport);

    const httpServer = createServer(async (req: IncomingMessage, res: ServerResponse) => {
      // Health check
      if (req.method === "GET" && req.url === "/health") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: "ok", server: "oracle-readiness-mcp" }));
        return;
      }

      // Bearer token auth
      if (TOKEN) {
        const auth = req.headers["authorization"] ?? "";
        if (auth !== `Bearer ${TOKEN}`) {
          res.writeHead(401, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Unauthorized" }));
          return;
        }
      }

      // Route all MCP traffic to /mcp
      if (req.url === "/mcp" || req.url === "/mcp/") {
        await transport.handleRequest(req, res);
        return;
      }

      res.writeHead(404);
      res.end("Not found");
    });

    httpServer.listen(PORT, () => {
      console.error(`oracle-readiness-mcp HTTP server listening on port ${PORT}`);
      console.error(`MCP endpoint: http://localhost:${PORT}/mcp`);
    });
  } else {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error("oracle-readiness-mcp running on stdio");
  }
}

main().catch(error => {
  console.error("Fatal:", error);
  process.exit(1);
});
