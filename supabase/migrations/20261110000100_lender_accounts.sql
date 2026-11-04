-- Phase 6b: lender workspaces, borrower upload + match review, served companies, alerts.
--
-- 1. Writes (create workspace, upload, match decisions, alert dispositions) go through the API
--    (service_role) after it verifies the user's JWT and membership; reads stay on api views.
-- 2. ml.served_company: the companies the scheduled jobs refresh and score. A newly matched
--    borrower is queued here, up to ml.serving_limits.max_companies (the free database tier
--    holds about 4 MB of curated data per company).
-- 3. app.generate_alerts(): one alert per tenant when a borrower's risk tier rises, plus one
--    when a borrower is high risk at the time it is added. Idempotent per prediction.

-- ---------------------------------------------------------------------------
-- 2. Served companies.
-- ---------------------------------------------------------------------------

create table ml.serving_limits (
  id            boolean primary key default true check (id),
  max_companies integer not null check (max_companies >= 0)
);
alter table ml.serving_limits enable row level security;
insert into ml.serving_limits (max_companies) values (70);

create table ml.served_company (
  cik            util.cik primary key references curated.company (cik),
  requested_at   timestamptz not null default now(),
  status         text not null default 'queued'
    check (status in ('queued', 'scored', 'no_data', 'excluded', 'failed')),
  detail         text,
  last_scored_at timestamptz
);
alter table ml.served_company enable row level security;
create index on ml.served_company (status);

grant select on ml.served_company to authenticated;
create policy served_company_select on ml.served_company for select to authenticated
  using (cik in (select app.user_ciks()));

-- Queue companies for serving while there is room; returns the ones left out (over capacity).
-- Companies in sectors the model does not cover are recorded as excluded and take no room.
create function ml.request_serving(p_ciks text[]) returns setof text
language plpgsql volatile set search_path = ''
as $$
declare
  cap integer := (select l.max_companies from ml.serving_limits l);
  c record;
begin
  perform pg_advisory_xact_lock(hashtext('ml.served_company capacity'));
  for c in
    select k.cik, k.is_excluded, k.exclusion_reason from curated.company k
    where k.cik = any(p_ciks)
      and not exists (select 1 from ml.served_company s where s.cik = k.cik)
    order by k.cik
  loop
    if c.is_excluded then
      insert into ml.served_company (cik, status, detail)
      values (c.cik, 'excluded', coalesce(c.exclusion_reason, 'sector the model does not cover'));
    elsif (select count(*) from ml.served_company where status <> 'excluded') >= cap then
      return next c.cik;
    else
      insert into ml.served_company (cik) values (c.cik);
    end if;
  end loop;
end
$$;
revoke execute on function ml.request_serving(text[]) from public;
grant execute on function ml.request_serving(text[]) to service_role;

-- ---------------------------------------------------------------------------
-- 3. Alerts.
-- ---------------------------------------------------------------------------

alter table app.alert
  add column as_of_date    date,
  add column risk_tier     text check (risk_tier in ('low', 'elevated', 'high')),
  add column previous_tier text check (previous_tier in ('low', 'elevated', 'high')),
  add constraint alert_one_per_prediction unique (tenant_id, prediction_id);

create function util.tier_rank(p_tier text) returns integer
language sql immutable set search_path = ''
as $$ select case p_tier when 'high' then 2 when 'elevated' then 1 else 0 end $$;
revoke execute on function util.tier_rank(text) from public;
grant execute on function util.tier_rank(text) to authenticated, service_role;

-- Run by the scoring jobs after new predictions. The demo tenant gets its whole history (dated
-- when the score was made); other tenants only get changes from the day a borrower was added,
-- plus one alert if it was already high risk then (on a score from the last six months).
create function app.generate_alerts() returns integer
language plpgsql volatile set search_path = ''
as $$
declare n integer;
begin
  with borrowers as (
    select b.tenant_id, b.cik, min(b.created_at)::date as added_on, bool_or(t.is_demo) as is_demo
    from app.portfolio_borrower b
    join app.tenant t on t.id = b.tenant_id
    where b.cik is not null and b.match_status in ('auto_matched', 'confirmed')
    group by b.tenant_id, b.cik
  ), scored as (
    select br.tenant_id, br.is_demo, br.added_on, p.id as prediction_id, p.cik, p.as_of_date,
           p.probability, p.risk_tier,
           lag(p.risk_tier) over w as previous_tier,
           lead(p.as_of_date) over w as next_as_of
    from borrowers br
    join ml.prediction p on p.cik = br.cik
    join ml.model_version v on v.id = p.model_version_id and v.is_active
    window w as (partition by br.tenant_id, p.cik order by p.as_of_date)
  ), alerts as (
    select s.*,
      case
        when (s.is_demo or s.as_of_date >= s.added_on) and s.previous_tier is not null
             and util.tier_rank(s.risk_tier) > util.tier_rank(s.previous_tier)
          then format('Risk tier rose from %s to %s (%s%% chance of bankruptcy within 12 months)',
                      s.previous_tier, s.risk_tier, round(s.probability * 100, 1))
        when not s.is_demo and s.risk_tier = 'high'
             and (s.as_of_date < s.added_on or s.previous_tier is null)
             and (s.next_as_of is null or s.next_as_of >= s.added_on)
             and s.as_of_date >= s.added_on - 183  -- a stale score (company stopped filing) is no news
          then format('High risk when added to the portfolio (%s%% chance of bankruptcy within '
                      '12 months)', round(s.probability * 100, 1))
      end as reason
    from scored s
  )
  insert into app.alert (tenant_id, cik, triggered_at, reason, prediction_id, as_of_date,
                         risk_tier, previous_tier)
  select a.tenant_id, a.cik,
         case when a.is_demo then a.as_of_date::timestamptz else now() end,
         a.reason, a.prediction_id, a.as_of_date, a.risk_tier, a.previous_tier
  from alerts a
  where a.reason is not null
  on conflict (tenant_id, prediction_id) do nothing;
  get diagnostics n = row_count;
  return n;
