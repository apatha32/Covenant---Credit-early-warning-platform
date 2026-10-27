# COVENANT — Credit Early Warning Platform

> Build spec for Claude Code. Build phase by phase. Do not start a phase until the previous phase's acceptance criteria pass.

**Repo:** `Covenant---Credit-early-warning-platform` (product name: Covenant; Python packages keep the internal `tripwire_*` names)
**One-liner:** Messy SEC filings in, explainable 12-month distress alerts out. Point-in-time data pipeline, entity resolution, LightGBM + LLM risk signals, multi-tenant analyst console.

---

## 1. Product

**What:** A platform that ingests messy public financial data (SEC EDGAR), normalizes it into a canonical point-in-time credit data model, predicts 12-month financial distress for companies, and serves explainable risk alerts to credit analysts in a multi-tenant web app.

**Customer persona:** Credit risk team at a mid-market lender or private credit fund monitoring a portfolio of ~200–2,000 borrowers that file with the SEC.

**Core user loop:**
1. Tenant uploads their borrower list (messy CSV: names, tickers, sometimes nothing useful).
2. The system resolves each borrower to an SEC entity (CIK). Low-confidence matches go to a human review queue.
3. The system shows a watchlist ranked by distress probability, with drivers ("why flagged").
4. The analyst drills in: financial timeline, filing events, text-derived risk signals, and Q&A over filings with citations.
5. The analyst records a disposition (agree / dismiss + reason). This feedback is stored for evaluation.

**Non-goals (v1):** banks, insurers and REITs (their financial structure differs; excluded by SIC code); private companies; real-time market data; trading signals.

**Design principle:** Keep the core engine domain-agnostic. Domain-specific logic (metric mapping, label rules, sector exclusions, event codes) lives in config as a "finance domain pack," so a second domain pack (e.g., energy/utilities) could be added later without changing the engine.

---

## 2. Stack

- **Railway:** Python 3.12 services: `ingest` (cron worker), `pipeline` (transforms), `trainer` (on-demand job), `api` (FastAPI scoring + app backend).
- **Supabase:** Postgres (schemas below), Auth, Row Level Security for multi-tenancy, Storage for raw Parquet + model artifacts, pgvector for filing text chunks.
- **Vercel:** Next.js (App Router, TypeScript, Tailwind, shadcn/ui) analyst console.
- **Processing:** Polars + DuckDB for heavy transforms on Parquet. Postgres holds only curated, served data (stay within storage limits).
- **ML:** scikit-learn (baseline), LightGBM (main), SHAP (explanations).
- **LLM (Claude API):**
  - `claude-haiku-5-5` for bulk text-signal extraction (high volume, cost-sensitive).
  - `claude-sonnet-5-5` for "Ask the filings" Q&A, and as an upgrade for any signal type where Haiku underperforms on the eval set.
  - Model names live in config, never hard-coded.
  - Cache every LLM result by (accession_number, section, prompt_version, model).
- **Quality:** pytest, ruff, mypy; Pandera for dataframe contracts; GitHub Actions CI.

---

## 3. Data sources & acquisition

### 3.1 Sources

| Source | Endpoint | Use |
|---|---|---|
| Company facts (XBRL) | `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json` or bulk `companyfacts.zip` | Financial metrics |
| Submissions | `https://data.sec.gov/submissions/CIK{cik10}.json` or bulk `submissions.zip` | Filing history, form types, 8-K item codes, SIC, former names |
| Ticker map | `https://www.sec.gov/files/company_tickers.json` | Ticker ↔ CIK |
| Filing documents | EDGAR Archives by accession number | 10-K/10-Q/8-K text for LLM extraction |
| FRED | FRED API (key required) | Macro series |

### 3.2 Setup
- `.env` (provide `.env.example`):
  - `SEC_USER_AGENT="Ambarish <your-email>"`
  - `FRED_API_KEY=...`
  - `ANTHROPIC_API_KEY=...`
  - `SUPABASE_URL=...`, `SUPABASE_SERVICE_ROLE_KEY=...`, `SUPABASE_ANON_KEY=...`, `DATABASE_URL=...`
- FRED key: free signup at https://fred.stlouisfed.org/docs/api/api_key.html
- SEC needs no key, but every request MUST include `User-Agent` = `SEC_USER_AGENT` and stay ≤10 requests/sec. Implement a token-bucket rate limiter + retries with exponential backoff. Missing or abusive headers get the IP blocked.

