-- Phase 1: index for raw files that are not per-company (ticker map, FRED series, bulk zips),
-- and a 'partial' run status for runs where some items failed but the rest landed.

create table raw.source_files (
  id           bigint generated always as identity primary key,
  source       text not null,                -- sec_tickers, fred, ...
  source_key   text not null,                -- e.g. 'company_tickers' or a FRED series id
  storage_path text not null,
  checksum     text not null,                -- sha256 of the raw payload
  fetched_at   timestamptz not null default now(),
  run_id       bigint not null references raw.ingest_runs (id),
  unique (source, source_key, checksum)
);
alter table raw.source_files enable row level security;
create index on raw.source_files (source, source_key, fetched_at desc);
create trigger source_files_append_only before update or delete on raw.source_files
  for each row execute function util.reject_mutation();

alter table raw.ingest_runs drop constraint ingest_runs_status_check;
alter table raw.ingest_runs add constraint ingest_runs_status_check
  check (status in ('running', 'succeeded', 'partial', 'failed'));
