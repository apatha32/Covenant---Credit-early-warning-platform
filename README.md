# Tripwire — credit early warning platform

Messy SEC filings in, explainable 12-month distress alerts out. See [SPEC.md](SPEC.md) for the
full build spec and phase plan.

**Status:** Phases 0 (foundations) and 1 (ingestion) complete. Cleaning & canonical model (Phase 2) is next.

## Layout

```
packages/common/     tripwire_common: settings (env), YAML config loaders, JSON logging, DB helpers
services/ingest/     SEC + FRED clients, rate limiter, raw Parquet landing, dev sample, Item 1.03 scan
services/            pipeline, ml, llm, api: one uv workspace member each (empty until their phase)
supabase/migrations/ schema + RLS, applied by the Supabase CLI
supabase/local/      supabase_shim.sql: Supabase roles + auth.uid() for plain-Postgres dev/tests
config/              models.yaml (LLM/ML settings) and domain/finance/ (the finance domain pack)
tests/               common/, db/ (migrations, schema guards, RLS), ingest/ (fake SEC/FRED, end to end)
reports/             committed data reports (Item 1.03 spot-check)
apps/web/            Next.js console (Phase 6)
```

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
cp .env.example .env      # fill in keys as phases need them
make setup                # uv sync --all-packages (installs Python 3.12 env)
make db-up                # Postgres 17 + pgvector on localhost:54329
make check                # ruff, mypy --strict, pytest
make db-reset             # optional: wipe the local DB and apply shim + migrations to it
```

DB tests create and drop their own throwaway database on `TEST_DATABASE_URL`, so they never
touch your working data. Without a reachable server they skip; with `REQUIRE_DB=1` (CI) they fail.

## Ingestion (Phase 1)

Needs `SEC_USER_AGENT` (name + email) and `FRED_API_KEY` in `.env`, and the local DB (`make db-reset`).

```sh
uv run python -m tripwire_ingest dev        # tickers, sample, submissions, companyfacts, fred, scan
uv run python -m tripwire_ingest --help     # individual commands
```

- **Raw landing:** Parquet goes to `data/` (local storage backend) under content-addressed keys
  (`.../sha256=<payload hash>/...`), indexed in `raw.*_index` / `raw.source_files`. Re-running
  writes nothing new for unchanged data (counted as `unchanged` in `raw.ingest_runs.counts`).
- **Rate limit:** SEC requests are capped at 8/sec (token bucket, retries included). Every run
  records `max_requests_per_second` actually observed.
- **Dev sample:** `config/domain/finance/dev_sample.yaml` is generated once and committed, so
  every re-run ingests the same 205 companies.
- **Item 1.03 scan:** `reports/item_103_spotcheck.md` (generated) plus
  `reports/item_103_review.md` (manual review). Issues are logged to `staging.quarantine`.

Against a real Supabase stack instead of the shim: `supabase db start` (database only) or
`supabase start` (full local stack), then `supabase db push` to deploy to a linked project.

## Database design

| Schema | Holds | Who can read |
|---|---|---|
| `raw` | ingest run log, index of raw Parquet files in Storage | service_role |
| `staging` | typed facts and filings, quarantine, LLM call cache | service_role |
| `curated` | canonical point-in-time model: companies, metrics, events, text signals, chunks, macro | service_role |
| `ml` | feature snapshots, labels, model registry, predictions, drift | service_role |
| `app` | tenants, portfolios, matches, alerts, dispositions, audit log | authenticated, own tenant only |

Key decisions:

- **Tenant isolation in two layers.** RLS on every `app` table scopes rows to the caller's
  memberships (`app.user_tenant_ids()`, `app.has_role()`). Children carry `tenant_id` and
  reference parents by the composite `(id, tenant_id)`, so a row cannot point at another
  tenant's parent even though FK checks bypass RLS. Column-level UPDATE grants stop users from
  moving rows across tenants or editing fields like an alert's reason.
- **Roles:** owner > admin > analyst > viewer. Viewers read only; only owners can grant owner.
- **Pipeline schemas are service-only.** RLS is still enabled on every table (deny by default)
  so an accidental grant exposes nothing. The tests fail if any table lacks RLS.
- **History is append-only.** `curated.financial_metric`, `ml.prediction`, `app.disposition`,
  `app.audit_log` and the raw indexes reject UPDATE/DELETE with a trigger. Restatements are new
  `version`s; derived values (e.g. Q4 = FY − 9M) must carry `derivation` lineage.
- **Point-in-time.** Every row a feature can read carries `filed_date`. `ml.feature_snapshot`
  has a check that its `max_source_filed_date <= as_of_date`, a last-line leakage guard behind
  the Phase 4 leakage test. Macro data is bitemporal (`observation_date`, `release_date`).
- **SEC identifiers are validated types:** `util.cik` (10-digit zero-padded) and
  `util.accession` (`0000320193-23-000106`). Filings are keyed by `(cik, accession)` because one
  accession can list several co-registrants (relevant to the 8-K 1.03 subsidiary issue).
- **Config, not code:** LLM model names live in `config/models.yaml` (a test fails if a model
  ID appears in Python); event codes, SIC exclusions and label rules live in
  `config/domain/finance/`.

## Open questions

1. **Embeddings provider.** Claude has no embeddings endpoint. `models.yaml` assumes Voyage
   (`voyage-3.5`, 1024 dims, matching `curated.filing_chunk.embedding`). Confirm before Phase 5.
2. **Tenant-provided aliases.** `curated.company_alias` is global. Reusing one tenant's match
   decisions for another would leak borrower names across tenants; Phase 3 should keep
   tenant-provided aliases tenant-scoped.
3. **Bankruptcy labels need text checks.** The raw Item 1.03 tag includes mis-tags, co-registrant
   and subsidiary-only bankruptcies, and follow-up filings (see `reports/item_103_review.md`).
4. **WeWork vs sector exclusion.** WeWork is SIC 6512 (real estate), so the spec's exclusion
   rule removes a bankruptcy the spec names as a dev-sample example.
5. **Credit spread.** FRED now serves only 3 years of `BAMLH0A0HYM2`; `BAA10Y` replaces it
   (SPEC.md 3.9).
6. **Tenant offboarding.** Alerts, dispositions and audit rows are history and do not cascade,
   so deleting a tenant needs an explicit admin procedure.
