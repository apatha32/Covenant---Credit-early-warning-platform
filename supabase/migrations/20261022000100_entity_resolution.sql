-- Phase 3: entity resolution.
--
-- app.entity_alias stores a tenant's human match decisions keyed by the normalized input
-- (name|ticker), so the same input is resolved the same way next time. It is tenant-scoped on
-- purpose: one tenant's borrower names and decisions are never visible to, or reused for,
-- another tenant (curated.company_alias stays SEC-only).

alter table app.portfolio_borrower
  add column row_number   integer check (row_number >= 1),  -- line in the uploaded file
  add column input_key    text,                             -- normalized name|ticker
  add column match_method text,                             -- which cascade step decided
  add column matched_at   timestamptz;
create index on app.portfolio_borrower (tenant_id, input_key);

create table app.entity_alias (
  tenant_id  uuid not null references app.tenant (id) on delete cascade,
  input_key  text not null,
  cik        util.cik references curated.company (cik),
  decision   text not null check (decision in ('accept', 'no_match')),
  decided_by uuid not null references auth.users (id) default auth.uid(),
  decided_at timestamptz not null default now(),
  primary key (tenant_id, input_key),
  check ((decision = 'accept') = (cik is not null))
);
alter table app.entity_alias enable row level security;

grant select, insert on app.entity_alias to authenticated;
grant update (cik, decision, decided_by, decided_at) on app.entity_alias to authenticated;
create policy entity_alias_select on app.entity_alias for select to authenticated
  using (tenant_id in (select app.user_tenant_ids()));
create policy entity_alias_insert on app.entity_alias for insert to authenticated
  with check (
    app.has_role(tenant_id, '{owner,admin,analyst}') and decided_by = (select auth.uid())
  );
create policy entity_alias_update on app.entity_alias for update to authenticated
  using (app.has_role(tenant_id, '{owner,admin,analyst}'))
  with check (
    app.has_role(tenant_id, '{owner,admin,analyst}') and decided_by = (select auth.uid())
  );

-- Re-uploading a file must not duplicate borrowers: one row per normalized input per portfolio.
create unique index portfolio_borrower_input_key_idx
  on app.portfolio_borrower (portfolio_id, input_key) where input_key is not null;
