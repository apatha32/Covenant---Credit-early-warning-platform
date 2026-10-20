-- ml: features, labels, model registry, predictions, drift.
-- The wide training feature set lives as Parquet in Storage; feature_snapshot holds the
-- served universe only.

create table ml.feature_snapshot (
  cik                   util.cik not null references curated.company (cik),
  as_of_date            date not null,
  feature_set_version   text not null,
  features              jsonb not null,
  -- Latest filed_date of any input behind these features. The check is a last-line
  -- leakage guard; the pipeline's leakage test is the primary one.
  max_source_filed_date date,
  created_at            timestamptz not null default now(),
  check (max_source_filed_date is null or max_source_filed_date <= as_of_date),
  primary key (cik, as_of_date, feature_set_version)
);
alter table ml.feature_snapshot enable row level security;

create table ml.label (
  cik                    util.cik not null references curated.company (cik),
  as_of_date             date not null,
  horizon_months         integer not null check (horizon_months > 0),
  label_version          text not null,      -- labels.yaml version that produced it
  label                  smallint not null check (label in (0, 1)),
  label_source_accession util.accession,     -- the positive event's filing
  created_at             timestamptz not null default now(),
  check (label = 0 or label_source_accession is not null),
  primary key (cik, as_of_date, horizon_months, label_version)
);
alter table ml.label enable row level security;

create table ml.model_version (
  id                  bigint generated always as identity primary key,
  algo                text not null,         -- altman_z | logistic | lightgbm
  feature_set_version text not null,
  train_window        daterange not null,
  metrics             jsonb not null default '{}'::jsonb,
  artifact_path       text,
  model_card_path     text,
  is_active           boolean not null default false,
  created_at          timestamptz not null default now()
);
alter table ml.model_version enable row level security;
-- At most one active model.
create unique index model_version_one_active on ml.model_version (is_active) where is_active;

create table ml.prediction (
  id               bigint generated always as identity primary key,
  cik              util.cik not null references curated.company (cik),
  as_of_date       date not null,
  model_version_id bigint not null references ml.model_version (id),
  probability      numeric not null check (probability between 0 and 1),
  risk_tier        text not null,
  top_drivers      jsonb not null default '[]'::jsonb,
  created_at       timestamptz not null default now(),
  unique (cik, as_of_date, model_version_id)
);
alter table ml.prediction enable row level security;
create index on ml.prediction (model_version_id, as_of_date);
create trigger prediction_append_only before update or delete on ml.prediction
  for each row execute function util.reject_mutation();

create table ml.drift_report (
  id               bigint generated always as identity primary key,
  model_version_id bigint not null references ml.model_version (id),
  report_date      date not null,
  feature          text not null,
  psi              numeric not null check (psi >= 0),
  flagged          boolean not null,
  created_at       timestamptz not null default now(),
  unique (model_version_id, report_date, feature)
);
alter table ml.drift_report enable row level security;