end
$$;
revoke execute on function app.generate_alerts() from public;
grant execute on function app.generate_alerts() to service_role;

-- ---------------------------------------------------------------------------
-- 1. api views for the new screens (security_invoker: the caller's RLS applies).
-- ---------------------------------------------------------------------------

-- Workspaces the caller can open, with their role (null for the public demo).
create view api.tenants with (security_invoker = true) as
select t.id, t.name, t.is_demo, m.role, t.created_at
from app.tenant t
left join app.membership m on m.tenant_id = t.id and m.user_id = (select auth.uid());

create view api.portfolios with (security_invoker = true) as
select p.id, p.tenant_id, p.name, p.created_at,
       (select count(*) from app.portfolio_borrower b where b.portfolio_id = p.id) as borrowers
from app.portfolio p;

-- Every uploaded row with its match and whether the company is scored yet.
create view api.borrowers with (security_invoker = true) as
select
  b.id, b.tenant_id, b.portfolio_id, b.row_number, b.uploaded_name, b.uploaded_ticker,
  b.cik, c.name as sec_name, c.tickers, b.match_status, b.match_confidence, b.match_method,
  b.created_at,
  case
    when b.cik is null or b.match_status not in ('auto_matched', 'confirmed') then null
    when s.cik is null then 'over_capacity'
    else s.status
  end as coverage,
  s.detail as coverage_detail,
  lp.as_of_date, lp.probability, lp.risk_tier
from app.portfolio_borrower b
left join curated.company c on c.cik = b.cik
left join ml.served_company s on s.cik = b.cik
left join lateral (
  select p.as_of_date, p.probability, p.risk_tier
  from ml.prediction p join ml.model_version v on v.id = p.model_version_id and v.is_active
  where p.cik = b.cik order by p.as_of_date desc limit 1
) lp on b.match_status in ('auto_matched', 'confirmed');

-- Open review items. Candidates are SEC company names (public data).
create view api.match_reviews with (security_invoker = true) as
select r.id, r.tenant_id, b.portfolio_id, r.borrower_id, b.row_number, b.uploaded_name,
       b.uploaded_ticker, r.candidate_ciks, r.created_at
from app.match_review r
join app.portfolio_borrower b on b.id = r.borrower_id
where r.decision is null;

create view api.alerts with (security_invoker = true) as
select
  a.id, a.tenant_id, a.cik, coalesce(n.uploaded_name, c.name) as name, a.triggered_at,
  a.as_of_date, a.reason, a.risk_tier, a.previous_tier, p.probability, a.status,
  d.decision, d.reason as disposition_reason, d.created_at as decided_at,
  d.user_id = (select auth.uid()) as decided_by_me
from app.alert a
left join curated.company c on c.cik = a.cik
left join ml.prediction p on p.id = a.prediction_id
left join lateral (
  select x.uploaded_name from app.portfolio_borrower x
  where x.tenant_id = a.tenant_id and x.cik = a.cik order by x.created_at limit 1
) n on true
left join lateral (
  select y.decision, y.reason, y.created_at, y.user_id from app.disposition y
  where y.alert_id = a.id order by y.created_at desc limit 1
) d on true;

grant select on api.tenants, api.portfolios, api.borrowers, api.match_reviews, api.alerts
  to authenticated, service_role;

-- Teammates' emails (auth.users is not readable by clients). Members only.
create function api.tenant_members(p_tenant_id uuid)
returns table (user_id uuid, email text, role text, created_at timestamptz)
language sql stable security definer set search_path = ''
as $$
  select m.user_id, u.email::text, m.role, m.created_at
  from app.membership m join auth.users u on u.id = m.user_id
  where m.tenant_id = p_tenant_id
    and exists (select 1 from app.membership me
                where me.tenant_id = p_tenant_id and me.user_id = (select auth.uid()))
  order by m.created_at
$$;
revoke execute on function api.tenant_members(uuid) from public;
grant execute on function api.tenant_members(uuid) to authenticated;

-- Data Health page: pipeline freshness and quality counts. No tenant data.
create function api.data_health() returns jsonb
language sql stable security definer set search_path = ''
as $$
  select jsonb_build_object(
    'runs', (select coalesce(jsonb_agg(r order by r.source), '[]') from (
      select distinct on (i.source) i.source, i.status, i.started_at, i.finished_at, i.counts,
             i.error
      from raw.ingest_runs i order by i.source, i.started_at desc) r),
    'quarantine', (select coalesce(jsonb_agg(q order by q.total desc), '[]') from (
      select x.rule_violated as rule, x.source, count(*) as total,
             count(*) filter (where x.created_at > now() - interval '30 days') as last_30_days
      from staging.quarantine x group by x.rule_violated, x.source) q),
    'served', (select coalesce(jsonb_object_agg(s.status, s.n), '{}') from (
      select status, count(*) as n from ml.served_company group by status) s),
    'max_companies', (select l.max_companies from ml.serving_limits l),
    'latest_prediction', (select max(p.as_of_date) from ml.prediction p
                          join ml.model_version v on v.id = p.model_version_id and v.is_active),
    'latest_filing', (select max(f.filed_date) from staging.filings f),
    'companies_with_metrics', (select count(distinct m.cik) from curated.financial_metric m),
    'text_signals', (select count(*) from curated.text_signal)
  )
$$;
revoke execute on function api.data_health() from public;
grant execute on function api.data_health() to authenticated;
