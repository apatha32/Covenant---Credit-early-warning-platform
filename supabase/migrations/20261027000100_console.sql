-- Phase 6a: read-only analyst console, served straight from Supabase (PostgREST + RLS).
--
-- 1. Tenants see curated / ml rows only for companies in their own portfolios (row policies on
--    the shared tables; raw and staging stay closed).
-- 2. Bankruptcy cases (Phase 4 labels) are stored in curated.bankruptcy_case for display.
-- 3. One tenant may be the public demo. Supabase anonymous sign-ins (JWT claim is_anonymous)
--    read that tenant and nothing else. They hold no membership, so every role-gated write
--    policy fails for them; restrictive policies block their writes again on every app table.
-- 4. Schema `api`: security_invoker views the web app reads. RLS of the underlying tables
--    applies to the caller, so a view can never show more than its tables allow.

-- ---------------------------------------------------------------------------
-- 1. Sector (SEC SIC division). Mirrors tripwire_common.sectors.
-- ---------------------------------------------------------------------------

create function util.sector_of(p_sic integer) returns text
language sql immutable set search_path = ''
as $$
  select case
    when p_sic between 100 and 999 then 'Agriculture'
    when p_sic between 1000 and 1499 then 'Mining & energy'
    when p_sic between 1500 and 1799 then 'Construction'
    when p_sic between 2000 and 3999 then 'Manufacturing'
    when p_sic between 4000 and 4999 then 'Transportation & utilities'
    when p_sic between 5000 and 5199 then 'Wholesale'
    when p_sic between 5200 and 5999 then 'Retail'
    when p_sic between 6000 and 6799 then 'Finance'
    when p_sic between 7000 and 8999 then 'Services'
    else 'Other'
  end
$$;
revoke execute on function util.sector_of(integer) from public;
grant execute on function util.sector_of(integer) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 2. Bankruptcy cases (written by the ML labels step).
-- ---------------------------------------------------------------------------

create table curated.bankruptcy_case (
  cik           util.cik not null references curated.company (cik),
  case_start    date not null,
  case_end      date,
  accession     util.accession not null,  -- the 8-K that started the case
  label_version text not null,            -- labels.yaml text_check version
  created_at    timestamptz not null default now(),
  check (case_end is null or case_end >= case_start),
  primary key (cik, case_start)
);
alter table curated.bankruptcy_case enable row level security;

-- ---------------------------------------------------------------------------
-- 3. Demo tenant + anonymous visitors.
-- ---------------------------------------------------------------------------

alter table app.tenant add column is_demo boolean not null default false;
create unique index tenant_one_demo on app.tenant (is_demo) where is_demo;

create function app.is_anonymous() returns boolean
language sql stable set search_path = ''
as $$
  select coalesce(((select auth.jwt()) ->> 'is_anonymous')::boolean, false)
$$;
revoke execute on function app.is_anonymous() from public;
grant execute on function app.is_anonymous() to authenticated, service_role;

-- Members' tenants, plus the demo tenant for anonymous visitors.
create or replace function app.user_tenant_ids() returns setof uuid
language sql stable security definer set search_path = ''
as $$
  select m.tenant_id from app.membership m where m.user_id = (select auth.uid())
  union
  select t.id from app.tenant t where t.is_demo and app.is_anonymous()
$$;

