-- Phase 2: canonical model adjustments.
--
-- 1. curated.financial_metric gains mapping_version. The metric mapping (config) decides which
--    tag feeds each metric; the table is append-only, so a mapping change must produce a new,
--    separate set of rows instead of colliding with (or overwriting) the old ones.
-- 2. curated.filing_event records the form type (8-K vs 8-K/A vs NT 10-K).
-- 3. staging.quarantine de-duplicates identical records, so re-running a step is idempotent.
-- 4. curated.financial_metric_as_of(): the point-in-time read path for features.
-- 5. Append-only tables reject TRUNCATE too.

alter table curated.financial_metric add column mapping_version text not null;
-- The old unique constraints have auto-generated (truncated) names; drop them by lookup.
do $$
declare c record;
begin
  for c in select conname from pg_constraint
           where conrelid = 'curated.financial_metric'::regclass and contype = 'u'
  loop
    execute format('alter table curated.financial_metric drop constraint %I', c.conname);
  end loop;
end
$$;
alter table curated.financial_metric
  add constraint financial_metric_accession_key
    unique (mapping_version, cik, canonical_metric, period_type, period_end, accession),
  add constraint financial_metric_version_key
    unique (mapping_version, cik, canonical_metric, period_type, period_end, version);
drop index if exists curated.financial_metric_cik_canonical_metric_period_end_filed_date_idx;
create index financial_metric_pit_idx on curated.financial_metric
  (mapping_version, cik, canonical_metric, period_type, period_end, filed_date desc, version desc);

alter table curated.filing_event add column form_type text not null;

create unique index quarantine_dedupe_idx
  on staging.quarantine (source, rule_violated, md5(record::text));

-- Every metric value as it was known on p_as_of: for each period, the latest version filed on or
-- before that date. Rows filed later are invisible, which is the leakage guarantee features use.
create function curated.financial_metric_as_of(
  p_as_of date,
  p_mapping_version text,
  p_cik util.cik default null
) returns setof curated.financial_metric
language sql stable
set search_path = ''
as $$
  select distinct on (m.cik, m.canonical_metric, m.period_type, m.period_end) m.*
  from curated.financial_metric m
  where m.filed_date <= p_as_of
    and m.mapping_version = p_mapping_version
    and (p_cik is null or m.cik = p_cik)
  order by m.cik, m.canonical_metric, m.period_type, m.period_end, m.filed_date desc, m.version desc
$$;
revoke execute on function curated.financial_metric_as_of(date, text, util.cik) from public;
grant execute on function curated.financial_metric_as_of(date, text, util.cik) to service_role;

-- 5. Append-only tables also reject TRUNCATE (row triggers alone do not fire on it).
do $$
declare t text;
begin
  foreach t in array array[
    'raw.submissions_index', 'raw.companyfacts_index', 'raw.source_files',
    'curated.financial_metric', 'ml.prediction', 'app.disposition', 'app.audit_log'
  ] loop
    execute format(
      'create trigger %I before truncate on %s for each statement execute function util.reject_mutation()',
      replace(t, '.', '_') || '_no_truncate', t);
  end loop;
end
$$;
