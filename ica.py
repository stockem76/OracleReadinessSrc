"""
ica.py
------
ICA (IBM Context Assistant) framer CSV export helpers.

Generates the five CSV files required by the ICA Context Studio Schema Builder
"Upload Sample Data" flow for the 26c Complete Ontology v1.0.x.

Each function returns a list of dicts with the canonical ICA column order:
  schemaVersion, sourceWorkbook, domain, entityType, name, status,
  moduleOrCategory, identifier, startDate, contextText

The caller is responsible for serialising to CSV (see csv_response()).
"""

from __future__ import annotations

import csv
import io
import re
import textwrap
from typing import Optional

# ---------------------------------------------------------------------------
# Column order ICA expects in Upload Sample Data CSV
# ---------------------------------------------------------------------------

ICA_CSV_COLUMNS = [
    "schemaVersion",
    "sourceWorkbook",
    "domain",
    "entityType",
    "name",
    "status",
    "moduleOrCategory",
    "identifier",
    "startDate",
    "contextText",
]

_SCHEMA_VERSION  = "1"
_SOURCE_WORKBOOK = "OracleReadinessMCP"
_DOMAIN          = "OracleFusion26C"
_STATUS_ACTIVE   = "active"

# Known modules already in the ICA enum (from schema doc analysis).
# Only modules that are ABSENT from this list need to be uploaded.
_KNOWN_MODULES = {
    "Global Payroll",
    "Absence Management",
    "Global Human Resources",
    "Talent Management",
    "Workforce Management",
    "Recruiting",
    "Learning",
    "Benefits",
    "Compensation",
    "Performance Management",
    "Succession Planning",
}

# Modules to add regardless (from the schema doc)
_EXTRA_MODULES = [
    ("Payroll Interface",   "MOD-PAYROLL-INTERFACE",   "Oracle Fusion Payroll Interface module for HCM"),
    ("Workforce Rewards",   "MOD-WORKFORCE-REWARDS",   "Oracle Fusion Workforce Rewards module"),
    ("HCM Extracts",        "MOD-HCM-EXTRACTS",        "Oracle Fusion HCM Extracts module"),
    ("Workforce Modeling",  "MOD-WORKFORCE-MODELING",  "Oracle Fusion Workforce Modeling module"),
    ("Career Development",  "MOD-CAREER-DEVELOPMENT",  "Oracle Fusion Career Development module"),
    ("Performance Management", "MOD-PERFORMANCE-MGMT", "Oracle Fusion Performance Management module"),
    ("Succession Planning", "MOD-SUCCESSION-PLANNING", "Oracle Fusion Succession Planning module"),
]

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _row(entity_type: str, name: str, module_or_category: str,
         identifier: str, start_date: str, context_text: str) -> dict:
    return {
        "schemaVersion":    _SCHEMA_VERSION,
        "sourceWorkbook":   _SOURCE_WORKBOOK,
        "domain":           _DOMAIN,
        "entityType":       entity_type,
        "name":             name,
        "status":           _STATUS_ACTIVE,
        "moduleOrCategory": module_or_category,
        "identifier":       identifier,
        "startDate":        start_date,
        "contextText":      context_text,
    }


def _slug(text: str) -> str:
    """Convert a feature name to a safe identifier fragment."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def csv_response(rows: list[dict]) -> str:
    """Serialise a list of row-dicts to CSV string with ICA_CSV_COLUMNS header."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ICA_CSV_COLUMNS, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Release codes  (extend enum:oracleFusion26cReleaseCode)
# ---------------------------------------------------------------------------

def build_releases_csv() -> list[dict]:
    """26D, 27A, 27B — future-proof release codes."""
    entries = [
        ("26D", "REL-26D", "2025-10-01T00:00:00Z", "Oracle Fusion 26D quarterly release"),
        ("27A", "REL-27A", "2026-02-01T00:00:00Z", "Oracle Fusion 27A quarterly release"),
        ("27B", "REL-27B", "2026-06-01T00:00:00Z", "Oracle Fusion 27B quarterly release"),
    ]
    return [
        _row("Release", name, "Release", ident, start, ctx)
        for name, ident, start, ctx in entries
    ]


# ---------------------------------------------------------------------------
# 2. ActionType enum extension
# ---------------------------------------------------------------------------

def build_action_types_csv() -> list[dict]:
    """Business Benefit and Key Resources — two new actionType enum values."""
    entries = [
        (
            "Business Benefit",
            "ACT-BUSINESS-BENEFIT",
            "2025-01-01T00:00:00Z",
            "Business benefit section describing the value delivered by enabling this feature",
        ),
        (
            "Key Resources",
            "ACT-KEY-RESOURCES",
            "2025-01-01T00:00:00Z",
            "Key resources links and documentation references for this feature",
        ),
    ]
    return [
        _row("Action", name, "ActionType", ident, start, ctx)
        for name, ident, start, ctx in entries
    ]


