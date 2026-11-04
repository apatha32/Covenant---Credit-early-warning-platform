# Deploying the live demo

The console has four hosted pieces:

| Piece | Where | Holds |
|---|---|---|
| Database, auth, storage | Supabase (free tier) | Migrations, the SEC company universe, demo + lender tenants, served companies' curated data, predictions, alerts; model artifacts in the private `tripwire` bucket |
| Web app (`apps/web`) | Vercel (Hobby) | Next.js console. Knows only the Supabase URL, the anon key and the API URL |
| API (`services/api`) | Render (free) | FastAPI: uploads, matching, review decisions, alert dispositions. Database URL + Supabase URL |
| Scheduled jobs | GitHub Actions | `refresh-demo.yml` (weekly: refresh + re-score every served company), `score-new.yml` (hourly: newly added borrowers) |

Visitors are signed in anonymously (Supabase anonymous sign-in). An anonymous session can read
only the demo tenant (`app.tenant.is_demo`) through the `api` views and can write nothing:
see `supabase/migrations/20261027000100_console.sql` and `tests/db/test_demo_access.py`.

The service-role key is used only by Python (your machine and GitHub Actions). It never goes
into `apps/web`, Vercel, or any `NEXT_PUBLIC_*` variable.

## One-time setup

### 1. Supabase project

1. Create a project at <https://supabase.com/dashboard>. Region close to you. Save the database
   password in your password manager.
2. **Project Settings → Data API → Exposed schemas**: add `api`, save.
3. **Authentication → Sign In / Providers**: turn on **Allow anonymous sign-ins**, save.
4. Create `.env.cloud` in the repo root (git-ignored) with:

   ```bash
   SUPABASE_URL=https://<project-ref>.supabase.co          # Project Settings → Data API
   SUPABASE_ANON_KEY=...                                   # Project Settings → API keys (anon / publishable)
   SUPABASE_SERVICE_ROLE_KEY=...                           # Project Settings → API keys (service_role / secret)
   # Connect → Session pooler (IPv4, port 5432). URL-encode special characters in the password.
   DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```

### 2. Database and data

From the repo root, with the local training data in `data/` (Phases 1–4):

```bash
make cloud-migrate   # supabase db push: every migration, tracked in supabase_migrations
make demo-bundle     # local DB + Parquet -> data/demo_bundle/ (active model, tier cutoffs,
                     #   demo predictions and bankruptcy cases)
make cloud-seed      # creates the Storage bucket, per-company SEC + FRED refresh for the 50
                     #   companies, uploads model artifacts, loads tenant / portfolio / history
make cloud-size      # database size
```

`cloud-seed` is idempotent. It loads only the demo companies; the training universe stays in local
Parquet.

### 3. Vercel

1. <https://vercel.com/new> → import `apatha32/Covenant---Credit-early-warning-platform`.
2. **Root Directory**: `apps/web`. Framework preset: Next.js (auto). Leave "Include files outside
   the root directory" on: the model card page reads `reports/model_eval.md` at build time.
3. **Environment Variables** (Production and Preview):
   - `NEXT_PUBLIC_SUPABASE_URL` = the `SUPABASE_URL` value
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY` = the anon / publishable key
4. For "Ask the filings" (Phase 5), also add `ANTHROPIC_API_KEY` (no `NEXT_PUBLIC_` prefix, so it
   stays on the server; Production only). Optional: `QA_MODEL` (default `claude-sonnet-5-5`).
   Without the key the panel says the feature is off; everything else works.
5. Deploy. Production deploys follow `main`; every PR gets a preview URL.

### 3b. Accounts and the API (Phase 6b)

1. **Supabase → Authentication → URL Configuration**: Site URL = the Vercel URL (confirmation
   emails link there). **Sign In / Providers → Email** stays on. "Confirm email" on means each new
   account must click an email link; Supabase's built-in mailer sends only a few emails per hour,
   so for a demo you can turn it off.
2. **Render**: <https://dashboard.render.com> → New → **Blueprint** → this repo. It reads
   `render.yaml` (Docker, free plan, health check `/health`). When asked, enter:
   - `DATABASE_URL` = the session pooler URL (same as `.env.cloud`)
   - `SUPABASE_URL` = `https://<project-ref>.supabase.co`
   The API never needs the service-role key or the anon key: it verifies tokens with the
   project's public JWKS and talks to Postgres directly.
