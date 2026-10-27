# Deploying the live demo

The demo has three hosted pieces:

| Piece | Where | Holds |
|---|---|---|
| Database, auth, storage | Supabase (free tier) | Migrations, demo tenant, 50 demo companies' curated data, predictions; model artifacts in the private `tripwire` bucket |
| Web app (`apps/web`) | Vercel (Hobby) | Next.js console. Knows only the Supabase URL and anon key |
| Weekly refresh | GitHub Actions (`refresh-demo.yml`) | Per-company SEC API + FRED refresh, re-score, new predictions |

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
4. Deploy. Production deploys follow `main`; every PR gets a preview URL.

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