-- Defense in depth: anonymous sessions never write to app tables, whatever other policy says.
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'app' loop
    execute format(
      'create policy %I on app.%I as restrictive for insert to authenticated
         with check (not app.is_anonymous())', t || '_anon_no_insert', t);
    execute format(
      'create policy %I on app.%I as restrictive for update to authenticated
         using (not app.is_anonymous()) with check (not app.is_anonymous())',
      t || '_anon_no_update', t);
    execute format(
      'create policy %I on app.%I as restrictive for delete to authenticated
         using (not app.is_anonymous())', t || '_anon_no_delete', t);
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 1. Shared data, scoped to the caller's portfolio companies.
-- ---------------------------------------------------------------------------

create function app.user_ciks() returns setof util.cik
language sql stable security definer set search_path = ''
as $$
  select distinct b.cik from app.portfolio_borrower b
  where b.tenant_id in (select app.user_tenant_ids())
    and b.cik is not null
    and b.match_status in ('auto_matched', 'confirmed')
$$;
revoke execute on function app.user_ciks() from public;
grant execute on function app.user_ciks() to authenticated, service_role;

grant usage on schema curated, ml to authenticated;
grant select on curated.company, curated.financial_metric, curated.filing_event,
  curated.bankruptcy_case, ml.prediction, ml.model_version to authenticated;

create policy company_select on curated.company for select to authenticated
  using (cik in (select app.user_ciks()));
create policy financial_metric_select on curated.financial_metric for select to authenticated
  using (cik in (select app.user_ciks()));
create policy filing_event_select on curated.filing_event for select to authenticated
  using (cik in (select app.user_ciks()));
create policy bankruptcy_case_select on curated.bankruptcy_case for select to authenticated
  using (cik in (select app.user_ciks()));
create policy prediction_select on ml.prediction for select to authenticated
  using (cik in (select app.user_ciks()));
-- Model metadata holds no tenant data.
create policy model_version_select on ml.model_version for select to authenticated
  using (true);

-- ---------------------------------------------------------------------------
-- 4. api: what the web app reads (exposed through PostgREST).
-- ---------------------------------------------------------------------------

create schema api;
revoke all on schema api from public, anon;
grant usage on schema api to authenticated, service_role;

-- Predictions of the active model, one row per portfolio company and as-of date, with the
-- last 8 scores for a sparkline and (hindsight, never a model input) the next bankruptcy.
create view api.watchlist with (security_invoker = true) as
select
  b.portfolio_id,
  b.tenant_id,
  b.cik,
  coalesce(b.uploaded_name, c.name) as name,
  c.tickers,
  util.sector_of(c.sic) as sector,
  p.as_of_date,
  p.probability,
  p.risk_tier,
  p.top_drivers -> 0 ->> 'text' as top_driver,
  (select array_agg(h.probability order by h.as_of_date)
     from (select q.as_of_date, q.probability from ml.prediction q
           where q.cik = p.cik and q.model_version_id = p.model_version_id
             and q.as_of_date <= p.as_of_date
           order by q.as_of_date desc limit 8) h) as sparkline,
  (select min(k.case_start) from curated.bankruptcy_case k
    where k.cik = p.cik and k.case_start > p.as_of_date) as next_case_start,
  p.model_version_id
from app.portfolio_borrower b
join curated.company c on c.cik = b.cik
join ml.prediction p on p.cik = b.cik
join ml.model_version v on v.id = p.model_version_id and v.is_active;

create view api.as_of_dates with (security_invoker = true) as
select p.as_of_date, count(*) as companies
from ml.prediction p
join ml.model_version v on v.id = p.model_version_id and v.is_active
group by p.as_of_date;

create view api.company with (security_invoker = true) as
select
  c.cik,
  coalesce(b.uploaded_name, c.name) as name,
  c.name as sec_name,
  c.tickers,
  util.sector_of(c.sic) as sector,
  c.sic_description,
  c.state,
  coalesce((select jsonb_agg(jsonb_build_object('case_start', k.case_start,
                                                'case_end', k.case_end,
                                                'accession', k.accession)
                             order by k.case_start)
            from curated.bankruptcy_case k where k.cik = c.cik), '[]') as bankruptcy_cases
from curated.company c
left join lateral (
  select x.uploaded_name from app.portfolio_borrower x
  where x.cik = c.cik order by x.created_at limit 1
) b on true;

create view api.score_history with (security_invoker = true) as
select p.cik, p.as_of_date, p.probability, p.risk_tier, p.top_drivers, p.model_version_id
from ml.prediction p
join ml.model_version v on v.id = p.model_version_id and v.is_active;

-- Every reported period of the newest metric mapping, as known today (latest version), with
-- the originally reported value (and when it was filed) and whether it was ever restated.
create view api.metric_history with (security_invoker = true) as
with current_mapping as (
  select max(mapping_version::integer)::text as mapping_version from curated.financial_metric
)
select distinct on (m.cik, m.canonical_metric, m.period_type, m.period_end)
  m.cik,
  m.canonical_metric as metric,
  m.period_type,
  m.period_start,
  m.period_end,
  m.value,
  m.unit,
  m.is_derived,
  m.source_tag,
  m.filed_date,
  m.accession,
  m.version,
  m.version > 1 as is_restated,
  first_value(m.value) over w as original_value,
  first_value(m.accession) over w as original_accession,
  first_value(m.filed_date) over w as original_filed_date
from curated.financial_metric m
join current_mapping cm on cm.mapping_version = m.mapping_version
window w as (partition by m.cik, m.canonical_metric, m.period_type, m.period_end
             order by m.version)
order by m.cik, m.canonical_metric, m.period_type, m.period_end, m.version desc;

create view api.filing_events with (security_invoker = true) as
select e.cik, e.accession, e.event_type, e.form_type, e.filed_date, e.raw_items
from curated.filing_event e;

create view api.model_versions with (security_invoker = true) as
select id, algo, feature_set_version, lower(train_window) as train_start,
       upper(train_window) as train_end, metrics, is_active, created_at
from ml.model_version;

grant select on all tables in schema api to authenticated, service_role;
