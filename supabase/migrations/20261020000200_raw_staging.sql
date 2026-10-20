-- raw: append-only landing records. Payloads live as Parquet in Supabase Storage;
-- these tables only index them.
-- staging: typed and cleaned rows, not yet canonical. Rebuildable from raw.

create table raw.ingest_runs (
  id          bigint generated always as identity primary key,
  source      text not null,                 -- e.g. sec_tickers, sec_submissions, sec_companyfacts, fred
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  status      text not null default 'running' check (status in ('running', 'succeeded', 'failed')),
  counts      jsonb not null default '{}'::jsonb,  -- row counts in/out per step
  error       text,
  check (finished_at is null or finished_at >= started_at),
  check (status = 'running' or finished_at is not null)
);
alter table raw.ingest_runs enable row level security;

create table raw.submissions_index (
  id           bigint generated always as identity primary key,
  cik          util.cik not null,
  storage_path text not null,
  checksum     text not null,                -- sha256 of the raw payload
  fetched_at   timestamptz not null default now(),
  run_id       bigint not null references raw.ingest_runs (id),
  unique (cik, checksum)                     -- identical re-fetch is a no-op: idempotent ingest
);
alter table raw.submissions_index enable row level security;
create index on raw.submissions_index (cik, fetched_at desc);
create trigger submissions_index_append_only before update or delete on raw.submissions_index
  for each row execute function util.reject_mutation();

create table raw.companyfacts_index (
  id           bigint generated always as identity primary key,
  cik          util.cik not null,
  storage_path text not null,
  checksum     text not null,
  fetched_at   timestamptz not null default now(),
  run_id       bigint not null references raw.ingest_runs (id),
  unique (cik, checksum)
);
alter table raw.companyfacts_index enable row level security;
create index on raw.companyfacts_index (cik, fetched_at desc);
create trigger companyfacts_index_append_only before update or delete on raw.companyfacts_index
  for each row execute function util.reject_mutation();


-- One row per XBRL fact as reported in one filing. The same (tag, period) appears again in
-- later filings (comparatives, restatements); all of them are kept.
create table staging.facts (
  id           bigint generated always as identity primary key,
  cik          util.cik not null,
  taxonomy     text not null,                -- us-gaap, dei, srt, ifrs-full
  tag          text not null,
  unit         text not null,                -- USD, shares, pure, USD/shares, ...
  value        numeric not null,
  period_start date,                         -- null for instant (balance sheet) facts
  period_end   date not null,
  fy           integer,
  fp           text,                         -- FY, Q1, Q2, Q3
  form         text not null,
  accession    util.accession not null,
  filed_date   date not null,
  frame        text,
  run_id       bigint not null references raw.ingest_runs (id),
  check (period_start is null or period_start <= period_end),
  unique nulls not distinct (cik, taxonomy, tag, unit, period_start, period_end, accession)
);
alter table staging.facts enable row level security;
create index on staging.facts (cik, tag, period_end);
create index on staging.facts (accession);

-- An accession can list several co-registrant CIKs (e.g. parent + subsidiaries), so the key
-- is (cik, accession), not accession alone.
create table staging.filings (
  cik              util.cik not null,
  accession        util.accession not null,
  form_type        text not null,
  filed_date       date not null,
  accepted_at      timestamptz,              -- EDGAR acceptance time, when available
  report_date      date,
  primary_document text,
  items            text[] not null default '{}',  -- 8-K item codes, e.g. {1.03,9.01}
  run_id           bigint not null references raw.ingest_runs (id),
  primary key (cik, accession)
);
alter table staging.filings enable row level security;
create index on staging.filings (form_type, filed_date);
create index on staging.filings using gin (items);

-- Records that failed a data-quality rule. Never drop silently; land here instead.
create table staging.quarantine (
  id            bigint generated always as identity primary key,
  source        text not null,
  record        jsonb not null,
  rule_violated text not null,
  run_id        bigint references raw.ingest_runs (id),
  created_at    timestamptz not null default now()
);
alter table staging.quarantine enable row level security;
create index on staging.quarantine (rule_violated, created_at desc);
create index on staging.quarantine (run_id);

-- Cache of every LLM call result, keyed as the spec requires. Re-running a prompt version
-- against a model never re-bills a section already processed.
create table staging.llm_cache (
  id             bigint generated always as identity primary key,
  cik            util.cik not null,
  accession      util.accession not null,
  section        text not null,
  prompt_version text not null,
  model          text not null,
  response       jsonb not null,
  input_tokens   integer,
  output_tokens  integer,
  created_at     timestamptz not null default now(),
  unique (accession, section, prompt_version, model)
);
alter table staging.llm_cache enable row level security;