### 3.3 Identifiers
- CIK is zero-padded to 10 digits in data.sec.gov URLs (`320193` → `0000320193`) and unpadded in Archives paths.
- Accession number `0000320193-23-000106` → strip dashes for Archives paths (`000032019323000106`).

### 3.4 Ticker → CIK map
- GET `https://www.sec.gov/files/company_tickers.json` → load into `curated.company` + `curated.company_alias`.

### 3.5 Bulk downloads (preferred for the training universe)
- Company facts (all XBRL facts, all filers): `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`
- Submissions (filing history, all filers): `https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`
- Both are large (multiple GB unzipped). Download to Railway disk or local, stream-parse per-company JSON into Parquet with Polars, upload Parquet to Supabase Storage. NEVER load raw bulk JSON into Postgres.
- SEC rebuilds bulk files nightly; refresh weekly.
- If either URL 404s, find the current links on SEC's "EDGAR Application Programming Interfaces" page and update config.

### 3.6 Per-company API (incremental updates + tenant portfolio companies)
- Facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json`
- Submissions: `https://data.sec.gov/submissions/CIK{cik10}.json`
  - `filings.recent` holds only the most recent ~1,000 filings. Older filings are in extra JSON files listed under `filings.files`; fetch those too: `https://data.sec.gov/submissions/{name}`.
  - Each filing has `form`, `filingDate`, `reportDate`, `accessionNumber`, `primaryDocument`, and for 8-Ks an `items` string like `"1.03,9.01"`. Parse items into `curated.filing_event`.