3. **Vercel**: add `NEXT_PUBLIC_API_URL` = the Render URL (e.g. `https://covenant-api.onrender.com`),
   then redeploy.
4. If the web app's URL changes, update `API_CORS_ORIGINS` on Render.

The free Render instance sleeps after 15 idle minutes; the first request after that takes up to
a minute (the web app pings `/health` when the Portfolio page opens and says so while waiting).

### 4. GitHub Actions

Repo → **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| Secret | `DATABASE_URL` | Session pooler URL (same as `.env.cloud`) |
| Secret | `SUPABASE_SERVICE_ROLE_KEY` | service_role / secret key |
| Secret | `SEC_USER_AGENT` | e.g. `Covenant Research you@example.com` (needs a real contact email) |
| Secret | `FRED_API_KEY` | your FRED key |
| Variable | `SUPABASE_URL` | `https://<project-ref>.supabase.co` |

Failure emails: GitHub notifies the user who last changed the schedule. Check **your profile →
Settings → Notifications → Actions** is set to email on failed workflows.

## "Ask the filings" limits and spend

Each question costs about 2 cents (Claude Sonnet 5.5 with ~8 filing excerpts). Limits live in the
database, so changing them needs no deploy:

```sql
update ml.qa_limits set per_user_per_day = 3, total_per_day = 30;   -- current defaults
select count(*), min(created_at) from ml.qa_answer where created_at > now() - interval '1 day';
```

Also set a monthly spend limit on the API key's workspace in the Anthropic Console.

## Text signals (Phase 5, run from a laptop)

The weekly job does not extract text signals yet (it would need the section files in Storage).
To add signals for new filings, from the repo root with `.env.cloud` loaded:

```bash
set -a && . ./.env.cloud && set +a
uv run python -m tripwire_llm sections          # new filings -> sections + Q&A chunks
uv run python -m tripwire_llm estimate          # cost check (exact token counts on a sample)
uv run python -m tripwire_llm signals --submit  # Batch API; refuses above budget_usd
uv run python -m tripwire_llm signals --collect # waits, verifies quotes, loads curated.text_signal
```

## Lender data: capacity and scoring

New borrowers matched in a lender workspace are queued in `ml.served_company`. The hourly
`score-new` job fetches their SEC data and scores them today plus every past as-of date since
2019; the weekly job keeps them fresh. Each served company adds about 4 MB to the database, so
the number served is capped (`ml.serving_limits`, default 70 including the 50 demo companies).
Companies over the cap stay matched but unscored, and the Portfolio page says so.

```sql
update ml.serving_limits set max_companies = 70;
select status, count(*) from ml.served_company group by 1;
```

## Running the weekly refresh by hand

Actions → **refresh-demo** → **Run workflow** (optional `as_of`). Or locally: `make cloud-refresh`.

Each run writes `raw.ingest_runs` rows (`sec_submissions`, `sec_companyfacts`, `fred`,
`pipeline_staging`, `pipeline_curated`, `pipeline_events`, `ml_demo_refresh`). The job fails if any
step ends `failed` or `partial`.

```sql
select id, source, status, started_at, counts from raw.ingest_runs order by id desc limit 8;
```

New predictions are dated the run day; the watchlist date picker lists them as "weekly refresh".

## Redeploying

| Change | Do |
|---|---|
| Web app code | Merge to `main`. Vercel redeploys. |
| API code (`services/`, `packages/`, `config/`) | Merge to `main`. Render redeploys. |
| New migration | `make cloud-migrate` (before merging code that needs it). |
| New model (after `python -m tripwire_ml train`) | `make demo-bundle && make cloud-seed`. The new version becomes active; old predictions stay. |
| Different demo companies | Edit / regenerate `config/domain/finance/demo_portfolio.yaml` (`scripts/build_demo_portfolio.py`), then `make demo-bundle && make cloud-seed`. |
| Rotate the service-role key | Supabase → API keys, then update `.env.cloud` and the GitHub secret. |
| Rotate the anon key | Supabase → API keys, then update the Vercel variable and redeploy. |

## Local stack (same as production)

```bash
supabase start -x studio,imgproxy,inbucket,edge-runtime,logflare,vector,supavisor,realtime
# .env.cloud-style values from `supabase status -o env`; DATABASE_URL on port 54322
cd apps/web && cp .env.local.example .env.local && npm run dev
```
