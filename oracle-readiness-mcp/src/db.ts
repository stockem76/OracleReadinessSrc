import initSqlJs, { type Database, type SqlJsStatic } from "sql.js";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import type { Feature, FeatureDetail } from "./types.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DB_DIR = join(__dirname, "..", "data");
const DB_PATH = join(DB_DIR, "readiness.db");

let _db: Database | null = null;
let _SQL: SqlJsStatic | null = null;

async function getSql(): Promise<SqlJsStatic> {
  if (_SQL) return _SQL;
  _SQL = await initSqlJs();
  return _SQL;
}

export async function getDb(): Promise<Database> {
  if (_db) return _db;
  const SQL = await getSql();
  if (!existsSync(DB_DIR)) mkdirSync(DB_DIR, { recursive: true });
  if (existsSync(DB_PATH)) {
    const data = readFileSync(DB_PATH);
    _db = new SQL.Database(data);
  } else {
    _db = new SQL.Database();
  }
  initSchema(_db);
  return _db;
}

function saveDb(db: Database): void {
  const data = db.export();
  writeFileSync(DB_PATH, Buffer.from(data));
}

function initSchema(db: Database): void {
  db.run(`
    CREATE TABLE IF NOT EXISTS features (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      release          TEXT NOT NULL,
      product_family   TEXT NOT NULL,
      product          TEXT NOT NULL,
      module           TEXT NOT NULL,
      feature_name     TEXT NOT NULL,
      description      TEXT NOT NULL DEFAULT '',
      impact           TEXT,
      enablement       TEXT,
      auto_enabled_in  TEXT,
      is_redwood       INTEGER NOT NULL DEFAULT 0,
      is_ai            INTEGER NOT NULL DEFAULT 0,
      ai_type          TEXT,
      setup_required   INTEGER NOT NULL DEFAULT 0,
      opt_in_required  INTEGER NOT NULL DEFAULT 0,
      source_url       TEXT NOT NULL DEFAULT '',
      retrieved_at     TEXT NOT NULL,
      UNIQUE(release, product_family, module, feature_name, description)
    );
    CREATE INDEX IF NOT EXISTS idx_features_release  ON features(release);
    CREATE INDEX IF NOT EXISTS idx_features_module   ON features(module);
    CREATE INDEX IF NOT EXISTS idx_features_family   ON features(product_family);
    CREATE TABLE IF NOT EXISTS feature_details (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      feature_id       INTEGER NOT NULL,
      release          TEXT NOT NULL,
      feature_name     TEXT NOT NULL,
      steps_to_enable  TEXT,
      business_benefit TEXT,
      key_resources    TEXT,
      tips             TEXT,
      source_url       TEXT NOT NULL DEFAULT '',
      retrieved_at     TEXT NOT NULL,
      UNIQUE(feature_id)
    );
    CREATE INDEX IF NOT EXISTS idx_fdetails_release ON feature_details(release);
    CREATE TABLE IF NOT EXISTS crawl_log (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      source_url   TEXT NOT NULL,
      release      TEXT NOT NULL,
      crawled_at   TEXT NOT NULL,
      row_count    INTEGER NOT NULL DEFAULT 0
    );
  `);
  saveDb(db);
}

function rowToFeatureDetail(row: Record<string, unknown>): FeatureDetail {
  return row as unknown as FeatureDetail;
}

function rowToFeature(row: Record<string, unknown>): Feature {
  return {
    ...(row as unknown as Feature),
    is_redwood:     Boolean(row["is_redwood"]),
    is_ai:          Boolean(row["is_ai"]),
    setup_required: Boolean(row["setup_required"]),
    opt_in_required:Boolean(row["opt_in_required"]),
  };
}

// Execute a SELECT and map column names to objects
function queryAll(db: Database, sql: string, params: (string | number | null)[] = []): Record<string, unknown>[] {
  const stmt = db.prepare(sql);
  stmt.bind(params);
  const results: Record<string, unknown>[] = [];
  while (stmt.step()) {
    results.push(stmt.getAsObject() as Record<string, unknown>);
  }
  stmt.free();
  return results;
}

export async function upsertFeatures(features: Feature[]): Promise<number> {
  const db = await getDb();
  const sql = `
    INSERT OR REPLACE INTO features
      (release, product_family, product, module, feature_name, description,
       impact, enablement, auto_enabled_in, is_redwood, is_ai, ai_type,
       setup_required, opt_in_required, source_url, retrieved_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
  `;
  for (const r of features) {
    db.run(sql, [
      r.release, r.product_family, r.product, r.module, r.feature_name,
      r.description, r.impact ?? null, r.enablement ?? null, r.auto_enabled_in ?? null,
      r.is_redwood ? 1 : 0, r.is_ai ? 1 : 0, r.ai_type ?? null,
      r.setup_required ? 1 : 0, r.opt_in_required ? 1 : 0,
      r.source_url, r.retrieved_at,
    ] as (string | number | null)[]);
  }
  saveDb(db);
  return features.length;
}