# ---------------------------------------------------------------------------
# 3. Module enum extension (from live MCP data + hardcoded extras)
# ---------------------------------------------------------------------------

def build_modules_csv(live_modules: Optional[list[str]] = None) -> list[dict]:
    """Modules present in live MCP data that are absent from the known enum.

    Args:
        live_modules: list of module names from the live DB (list_modules result).
                      If provided, any module not in _KNOWN_MODULES is included.
                      The hardcoded _EXTRA_MODULES are always included.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    # Always emit the schema-doc extras
    for name, ident, ctx in _EXTRA_MODULES:
        if name not in seen:
            rows.append(_row("Module", name, "Module", ident, "2025-01-01T00:00:00Z", ctx))
            seen.add(name)

    # Add any live modules not already in the known enum
    if live_modules:
        for mod in live_modules:
            if mod and mod not in _KNOWN_MODULES and mod not in seen:
                ident = f"MOD-{_slug(mod).upper().replace('-', '-')}"
                ctx = f"Oracle Fusion {mod} module (discovered from live MCP data)"
                rows.append(_row("Module", mod, "Module", ident, "2025-01-01T00:00:00Z", ctx))
                seen.add(mod)

    return rows


# ---------------------------------------------------------------------------
# 4. Derivation method (add M017_MCP_FRAMER_INGESTION)
# ---------------------------------------------------------------------------

def build_derivation_methods_csv() -> list[dict]:
    """New derivation method for nodes ingested via the Homebrew Readiness framer."""
    return [
        _row(
            "DerivationMethod",
            "M017_MCP_FRAMER_INGESTION",
            "Ingestion",
            "DM-017",
            "2025-01-01T00:00:00Z",
            (
                "Nodes and edges ingested via the Homebrew Readiness MCP framer connector "
                "from the Oracle Cloud Readiness scraper service. "
                "Distinct from original corpus crawler ingestion."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# 5. Feature flags  (the main payload — drives Feature node creation in ICA)
# ---------------------------------------------------------------------------

def build_features_csv(features: list[dict]) -> list[dict]:
    """Generate ICA Feature rows from live DB feature records.

    Packs the five new boolean/string properties (isAiFeature, isRedwood,
    autoEnabledIn, optInRequired, aiType) into contextText so ICA's vector
    embedding captures them even before the schema properties are added.

    Args:
        features: list of feature dicts from ReadinessDB (get_filtered_features /
                  filter_entries result).  Must include at minimum:
                    feature_name, module, product_family, release, description,
                    impact, is_ai, is_redwood, auto_enabled_in, opt_in_required,
                    ai_type, setup_required, enablement.
    """
    rows: list[dict] = []
    for f in features:
        name     = f.get("feature_name") or ""
        module   = f.get("module") or f.get("product_family") or "Unknown"
        release  = f.get("release") or "Unknown"
        desc     = (f.get("description") or "")[:300]
        impact   = f.get("impact") or ""
        is_ai    = bool(f.get("is_ai"))
        is_rw    = bool(f.get("is_redwood"))
        auto_in  = f.get("auto_enabled_in") or ""
        opt_in   = bool(f.get("opt_in_required"))
        ai_type  = f.get("ai_type") or ""
        setup    = bool(f.get("setup_required"))
        enable   = f.get("enablement") or ""

        flags: list[str] = []
        if is_ai:    flags.append(f"Is AI: true. AI type: {ai_type or 'unspecified'}.")
        if is_rw:    flags.append("Is Redwood: true.")
        if auto_in:  flags.append(f"Auto-enabled in: {auto_in}.")
        if opt_in:   flags.append("Opt-in required: true.")
        if setup:    flags.append("Setup required: true.")
        if impact:   flags.append(f"Impact: {impact}.")
        if enable:   flags.append(f"Enablement: {enable}.")

        ctx = f"{name}. Module: {module}. Release: {release}."
        if desc:
            ctx += f" {desc}"
        if flags:
            ctx += " " + " ".join(flags)

        ident = f"F-{_slug(name)}"
        rows.append(_row(
            "Feature",
            name,
            module,
            ident,
            "2025-06-01T00:00:00Z",
            ctx.strip(),
        ))

    return rows


# ---------------------------------------------------------------------------
# 6. Action rows  (Steps to Enable / Business Benefit / Key Resources / Tips)
# ---------------------------------------------------------------------------

def build_actions_csv(details: list[dict]) -> list[dict]:
    """Generate ICA Action rows from feature detail records.

    One Action row is emitted per non-null detail section per feature.
    actionType is mapped as:
      steps_to_enable    → Steps to Enable
      business_benefit   → Business Benefit
      key_resources      → Key Resources
      tips_considerations / tips → Tips and Considerations

    Args:
        details: list of feature_detail dicts from ReadinessDB.  Must include:
            feature_name, release, module, product_family and the section fields.
    """
    SECTION_MAP = [
        ("steps_to_enable",    "Steps to Enable"),
        ("business_benefit",   "Business Benefit"),
        ("key_resources",      "Key Resources"),
        ("tips_considerations","Tips and Considerations"),
        ("tips",               "Tips and Considerations"),  # TypeScript MCP field name
    ]

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (feature_name, action_type) dedup

    for d in details:
        fname   = d.get("feature_name") or ""
        module  = d.get("module") or d.get("product_family") or "Unknown"
        release = d.get("release") or "Unknown"

        for field, action_type in SECTION_MAP:
            content = d.get(field) or ""
            if not content:
                continue
            key = (fname, action_type)
            if key in seen:
                continue
            seen.add(key)

            # Truncate to keep contextText manageable (ICA limit is typically 2 KB)
            snippet = textwrap.shorten(content, width=400, placeholder="…")
            ctx = (
                f"Feature: {fname}. Module: {module}. Release: {release}. "
                f"Action type: {action_type}. {snippet}"
            )
            ident = f"ACT-{_slug(fname)}-{_slug(action_type)}"
            rows.append(_row(
                "Action",
                f"{action_type}: {fname}"[:200],
                module,
                ident,
                "2025-06-01T00:00:00Z",
                ctx.strip(),
            ))

    return rows


# ---------------------------------------------------------------------------
# Schema changes summary (machine-readable, for /api/ica/schema-changes.json)
# ---------------------------------------------------------------------------

SCHEMA_CHANGES = {
    "ontology": "26c Complete Ontology",
    "version":  "v1.0.437",
    "changes": [
        {
            "target":  "custom:feature",
            "type":    "ADD_PROPS",
            "status":  "data_ready",
            "props": [
                {"name": "Is AI Feature",   "curie": "custom:isAiFeature",    "datatype": "boolean", "required": False},
                {"name": "AI Type",         "curie": "custom:aiType",         "datatype": "string",  "required": False},
                {"name": "Is Redwood",      "curie": "custom:isRedwood",      "datatype": "boolean", "required": False},
                {"name": "Auto Enabled In", "curie": "custom:autoEnabledIn",  "datatype": "string",  "required": False},
                {"name": "Opt In Required", "curie": "custom:optInRequired",  "datatype": "boolean", "required": False},
            ],
            "note": "Add manually in Schema Builder → Feature node → Properties panel. Not uploadable via CSV.",
        },
        {
            "target":  "enum:oracleFusion26cReleaseCode",
            "type":    "EXTEND_ENUM",
            "status":  "csv_ready",
            "csv_endpoint": "/api/ica/releases.csv",
        },
        {
            "target":  "enum:oracleFusion26cActionType",
            "type":    "EXTEND_ENUM",
            "status":  "csv_ready",
            "csv_endpoint": "/api/ica/action-types.csv",
        },
        {
            "target":  "enum:oracleFusion26cModule",
            "type":    "EXTEND_ENUM",
            "status":  "csv_ready",
            "csv_endpoint": "/api/ica/modules.csv",
        },
        {
            "target":  "enum:oracleFusion26cMethod",
            "type":    "ADD",
            "status":  "csv_ready",
            "csv_endpoint": "/api/ica/derivation-methods.csv",
        },
        {
            "target":  "custom:feature (Feature nodes)",
            "type":    "BULK_UPLOAD",
            "status":  "csv_ready",
            "csv_endpoint": "/api/ica/features.csv",
            "note":    "Generated from live feature_details table. Use Upload Sample Data in Schema Builder.",
        },
        {
            "target":  "custom:action (Action nodes)",
            "type":    "BULK_UPLOAD",
            "status":  "csv_ready",
            "csv_endpoint": "/api/ica/actions.csv",
            "note":    "Generated from scraped feature detail sections (steps, benefit, resources, tips).",
        },
        {
            "target":  "oracleFusionGraphEntity.featureCode",
            "type":    "SET_OPTIONAL",
            "status":  "manual_ui_action",
            "note":    "Change required:true → required:false on abstract parent to prevent validation failures on unknown codes.",
        },
    ],
}
