// Core data model for a normalised Oracle Fusion release feature
export interface Feature {
  id?: number;
  release: string;           // e.g. "26C"
  product_family: string;    // e.g. "HCM", "ERP", "SCM"
  product: string;           // e.g. "Payroll", "Human Resources"
  module: string;            // e.g. "Global Payroll", "Absence Management"
  feature_name: string;
  description: string;
  impact: string | null;     // "Large scale" | "Small scale" | "Report" | null
  enablement: string | null; // "Auto", "Opt In plus Setup", "Setup Required", "Potential Setup", "REST APIs", etc.
  auto_enabled_in: string | null; // e.g. "26D", "Does not expire"
  is_redwood: boolean;
  is_ai: boolean;
  ai_type: string | null;    // "Agent", "Generative", "Agentic App"
  setup_required: boolean;
  opt_in_required: boolean;
  source_url: string;
  retrieved_at: string;      // ISO timestamp
}

// A release entry from the catalogue
export interface Release {
  name: string;         // e.g. "26C"
  label: string;        // e.g. "Oracle Cloud Applications 26C"
  year: number;
  quarter: string;      // "A" | "B" | "C" | "D"
  catalogue_urls: string[];
}

// Comparison result between two releases for a module
export interface ReleaseComparison {
  module: string;
  old_release: string;
  new_release: string;
  added: Feature[];
  changed: Feature[];
  removed_names: string[];
  new_large_scale: Feature[];
  new_setup_required: Feature[];
  new_opt_in: Feature[];
  new_auto_enabled: Feature[];
}

// Summary stats for a module in a release
export interface FeatureSummary {
  release: string;
  product_family: string;
  module: string;
  total: number;
  large_scale: number;
  small_scale: number;
  setup_required: number;
  opt_in_required: number;
  auto_enabled: number;
  redwood: number;
  ai_features: number;
  features: Feature[];
}

// Rich per-feature detail sections scraped from the individual feature page
export interface FeatureDetail {
  id?: number;
  feature_id: number;           // FK ÔåÆ features.id
  release: string;              // denormalised for easy querying
  feature_name: string;         // denormalised for easy querying
  steps_to_enable: string | null;   // "Steps to Enable" section text
  business_benefit: string | null;  // "Business Benefit" section text
  key_resources: string | null;     // "Key Resources" links / text
  tips: string | null;              // "Tips and Considerations" section text
  source_url: string;
  retrieved_at: string;
}