export async function upsertFeatureDetail(detail: FeatureDetail): Promise<void> {
  const db = await getDb();
  db.run(`
    INSERT OR REPLACE INTO feature_details
      (feature_id, release, feature_name, steps_to_enable, business_benefit,
       key_resources, tips, source_url, retrieved_at)
    VALUES (?,?,?,?,?,?,?,?,?)
  `, [
    detail.feature_id, detail.release, detail.feature_name,
    detail.steps_to_enable ?? null, detail.business_benefit ?? null,
    detail.key_resources ?? null, detail.tips ?? null,
    detail.source_url, detail.retrieved_at,
  ] as (string | number | null)[]);
  saveDb(db);
}

export async function getFeatureDetail(featureId: number): Promise<FeatureDetail | null> {
  const db = await getDb();
  const rows = queryAll(db, `SELECT * FROM feature_details WHERE feature_id = ?`, [featureId]);
  return rows.length > 0 ? rowToFeatureDetail(rows[0]) : null;
}

export async function getFeatureDetailByName(release: string, featureName: string): Promise<FeatureDetail | null> {
  const db = await getDb();
  const rows = queryAll(db, `SELECT * FROM feature_details WHERE release = ? AND LOWER(feature_name) = LOWER(?)`, [release, featureName]);
  return rows.length > 0 ? rowToFeatureDetail(rows[0]) : null;
}

export async function getFeatureById(id: number): Promise<Feature | null> {
  const db = await getDb();
  const rows = queryAll(db, `SELECT * FROM features WHERE id = ?`, [id]);
  return rows.length > 0 ? rowToFeature(rows[0]) : null;
}

export async function logCrawl(url: string, release: string, count: number): Promise<void> {
  const db = await getDb();
  db.run(`INSERT INTO crawl_log (source_url, release, crawled_at, row_count) VALUES (?,?,?,?)`,
    [url, release, new Date().toISOString(), count]);
  saveDb(db);
}

export async function listReleases(): Promise<string[]> {
  const db = await getDb();
  return queryAll(db, `SELECT DISTINCT release FROM features ORDER BY release DESC`).map(r => r["release"] as string);
}

export async function listProductFamilies(release?: string): Promise<string[]> {
  const db = await getDb();
  if (release) {
    return queryAll(db, `SELECT DISTINCT product_family FROM features WHERE release=? ORDER BY product_family`, [release]).map(r => r["product_family"] as string);
  }
  return queryAll(db, `SELECT DISTINCT product_family FROM features ORDER BY product_family`).map(r => r["product_family"] as string);
}

export async function listModules(release: string, productFamily?: string): Promise<string[]> {
  const db = await getDb();
  if (productFamily) {
    return queryAll(db, `SELECT DISTINCT module FROM features WHERE release=? AND product_family=? ORDER BY module`, [release, productFamily]).map(r => r["module"] as string);
  }
  return queryAll(db, `SELECT DISTINCT module FROM features WHERE release=? ORDER BY module`, [release]).map(r => r["module"] as string);
}

export async function searchFeatures(query: string, release?: string, module?: string, productFamily?: string): Promise<Feature[]> {
  const db = await getDb();
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  const conditions: string[] = [];
  const params: (string | number | null)[] = [];

  if (release) { conditions.push("release = ?"); params.push(release); }
  if (module) { conditions.push("LOWER(module) LIKE ?"); params.push(`%${module.toLowerCase()}%`); }
  if (productFamily) { conditions.push("LOWER(product_family) LIKE ?"); params.push(`%${productFamily.toLowerCase()}%`); }

  for (const t of terms) {
    conditions.push(`(LOWER(feature_name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(module) LIKE ?)`);
    params.push(`%${t}%`, `%${t}%`, `%${t}%`);
  }

  const where = conditions.length ? `WHERE ${conditions.join(" AND ")}` : "";
  return queryAll(db, `SELECT * FROM features ${where} ORDER BY release DESC, module, feature_name LIMIT 100`, params).map(rowToFeature);
}

export async function getFeaturesByModule(release: string, module: string): Promise<Feature[]> {
  const db = await getDb();
  return queryAll(db, `SELECT * FROM features WHERE release=? AND LOWER(module) LIKE ? ORDER BY feature_name`, [release, `%${module.toLowerCase()}%`]).map(rowToFeature);
}

export async function getFilteredFeatures(release: string, filter: "setup_required" | "opt_in" | "large_scale" | "auto_enabled" | "ai", module?: string): Promise<Feature[]> {
  const db = await getDb();
  const cond: string[] = ["release = ?"];
  const params: (string | number | null)[] = [release];
  if (module) { cond.push("LOWER(module) LIKE ?"); params.push(`%${module.toLowerCase()}%`); }
  if (filter === "setup_required") cond.push("setup_required = 1");
  else if (filter === "opt_in") cond.push("opt_in_required = 1");
  else if (filter === "large_scale") cond.push("LOWER(impact) LIKE '%large%'");
  else if (filter === "auto_enabled") cond.push("auto_enabled_in IS NOT NULL");
  else if (filter === "ai") cond.push("is_ai = 1");
  return queryAll(db, `SELECT * FROM features WHERE ${cond.join(" AND ")} ORDER BY module, feature_name`, params).map(rowToFeature);
}

export async function getCrawlLog(): Promise<{source_url: string; release: string; crawled_at: string; row_count: number}[]> {
  const db = await getDb();
  return queryAll(db, `SELECT source_url, release, crawled_at, row_count FROM crawl_log ORDER BY crawled_at DESC LIMIT 50`) as {source_url: string; release: string; crawled_at: string; row_count: number}[];
}