### 3.7 Filing text (LLM layer)
- Filing folder: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/`
- Main document: same folder + `primaryDocument` from submissions.
- HTML → text with selectolax or BeautifulSoup; strip inline XBRL tags; split sections by Item headers (Item 1A Risk Factors, Item 7 MD&A, Item 7A, Item 8 incl. auditor report; 10-Q Part I Item 2 MD&A).
- Section detection will be messy; log failures to quarantine instead of guessing.

### 3.8 Event codes (finance domain pack config)
- `1.03` bankruptcy/receivership — **PRIMARY LABEL**
- `3.01` delisting notice
- `4.01` auditor change
- `4.02` non-reliance on previously issued financials
- `5.02` executive/director departure
- Forms `NT 10-K` and `NT 10-Q` = late filing notices (strong feature)

### 3.9 FRED series
- ~~`BAMLH0A0HYM2` (high-yield OAS)~~, `BAA10Y` (replacement, see below), `FEDFUNDS`, `UNRATE`, `INDPRO`, `DGS10`
- Endpoint: `https://api.stlouisfed.org/fred/series/observations?series_id={id}&api_key={key}&file_type=json`
- Point-in-time: use observations as of their release date, or lag one month to be safe.
- **Updated 2026-10-20 (Phase 1), source behaved differently than specified:**
  - Since April 2026 FRED serves only the last 3 years of `BAMLH0A0HYM2` (first observation 2023-10-23), so it cannot cover a 2010+ training window. Replaced by `BAA10Y` (Moody's Baa corporate yield minus 10-year Treasury, daily since 1986) as the credit-spread feature.
  - Monthly series (`FEDFUNDS`, `UNRATE`, `INDPRO`) are pulled with every ALFRED vintage (`realtime_start=1776-07-04&realtime_end=9999-12-31`); release date = `realtime_start`.
  - Daily market series (`BAA10Y`, `DGS10`) exceed FRED's 2,000-vintage limit per request and are not revised, so they are pulled without vintages; release date = observation date + 1 day.
  - Configured in `config/domain/finance/macro_series.yaml`.

### 3.10 Sector exclusions (by SIC from submissions)
- Exclude SIC 6000–6799 (banks, insurance, REITs, investment vehicles), including shell companies (6770).
- Record `is_excluded` + `exclusion_reason`; don't delete.

### 3.11 Dev sample (Phase 1)
- ~200 companies: ~180 random non-excluded filers with XBRL since 2015, plus ~20 known Chapter 11 filers, e.g., Hertz (2020), J.C. Penney (2020), Frontier Communications (2020), Chesapeake Energy (2020), Revlon (2022), Party City (2023), Bed Bath & Beyond (2023), Rite Aid (2023), WeWork (2023).
- Resolve their CIKs via `company_tickers.json` or submissions data (tickers often change after bankruptcy; match by former names).
- Derive the full positive-label set programmatically by scanning all submissions for 8-K item 1.03; never hand-maintain it.
- Spot-check the 1.03 scan on the dev sample: some hits will be subsidiaries or oddly tagged. Log them as data issues; don't silently fix them.
- **Updated 2026-10-20 (Phase 1):**
  - The seed list (with verified CIKs) lives in `config/domain/finance/known_bankruptcies.yaml`. CEC Entertainment was dropped (no Item 1.03 filing on EDGAR).
  - WeWork has SIC 6512 (real estate operators), so the 3.10 exclusion rule excludes it. It stays in the seed list, flagged, until the exclusion rule is revisited (open question).
  - Bankruptcies produce several Item 1.03 8-Ks (petition, then confirmation/emergence), and some companies file Chapter 11 twice. Filing dates alone cannot separate them; see `reports/item_103_spotcheck.md`.
  - The random part of the dev sample is drawn from today's `company_tickers.json`, so it only includes surviving companies. The Phase 4 training universe comes from the bulk files instead.

---

## 4. The hard data problems (the point of the project; do them properly)

1. **Tag chaos:** The same concept appears under many US-GAAP tags across companies and years (e.g., revenue: `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`, `SalesRevenueGoodsNet`, ...). Build `config/metric_mapping.yaml`: canonical_metric → ordered list of candidate tags + sign conventions. Resolution picks the highest-priority tag available per company-period and records which tag was used (lineage).
2. **Duration vs instant facts:** Balance sheet = instant; income/cash flow = duration. Store both correctly.
3. **YTD → quarterly:** Many 10-Q facts are year-to-date. Derive discrete quarters (Q2 = 6M YTD − Q1; Q4 = FY − 9M YTD). Flag derived values.
4. **Restatements / amendments:** The same period is reported multiple times. Keep ALL versions (bitemporal: `period_end` + `filed_date`). Never overwrite.
5. **Point-in-time correctness (critical):** Every feature for as-of date T may only use facts with `filed_date <= T`. Write an explicit test that fails if any feature uses future-filed data. Leakage invalidates the whole model.
6. **Units & scale:** USD vs shares vs pure; reject or convert non-USD; detect obvious scale errors (values off by ~1000x versus neighbors) → quarantine.
7. **Entity resolution:** CIK is the stable key, but tenants upload messy names/tickers. Match via ticker (exact) → normalized name (strip Inc/Corp/LLC/Ltd, punctuation, case) → fuzzy match (rapidfuzz) against current + former names → confidence score. Confidence < threshold → review queue. Human decisions are stored and reused. *(Phase 3: decisions are stored per tenant in `app.entity_alias`, never in the shared `curated.company_alias`, so one tenant's borrower names are never visible to or reused for another. A ticker whose company disagrees with the uploaded name is a conflict and goes to review.)*
8. **Fiscal calendars:** Non-December fiscal years; align features on filing date, not calendar quarter.

**Implementation notes (Phase 2, 2026-10-21):**
- SEC's `fy`/`fp` on a companyfacts value describe the *filing*, not the value's own period (10-Ks repeat prior-year comparatives with the current `fy`/`fp`). Periods are therefore classified by length in days: Q 80–100, 6M YTD 172–193, 9M YTD 263–285, FY 350–380 (covers 52/53-week years).
- Tag mapping lives in `config/domain/finance/metric_mapping.yaml` (25 metrics). Curated rows carry `mapping_version`; a mapping change is a new version and never collides with existing rows.
- A derived quarter takes the filed date and accession of its *later* input and lists both inputs in `derivation`; it is re-derived when an input is restated, and never produced once the quarter is reported directly.
- Every filing that reports a period is kept as a version (comparatives included); "restated" = value differs from the previous version.
- Scale errors (#6) are flagged only when a value is 500–1500x off neighbors that agree with each other (spread ≤ 10x), so restatements after reverse mergers are not mistaken for scale slips.
- `staging.facts` in Postgres holds only facts whose tag feeds a canonical metric (storage budget); the full raw set stays in Parquet.

---

## 5. Database (Supabase Postgres)

Schemas, in data-flow order:

**`raw`** — append-only landing, minimal parsing
- `raw.ingest_runs` (id, source, started_at, finished_at, status, counts jsonb, error)
- `raw.submissions_index` / `raw.companyfacts_index` (cik, storage path to Parquet, checksum, fetched_at)

**`staging`** — typed, cleaned, not yet canonical
- `staging.facts` (cik, taxonomy, tag, unit, value, period_start, period_end, fy, fp, form, accession, filed_date)
- `staging.filings` (cik, accession, form_type, filed_date, report_date, primary_document, items text[])
- `staging.quarantine` (id, source, record jsonb, rule_violated, run_id, created_at)

**`curated`** — canonical domain model
- `curated.company` (cik PK, name, tickers text[], sic, sic_description, state, fiscal_year_end, is_excluded, exclusion_reason)
- `curated.company_alias` (cik, alias, alias_type: former_name | ticker | tenant_provided)
- `curated.financial_metric` (cik, canonical_metric, period_end, period_type: Q | FY, value, source_tag, is_derived, filed_date, accession, version)
- `curated.filing_event` (cik, accession, filed_date, event_type, raw_items)
- `curated.text_signal` (cik, accession, section, signal_type, value, evidence_quote, confidence, prompt_version, model)
- `curated.filing_chunk` (id, cik, accession, section, chunk_text, embedding vector)
- `curated.macro_series` (series_id, date, release_date, value)

**`ml`**
- `ml.feature_snapshot` (cik, as_of_date, feature_set_version, features jsonb)
- `ml.label` (cik, as_of_date, horizon_months, label, label_source_accession)
- `ml.model_version` (id, algo, feature_set_version, train_window, metrics jsonb, artifact_path, is_active, created_at)
- `ml.prediction` (cik, as_of_date, model_version_id, probability, risk_tier, top_drivers jsonb)
- `ml.drift_report` (model_version_id, date, feature, psi, flagged)

**`app`** — tenant-facing; ALL tables have RLS by tenant_id
- `app.tenant`, `app.membership` (user_id, tenant_id, role)
- `app.portfolio` (tenant_id, name)
- `app.portfolio_borrower` (portfolio_id, cik nullable, uploaded_name, uploaded_ticker, match_status, match_confidence)
- `app.match_review` (borrower_id, candidate_ciks jsonb, decision, decided_by, decided_at)
- `app.alert` (tenant_id, cik, triggered_at, reason, prediction_id, status)
- `app.disposition` (alert_id, user_id, decision: agree | dismiss, reason, created_at)
- `app.audit_log`

**Storage budget:** Raw data and the wide training feature set stay as Parquet in Supabase Storage. Postgres holds curated data for the served universe, predictions, and app data only.

---

## 6. Labels

- **Positive event:** 8-K with Item 1.03 (bankruptcy/receivership). Secondary (configurable, off by default): delisting notice Item 3.01; going-concern language detected in the 10-K auditor report (text signal, confidence ≥ threshold).
- **Label for (cik, T):** 1 if a positive event is filed within (T, T + 12 months].
- **Training universe:** All non-excluded XBRL filers 2010–present (wide universe for enough positives). The served watchlist = tenant portfolios only.
- **Class imbalance is severe.** Handle with class weights; never random oversampling across time.
- **Implemented (Phase 4, 2026-10-26):** each Item 1.03 8-K is classified from its text (own petition, receivership, subsidiary-only, sister-company joint filing, follow-up, mis-tag, unclear) by rules in `config/domain/finance/labels.yaml`; petitions and follow-ups are grouped into bankruptcy cases (a confirmation ends a case; a confirmation with no petition on file starts an inferred case). label(cik, T) = 1 if a case starts in (T, T+12m]; rows inside an open case are excluded. Hand review of a fresh sample estimates ≈ 88% label precision.

---

## 7. Features (versioned as `feature_set_version`)

- **Ratios:** Altman Z components, debt/EBITDA, interest coverage, current ratio, quick ratio, cash/total assets, debt/assets, FCF margin, cash runway (cash ÷ trailing burn).
- **Trends:** QoQ and YoY deltas; 4-quarter slopes for revenue, margin, cash, debt.
- **Filing behavior:** late filings (NT 10-K/NT 10-Q count, trailing 24m), auditor change, exec departures, 4.02 non-reliance, 8-K frequency spike, days from period end to filing (lengthening = bad).
- **Text signals (LLM):** liquidity concern, covenant breach/waiver, going-concern mention, refinancing risk, customer concentration — each with evidence quote + confidence.
- **Macro:** HY spread, rate level and change at T.
- **Missingness indicators** (a missing metric is itself informative).

---

## 8. Modeling

- **Splits:** Strictly time-based (e.g., train ≤ 2019, validate 2020–2021, test 2022+). Also run a rolling-origin backtest.
  *(Phase 4: the year ranges are read as label-window years, so as-of dates are train 2011–2018, validation 2020, test Dec 2021–Sep 2025; as-of dates in 2019 and early 2021 are left out so label windows never overlap. Split dates live in `config/models.yaml`.)*
- **Models:** (a) Altman Z-score rule baseline, (b) logistic regression, (c) LightGBM. Report all three side by side. *(Phase 4: with book values only, the baseline is Altman Z'' (book-equity variant). SHAP uses LightGBM's exact TreeSHAP contributions.)*
- **Metrics:** PR-AUC (primary), recall@top-5% and top-10% of the risk ranking, Brier score + calibration curve, median lead time (months before the event that the company first entered the top tier).
- **Calibration:** isotonic or Platt on the validation set, so probabilities are meaningful.
- **Explanations:** SHAP top-5 drivers per prediction, translated to plain English ("Interest coverage fell from 3.1x to 0.8x").
- **Model card:** auto-generated per model version — data window, metrics, known limitations, excluded sectors.
- **Promotion:** A new model becomes `is_active` only if it beats the current one on test PR-AUC without worse calibration.

---

## 9. LLM layer

- **Extraction:** For MD&A, Risk Factors, Liquidity sections, and the auditor report: chunk → Claude with a strict JSON schema → `curated.text_signal`. Every signal must include a verbatim evidence quote and source accession; reject outputs whose quote is not found in the source text.
- **Model choice:** Haiku 5.5 by default. Run the eval set against Haiku and Sonnet; upgrade only signal types where Haiku is meaningfully worse. Publish this comparison (accuracy vs cost) in the README.
- **Eval:** Hand-label ~100 passages → `evals/text_signals.jsonl`; report precision/recall per signal type per prompt_version and model.
- **Q&A:** RAG over `curated.filing_chunk` scoped to one company, using Sonnet 5.5; answers must cite accession + section; refuse if retrieval confidence is low.
- **Cost control:** Cache by (accession, section, prompt_version, model); only process filings for companies in the training sample or tenant portfolios.

---

## 10. Data quality & observability

- Pandera contracts at each layer boundary; violations → `staging.quarantine`, never silent drops.
- Every pipeline run writes to `raw.ingest_runs` with row counts in/out per step.
- **Data Health page:** freshness per source, quarantine counts by rule, % of companies with complete core metrics, tag-mapping coverage.
- **Drift:** weekly PSI per feature vs training distribution; score distribution shift; flagged in UI.
- Structured JSON logging across all services.

---

## 11. API (FastAPI on Railway)

Auth: verify the Supabase JWT and enforce tenant scope server-side (in addition to RLS).

- `POST /portfolios/{id}/upload` — CSV → entity resolution job
- `GET /portfolios/{id}/matches` · `POST /matches/{id}/decision`
- `GET /portfolios/{id}/watchlist?tier=&sort=`
- `GET /companies/{cik}` — profile, metric timeline, events, signals, prediction history
- `GET /companies/{cik}/explain` — SHAP drivers in plain English
- `POST /companies/{cik}/ask` — RAG Q&A with citations
- `POST /alerts/{id}/disposition`
- `GET /health/data` · `GET /models/active` (model card)

---

## 12. Frontend (Next.js on Vercel)

1. **Login / tenant switcher**
2. **Portfolio upload + match review:** uploaded rows, proposed match, confidence, accept / reject / search
3. **Watchlist:** ranked table, tier badges, score sparkline, top driver, filters
4. **Company detail:** score history chart, driver panel, financial metrics timeline (with "derived" and "restated" markers), event timeline, text signals with evidence quotes, "Ask the filings" panel
5. **Alerts inbox** with disposition actions
6. **Data Health** and **Model Card** pages (these impress FDE interviewers; don't skip them)

---

## 13. Repo layout

```
Covenant---Credit-early-warning-platform/
  SPEC.md
  README.md
  .env.example
  apps/web/                 # Next.js (Vercel)
  services/
    ingest/                 # SEC + FRED clients, rate limiter, raw landing
    pipeline/               # staging → curated transforms, tag mapping, ER
    ml/                     # features, labels, training, eval, explain
    llm/                    # extraction, prompts/, RAG
    api/                    # FastAPI
  packages/common/          # config, logging, db client, Pandera schemas
  supabase/migrations/      # SQL migrations + RLS policies
  config/
    domain/finance/         # metric_mapping.yaml, event_codes.yaml, exclusions.yaml, labels.yaml
    models.yaml             # LLM + ML model settings
  evals/
  notebooks/                # exploration only; no logic lives here
  tests/
  .github/workflows/ci.yml
```

---

## 14. Build phases

**Phase 0 — Foundations.** Monorepo, config/env handling, logging, Supabase migrations for all schemas, RLS policies, CI (lint, type check, tests).
✅ Migrations apply cleanly; an RLS test proves tenant A cannot read tenant B's rows.

**Phase 1 — Ingestion.** Rate-limited SEC client; ticker map + submissions (incl. paginated `filings.files`) + companyfacts for the ~200-company dev sample (incl. ~20 known bankruptcies); raw Parquet in Storage; FRED pull.
✅ Re-running is idempotent; run log shows counts; rate limits respected; 1.03 scan spot-checked.

**Phase 2 — Cleaning & canonical model.** Staging parse, metric mapping (start with ~25 core metrics), YTD→quarterly derivation, restatement versioning, quarantine rules, SIC exclusions, filing_event extraction from 8-K item codes and NT forms.
✅ Point-in-time query returns correct "as known on date T" values; unit tests for YTD derivation and tag fallback; coverage report generated.

**Phase 3 — Entity resolution.** Upload flow, matching cascade, confidence scoring, review queue, decision reuse.
✅ On a deliberately messy 100-row test CSV (typos, old names, missing tickers), ≥90% auto-match precision and all low-confidence rows routed to review.

**Phase 4 — Features, labels, models.** Feature snapshots at quarterly as-of dates, labels, three models, time-based eval, calibration, SHAP, model card, model registry. Scale from the dev sample to the full training universe via bulk files.
✅ Leakage test passes; LightGBM beats the Altman baseline on test PR-AUC; eval report committed.

**Phase 5 — LLM signals + RAG.** Section extraction, structured signal extraction with evidence verification, Haiku vs Sonnet eval, embeddings, Q&A endpoint.
✅ Signal eval report per prompt version and model; every Q&A answer has a valid citation.

**Phase 6 — API + App.** All endpoints and screens above.
✅ End-to-end demo: upload CSV → review matches → watchlist → drill into a company that later went bankrupt → see it flagged months early with drivers and evidence.

**Phase 7 — Production hardening.** Railway cron for ingestion + weekly scoring, drift reports, alerts on new high-risk entries, Data Health page, API load test, README with architecture diagram, design decisions, known limitations.
✅ System runs unattended for a week with fresh data and correct alerts.

---

## 15. Rules for Claude Code

- Work one phase at a time. At each checkpoint, summarize what was built, what's next, and any open questions.
- Write tests alongside code; never mark a phase done with failing tests.
- No business logic in notebooks.
- Never overwrite raw or historical data; always append with versioning.
- Every derived number must be traceable to source accession + tag.
- Secrets only via env vars; keep `.env.example` current.
- Prefer boring, explicit code over clever abstractions.
- Keep finance-specific logic in `config/domain/finance/` so another domain pack could be added later.
- When a data source behaves differently than this spec says (URL moved, field missing), stop, report it, and update the spec rather than silently working around it.

### Model usage (for the human driving Claude Code)
- **Opus 5.5:** phase planning, data model, point-in-time logic, YTD derivation, restatements, leakage test, ML eval design, hard debugging, and an end-of-phase review ("review this phase against SPEC.md for correctness and leakage bugs").
- **Sonnet 5.5:** API endpoints, Next.js screens, migrations, test scaffolding, CI, docs.

### Kickoff prompt
> Read SPEC.md. Propose your plan for Phase 0, then build it. Stop at the Phase 0 acceptance criteria.
