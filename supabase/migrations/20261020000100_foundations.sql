-- Foundations: extensions, schemas, shared domains and helper functions, base grants.
--
-- Access model:
--   raw, staging, curated, ml  -> service_role only (Python services). No anon/authenticated access.
--   app                        -> authenticated users, row-scoped by tenant via RLS.
--   util                       -> shared types and trigger functions.
-- RLS is enabled on every table, even service-only ones, so an accidental grant exposes nothing.

create extension if not exists vector with schema extensions;

create schema if not exists util;
create schema if not exists raw;
create schema if not exists staging;
create schema if not exists curated;
create schema if not exists ml;
create schema if not exists app;

revoke all on schema util, raw, staging, curated, ml, app from public, anon, authenticated;
grant usage on schema util, raw, staging, curated, ml, app to service_role;
grant usage on schema util, app to authenticated;

-- Tables created later in these schemas are usable by service_role by default.
alter default privileges in schema raw, staging, curated, ml, app
  grant all on tables to service_role;
alter default privileges in schema raw, staging, curated, ml, app
  grant all on sequences to service_role;
-- Note: Postgres grants EXECUTE on new functions to PUBLIC and that cannot be revoked
-- per schema, so every function below revokes it explicitly.

-- SEC identifiers, stored in their canonical text form.
-- CIK: 10 digits, zero-padded (strip padding only when building Archives paths).
create domain util.cik as text
  check (value ~ '^[0-9]{10}$');

-- Accession number with dashes, e.g. 0000320193-23-000106.
create domain util.accession as text
  check (value ~ '^[0-9]{10}-[0-9]{2}-[0-9]{6}$');

-- Guard for tables that hold history: rows are appended (with versioning), never changed.
create function util.reject_mutation() returns trigger
language plpgsql
as $$
begin
  raise exception 'table %.% is append-only (% rejected)', tg_table_schema, tg_table_name, tg_op
    using errcode = 'insufficient_privilege';
end
$$;

revoke execute on function util.reject_mutation() from public;
