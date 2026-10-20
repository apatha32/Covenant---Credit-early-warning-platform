-- app: tenant-facing tables. Every table carries tenant_id and is scoped by RLS.
--
-- tenant_id is denormalized onto child tables, and children reference parents by the
-- composite (id, tenant_id). A row therefore can never point at another tenant's parent,
-- even though FK checks themselves bypass RLS.
--
-- Roles: owner > admin > analyst > viewer. Viewers read; analysts and up change portfolio data;
-- owners/admins manage memberships. Tenants are created by the API (service_role).

create table app.tenant (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  created_at timestamptz not null default now()
);

create table app.membership (
  user_id    uuid not null references auth.users (id) on delete cascade,
  tenant_id  uuid not null references app.tenant (id) on delete cascade,
  role       text not null check (role in ('owner', 'admin', 'analyst', 'viewer')),
  created_at timestamptz not null default now(),
  primary key (user_id, tenant_id)
);
create index on app.membership (tenant_id);

create table app.portfolio (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references app.tenant (id) on delete cascade,
  name       text not null,
  created_by uuid references auth.users (id) default auth.uid(),
  created_at timestamptz not null default now(),
  unique (id, tenant_id),
  unique (tenant_id, name)
);

create table app.portfolio_borrower (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null,
  portfolio_id     uuid not null,
  cik              util.cik references curated.company (cik),
  uploaded_name    text,
  uploaded_ticker  text,
  match_status     text not null default 'pending'
    check (match_status in ('pending', 'auto_matched', 'needs_review', 'confirmed', 'rejected', 'unmatched')),
  match_confidence numeric check (match_confidence between 0 and 1),
  created_at       timestamptz not null default now(),
  unique (id, tenant_id),
  foreign key (portfolio_id, tenant_id) references app.portfolio (id, tenant_id) on delete cascade,
  check (uploaded_name is not null or uploaded_ticker is not null)
);
create index on app.portfolio_borrower (tenant_id);
create index on app.portfolio_borrower (portfolio_id);
create index on app.portfolio_borrower (cik);

create table app.match_review (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null,
  borrower_id    uuid not null,
  candidate_ciks jsonb not null,             -- [{cik, name, score, method}, ...]
  decision       text check (decision in ('accept', 'reject', 'no_match')),
  decided_cik    util.cik references curated.company (cik),
  decided_by     uuid references auth.users (id),
  decided_at     timestamptz,
  created_at     timestamptz not null default now(),
  foreign key (borrower_id, tenant_id) references app.portfolio_borrower (id, tenant_id) on delete cascade,
  check ((decision is null) = (decided_at is null)),
  check (decision is distinct from 'accept' or decided_cik is not null)
);
create index on app.match_review (tenant_id);
create index on app.match_review (borrower_id);

-- alert, disposition and audit_log keep history: no cascades from tenant deletion.
create table app.alert (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references app.tenant (id),
  cik           util.cik not null references curated.company (cik),
  triggered_at  timestamptz not null default now(),
  reason        text not null,
  prediction_id bigint references ml.prediction (id),
  status        text not null default 'open' check (status in ('open', 'acknowledged', 'closed')),
  unique (id, tenant_id)
);
create index on app.alert (tenant_id, status, triggered_at desc);

create table app.disposition (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null,
  alert_id   uuid not null,
  user_id    uuid not null references auth.users (id) default auth.uid(),
  decision   text not null check (decision in ('agree', 'dismiss')),
  reason     text,
  created_at timestamptz not null default now(),
  foreign key (alert_id, tenant_id) references app.alert (id, tenant_id),
  check (decision = 'agree' or reason is not null)
);
create index on app.disposition (tenant_id);
create index on app.disposition (alert_id, created_at desc);
create trigger disposition_append_only before update or delete on app.disposition
  for each row execute function util.reject_mutation();

create table app.audit_log (
  id            bigint generated always as identity primary key,
  tenant_id     uuid references app.tenant (id),  -- null for system-level events
  actor_user_id uuid references auth.users (id),
  action        text not null,
  entity_type   text,
  entity_id     text,
  details       jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);
create index on app.audit_log (tenant_id, created_at desc);
create trigger audit_log_append_only before update or delete on app.audit_log
  for each row execute function util.reject_mutation();


-- ---------------------------------------------------------------------------
-- Tenant-scope helpers. SECURITY DEFINER so policies can read membership without
-- recursing into membership's own RLS policy.
-- ---------------------------------------------------------------------------

create function app.user_tenant_ids() returns setof uuid
language sql stable security definer set search_path = ''
as $$
  select m.tenant_id from app.membership m where m.user_id = (select auth.uid())
$$;

create function app.has_role(p_tenant_id uuid, p_roles text[]) returns boolean
language sql stable security definer set search_path = ''
as $$
  select exists (
    select 1 from app.membership m
    where m.tenant_id = p_tenant_id
      and m.user_id = (select auth.uid())
      and m.role = any (p_roles)
  )
$$;

revoke execute on function app.user_tenant_ids() from public;
revoke execute on function app.has_role(uuid, text[]) from public;
grant execute on function app.user_tenant_ids() to authenticated, service_role;
grant execute on function app.has_role(uuid, text[]) to authenticated, service_role;


