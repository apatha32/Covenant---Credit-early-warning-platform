-- curated: canonical, point-in-time domain model.
--
-- Point-in-time rule: every row that a feature can read carries `filed_date`, the date the
-- information became public. A feature for as-of date T may only use rows with filed_date <= T.

create table curated.company (
  cik              util.cik primary key,
  name             text not null,
  tickers          text[] not null default '{}',
  sic              integer,
  sic_description  text,
  state            text,
  fiscal_year_end  text check (fiscal_year_end ~ '^(0[1-9]|1[0-2])[0-3][0-9]$'),  -- MMDD
  is_excluded      boolean not null default false,
  exclusion_reason text,
  updated_at       timestamptz not null default now(),
  check (is_excluded = (exclusion_reason is not null))
);
alter table curated.company enable row level security;
create index on curated.company using gin (tickers);

create table curated.company_alias (
  id               bigint generated always as identity primary key,
  cik              util.cik not null references curated.company (cik),
  alias            text not null,
  alias_normalized text not null,            -- lowercased, legal suffixes and punctuation stripped
  alias_type       text not null check (alias_type in ('former_name', 'ticker', 'tenant_provided')),
  valid_from       date,                     -- from submissions formerNames, when known
  valid_to         date,
  created_at       timestamptz not null default now(),
  unique (cik, alias_type, alias)
);
alter table curated.company_alias enable row level security;
create index on curated.company_alias (alias_normalized);

-- One row per (company, metric, period) per filing that reported it. A restatement is a new
-- row with a higher version; nothing is overwritten. Derived values (e.g. Q4 = FY - 9M YTD)
-- record their inputs in `derivation`, and take filed_date/accession from the latest input,
-- because that is when the derived value first became knowable.
create table curated.financial_metric (
  id               bigint generated always as identity primary key,
  cik              util.cik not null references curated.company (cik),
  canonical_metric text not null,
  period_type      text not null check (period_type in ('Q', 'FY')),
  period_start     date,                     -- null for instant (balance sheet) metrics
  period_end       date not null,
  unit             text not null,
  value            numeric not null,
  source_tag       text not null,            -- taxonomy:tag chosen by metric_mapping.yaml
  is_derived       boolean not null default false,
  derivation       jsonb,                    -- [{accession, tag, period_start, period_end, value}, ...]
  filed_date       date not null,
  accession        util.accession not null,
  version          integer not null check (version >= 1),
  created_at       timestamptz not null default now(),
  check (period_start is null or period_start <= period_end),
  check (is_derived = (derivation is not null)),
  unique (cik, canonical_metric, period_type, period_end, accession),
  unique (cik, canonical_metric, period_type, period_end, version)
);
alter table curated.financial_metric enable row level security;
create index on curated.financial_metric (cik, canonical_metric, period_end, filed_date);
create trigger financial_metric_append_only before update or delete on curated.financial_metric
  for each row execute function util.reject_mutation();

-- Events derived from 8-K item codes and NT 10-K / NT 10-Q forms (event_codes.yaml).
create table curated.filing_event (
  cik        util.cik not null references curated.company (cik),
  accession  util.accession not null,
  event_type text not null,
  filed_date date not null,
  raw_items  text,                           -- original items string, e.g. '1.03,9.01'
  created_at timestamptz not null default now(),
  primary key (cik, accession, event_type)
);
alter table curated.filing_event enable row level security;
create index on curated.filing_event (event_type, filed_date);
create index on curated.filing_event (cik, filed_date);

-- LLM-extracted signals. Every row must quote its evidence verbatim from the source section.
create table curated.text_signal (
  id             bigint generated always as identity primary key,
  cik            util.cik not null references curated.company (cik),
  accession      util.accession not null,
  filed_date     date not null,
  section        text not null,
  signal_type    text not null,
  value          numeric not null,
  evidence_quote text not null check (length(evidence_quote) > 0),
  confidence     numeric not null check (confidence between 0 and 1),
  prompt_version text not null,
  model          text not null,
  created_at     timestamptz not null default now(),
  unique (cik, accession, section, signal_type, prompt_version, model)
);
alter table curated.text_signal enable row level security;
create index on curated.text_signal (cik, signal_type, filed_date);

-- RAG chunks. Embedding size must match config/models.yaml embedding.dimensions.
create table curated.filing_chunk (
  id          bigint generated always as identity primary key,
  cik         util.cik not null references curated.company (cik),
  accession   util.accession not null,
  filed_date  date not null,
  section     text not null,
  chunk_index integer not null check (chunk_index >= 0),
  chunk_text  text not null,
  embedding   extensions.vector(1024),
  created_at  timestamptz not null default now(),
  unique (cik, accession, section, chunk_index)
);
alter table curated.filing_chunk enable row level security;
create index on curated.filing_chunk (cik, filed_date);
create index on curated.filing_chunk using hnsw (embedding extensions.vector_cosine_ops);

-- Macro observations, bitemporal: an observation date can be revised in later releases.
create table curated.macro_series (
  series_id        text not null,
  observation_date date not null,
  release_date     date not null,
  value            numeric not null,
  created_at       timestamptz not null default now(),
  check (release_date >= observation_date),
  primary key (series_id, observation_date, release_date)
);
alter table curated.macro_series enable row level security;
