# apps/web

Analyst console (Next.js App Router, TypeScript, Tailwind, shadcn/ui), deployed on Vercel.

- `/watchlist`: the demo portfolio ranked by 12-month distress probability, rewindable to any
  past quarter (`?as_of=`). Opens on Spirit Airlines' story (June 30, 2024).
- `/company/[cik]`: score history, top SHAP drivers in plain English, key metrics as known on the
  as-of date (derived and restated values marked), filing events; every number links to EDGAR.
- `/model`: `reports/model_eval.md`, rendered unchanged at build time.

Data comes straight from Supabase: visitors get an anonymous session, and every read goes through
the RLS-protected views in the `api` schema (`supabase/migrations/20261027000100_console.sql`).
The only environment variables are the Supabase URL and anon key.

```bash
cp .env.local.example .env.local   # fill in from `supabase status -o env` or the cloud project
npm install
npm run dev
```

Deploying: [docs/deploy.md](../../docs/deploy.md).
