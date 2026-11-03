-- Phase 5: LLM text signals and "Ask the filings" (keyword retrieval, no embeddings).
--
-- 1. curated.filing_chunk gets a generated full-text index; retrieval is Postgres text search
--    scoped to one company and to filings public on the as-of date. `embedding` stays (nullable)
--    in case a vector index is added later.
-- 2. Signals and chunks are readable by tenants for their portfolio companies (same rule as the
--    other console tables), through api views / functions.
-- 3. ml.qa_answer: one row per question. It reserves quota before the model is called (per user
--    and per day overall) and stores the answer for that user only, so no visitor can plant an
--    answer that another visitor sees.

alter table curated.filing_chunk
  add column form_type text,
  add column tsv tsvector generated always as (to_tsvector('english', chunk_text)) stored;
create index filing_chunk_tsv_idx on curated.filing_chunk using gin (tsv);

grant select on curated.text_signal, curated.filing_chunk to authenticated;
create policy text_signal_select on curated.text_signal for select to authenticated
  using (cik in (select app.user_ciks()));
create policy filing_chunk_select on curated.filing_chunk for select to authenticated
  using (cik in (select app.user_ciks()));

create view api.text_signals with (security_invoker = true) as
select s.cik, s.accession, s.filed_date, s.section, s.signal_type, s.value, s.evidence_quote,
       s.confidence, s.model, s.prompt_version
from curated.text_signal s;
grant select on api.text_signals to authenticated, service_role;

-- Best-matching chunks of one company's filings public on p_as_of. Runs as the caller, so RLS
-- limits it to the caller's portfolio companies.
create function api.search_filings(
  p_cik text, p_query text, p_as_of date, p_limit integer default 8
) returns table (
  id bigint, accession text, form_type text, filed_date date, section text,
  chunk_index integer, chunk_text text, rank real
)
language sql stable security invoker set search_path = ''
as $$
  with q as (select websearch_to_tsquery('english', p_query) as query)
  select c.id, c.accession, c.form_type, c.filed_date, c.section, c.chunk_index, c.chunk_text,
         ts_rank_cd(c.tsv, q.query, 32)::real as rank
  from curated.filing_chunk c, q
  where c.cik = p_cik and c.filed_date <= p_as_of and c.tsv @@ q.query
  order by rank desc, c.filed_date desc
  limit least(greatest(p_limit, 1), 20)
$$;
revoke execute on function api.search_filings(text, text, date, integer) from public;
grant execute on function api.search_filings(text, text, date, integer) to authenticated, service_role;

create table ml.qa_answer (
  id           bigint generated always as identity primary key,
  user_id      uuid not null,
  cik          util.cik not null references curated.company (cik),
  as_of_date   date not null,
  question     text not null check (length(question) between 3 and 500),
  question_key text not null,              -- normalized question, for the per-user cache
  answer       jsonb,                      -- null while the model call is in flight
  model        text,
  created_at   timestamptz not null default now(),
  answered_at  timestamptz
);
alter table ml.qa_answer enable row level security;
create index on ml.qa_answer (created_at);
create index on ml.qa_answer (user_id, cik, as_of_date, question_key);

-- Limits for "Ask the filings". Change them here (or with an update) without a deploy.
create table ml.qa_limits (
  id                 boolean primary key default true check (id),
  per_user_per_day   integer not null check (per_user_per_day >= 0),
  total_per_day      integer not null check (total_per_day >= 0)
);
alter table ml.qa_limits enable row level security;
insert into ml.qa_limits (per_user_per_day, total_per_day) values (3, 30);

-- Start a question: returns the cached answer if this user already asked it, an error code if
-- a limit is reached, or a new row id that reserves the quota.
create function api.qa_begin(p_cik text, p_question text, p_as_of date) returns jsonb
language plpgsql volatile security definer set search_path = ''
as $$
declare
  uid uuid := auth.uid();
  qkey text := lower(regexp_replace(trim(p_question), '\s+', ' ', 'g'));
  lim ml.qa_limits;
  cached jsonb;
  new_id bigint;
begin
  if uid is null then
    return jsonb_build_object('error', 'not_signed_in');
  end if;
  if p_cik not in (select app.user_ciks()) then
    return jsonb_build_object('error', 'unknown_company');
  end if;
  select a.answer into cached from ml.qa_answer a
  where a.user_id = uid and a.cik = p_cik and a.as_of_date = p_as_of
    and a.question_key = qkey and a.answer is not null
  order by a.id desc limit 1;
  if cached is not null then
    return jsonb_build_object('cached', true, 'answer', cached);
  end if;
  select * into lim from ml.qa_limits;
  -- Serialize quota checks so concurrent requests cannot both take the last slot.
  perform pg_advisory_xact_lock(hashtext('ml.qa_answer quota'));
  if (select count(*) from ml.qa_answer
      where user_id = uid and created_at > now() - interval '1 day') >= lim.per_user_per_day then
    return jsonb_build_object('error', 'user_limit', 'limit', lim.per_user_per_day);
  end if;
  if (select count(*) from ml.qa_answer
      where created_at > now() - interval '1 day') >= lim.total_per_day then
    return jsonb_build_object('error', 'daily_limit', 'limit', lim.total_per_day);
  end if;
  insert into ml.qa_answer (user_id, cik, as_of_date, question, question_key)
  values (uid, p_cik, p_as_of, left(trim(p_question), 500), qkey)
  returning id into new_id;
  return jsonb_build_object('id', new_id);
end
$$;

-- Store the answer for the caller's own reserved row.
create function api.qa_finish(p_id bigint, p_answer jsonb, p_model text) returns void
language sql volatile security definer set search_path = ''
as $$
  update ml.qa_answer set answer = p_answer, model = p_model, answered_at = now()
  where id = p_id and user_id = (select auth.uid()) and answer is null
$$;

revoke execute on function api.qa_begin(text, text, date) from public;
revoke execute on function api.qa_finish(bigint, jsonb, text) from public;
grant execute on function api.qa_begin(text, text, date) to authenticated;
grant execute on function api.qa_finish(bigint, jsonb, text) to authenticated;