-- ---------------------------------------------------------------------------
-- RLS + grants. Grants say which columns a user may touch at all; policies say which rows.
-- ---------------------------------------------------------------------------

alter table app.tenant             enable row level security;
alter table app.membership         enable row level security;
alter table app.portfolio          enable row level security;
alter table app.portfolio_borrower enable row level security;
alter table app.match_review       enable row level security;
alter table app.alert              enable row level security;
alter table app.disposition        enable row level security;
alter table app.audit_log          enable row level security;

-- tenant: members read; writes via service_role only.
grant select on app.tenant to authenticated;
create policy tenant_select on app.tenant for select to authenticated
  using (id in (select app.user_tenant_ids()));

-- membership: members see their tenant's roster; owners/admins manage it.
-- Only an owner may create, promote to, or remove an owner.
grant select, insert, delete on app.membership to authenticated;
grant update (role) on app.membership to authenticated;
create policy membership_select on app.membership for select to authenticated
  using (tenant_id in (select app.user_tenant_ids()));
create policy membership_insert on app.membership for insert to authenticated
  with check (
    app.has_role(tenant_id, '{owner}')
    or (app.has_role(tenant_id, '{admin}') and role <> 'owner')
  );
create policy membership_update on app.membership for update to authenticated
  using (
    app.has_role(tenant_id, '{owner}')
    or (app.has_role(tenant_id, '{admin}') and role <> 'owner')
  )
  with check (
    app.has_role(tenant_id, '{owner}')
    or (app.has_role(tenant_id, '{admin}') and role <> 'owner')
  );
create policy membership_delete on app.membership for delete to authenticated
  using (
    app.has_role(tenant_id, '{owner}')
    or (app.has_role(tenant_id, '{admin}') and role <> 'owner')
  );

-- portfolio
grant select, insert, delete on app.portfolio to authenticated;
grant update (name) on app.portfolio to authenticated;
create policy portfolio_select on app.portfolio for select to authenticated
  using (tenant_id in (select app.user_tenant_ids()));
create policy portfolio_insert on app.portfolio for insert to authenticated
  with check (app.has_role(tenant_id, '{owner,admin,analyst}'));
create policy portfolio_update on app.portfolio for update to authenticated
  using (app.has_role(tenant_id, '{owner,admin,analyst}'))
  with check (app.has_role(tenant_id, '{owner,admin,analyst}'));
create policy portfolio_delete on app.portfolio for delete to authenticated
  using (app.has_role(tenant_id, '{owner,admin,analyst}'));

-- portfolio_borrower
grant select, insert, delete on app.portfolio_borrower to authenticated;
grant update (cik, match_status, match_confidence) on app.portfolio_borrower to authenticated;
create policy portfolio_borrower_select on app.portfolio_borrower for select to authenticated
  using (tenant_id in (select app.user_tenant_ids()));
create policy portfolio_borrower_insert on app.portfolio_borrower for insert to authenticated
  with check (app.has_role(tenant_id, '{owner,admin,analyst}'));
create policy portfolio_borrower_update on app.portfolio_borrower for update to authenticated
  using (app.has_role(tenant_id, '{owner,admin,analyst}'))
  with check (app.has_role(tenant_id, '{owner,admin,analyst}'));
create policy portfolio_borrower_delete on app.portfolio_borrower for delete to authenticated
  using (app.has_role(tenant_id, '{owner,admin,analyst}'));

-- match_review: decisions are attributed to the deciding user.
grant select, insert on app.match_review to authenticated;
grant update (decision, decided_cik, decided_by, decided_at) on app.match_review to authenticated;
create policy match_review_select on app.match_review for select to authenticated
  using (tenant_id in (select app.user_tenant_ids()));
create policy match_review_insert on app.match_review for insert to authenticated
  with check (app.has_role(tenant_id, '{owner,admin,analyst}'));
create policy match_review_update on app.match_review for update to authenticated
  using (app.has_role(tenant_id, '{owner,admin,analyst}'))
  with check (
    app.has_role(tenant_id, '{owner,admin,analyst}')
    and decided_by = (select auth.uid())
  );

-- alert: created by the scoring job (service_role); users only change status.
grant select on app.alert to authenticated;
grant update (status) on app.alert to authenticated;
create policy alert_select on app.alert for select to authenticated
  using (tenant_id in (select app.user_tenant_ids()));
create policy alert_update on app.alert for update to authenticated
  using (app.has_role(tenant_id, '{owner,admin,analyst}'))
  with check (app.has_role(tenant_id, '{owner,admin,analyst}'));

-- disposition: append-only feedback, always recorded as the current user.
grant select, insert on app.disposition to authenticated;
create policy disposition_select on app.disposition for select to authenticated
  using (tenant_id in (select app.user_tenant_ids()));
create policy disposition_insert on app.disposition for insert to authenticated
  with check (
    app.has_role(tenant_id, '{owner,admin,analyst}')
    and user_id = (select auth.uid())
  );

-- audit_log: written by the API (service_role); owners/admins read their tenant's log.
grant select on app.audit_log to authenticated;
create policy audit_log_select on app.audit_log for select to authenticated
  using (app.has_role(tenant_id, '{owner,admin}'));
