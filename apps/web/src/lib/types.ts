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
