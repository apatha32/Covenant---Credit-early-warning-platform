export type Tier = "high" | "elevated" | "low";

export type Driver = { feature: string; contribution: number; text: string };

export type WatchlistRow = {
  cik: string;
  name: string;
  tickers: string[];
  sector: string;
  as_of_date: string;
  probability: number;
  risk_tier: Tier;
  top_driver: string | null;
  sparkline: number[];
  next_case_start: string | null;
  model_version_id: number;
};

export type Company = {
  cik: string;
  name: string;
  sec_name: string;
  tickers: string[];
  sector: string;
  sic_description: string | null;
  state: string | null;
  bankruptcy_cases: { case_start: string; case_end: string | null; accession: string }[];
};

export type ScorePoint = {
  as_of_date: string;
  probability: number;
  risk_tier: Tier;
  top_drivers: Driver[];
};

export type MetricPoint = {
  metric: string;
  period_type: "Q" | "FY";
  period_start: string | null;
  period_end: string;
  value: number;
  is_derived: boolean;
  source_tag: string;
  filed_date: string;
  accession: string;
  version: number;
  is_restated: boolean;
  original_value: number;
  original_accession: string;
  original_filed_date: string;
};

export type FilingEvent = {
  accession: string;
  event_type: string;
  form_type: string;
  filed_date: string;
  raw_items: string | null;
};

export type TextSignal = {
  accession: string;
  filed_date: string;
  section: string;
  signal_type: string;
  evidence_quote: string;
  confidence: number;
  model: string;
};

export type QaCitation = {
  quote: string;
  accession: string;
  form_type: string | null;
  filed_date: string;
  section: string;
  url: string;
};

export type QaAnswer = { answerable: boolean; answer: string; citations: QaCitation[]; cached?: boolean };

export type Role = "owner" | "admin" | "analyst" | "viewer";

export type Tenant = { id: string; name: string; is_demo: boolean; role: Role | null; created_at: string };

export type Portfolio = { id: string; tenant_id: string; name: string; borrowers: number };

export type Coverage = "queued" | "scored" | "no_data" | "excluded" | "failed" | "over_capacity";

export type Borrower = {
  id: string;
  tenant_id: string;
  portfolio_id: string;
  row_number: number | null;
  uploaded_name: string | null;
  uploaded_ticker: string | null;
  cik: string | null;
  sec_name: string | null;
  tickers: string[] | null;
  match_status: "pending" | "auto_matched" | "needs_review" | "confirmed" | "rejected" | "unmatched";
  match_confidence: number | null;
  match_method: string | null;
  coverage: Coverage | null;
  coverage_detail: string | null;
  as_of_date: string | null;
  probability: number | null;
  risk_tier: Tier | null;
};

export type Candidate = {
  cik: string;
  name: string;
  score: number;
  matched_name: string;
  kind: string;
  tickers?: string[];
  is_excluded?: boolean;
};

export type MatchReview = {
  id: string;
  tenant_id: string;
  portfolio_id: string;
  borrower_id: string;
  row_number: number | null;
  uploaded_name: string | null;
  uploaded_ticker: string | null;
  candidate_ciks: Candidate[];
};

export type Alert = {
  id: string;
  tenant_id: string;
  cik: string;
  name: string | null;
  triggered_at: string;
  as_of_date: string | null;
  reason: string;
  risk_tier: Tier | null;
  previous_tier: Tier | null;
  probability: number | null;
  status: "open" | "acknowledged" | "closed";
  decision: "agree" | "dismiss" | null;
  disposition_reason: string | null;
  decided_at: string | null;
  decided_by_me: boolean | null;
};

export type Member = { user_id: string; email: string; role: Role; created_at: string };
