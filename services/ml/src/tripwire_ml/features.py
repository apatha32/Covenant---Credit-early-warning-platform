"""Point-in-time features for the finance domain (feature_set_version v1).

`features_as_of(inputs, T)` is the only way features are computed, for training and scoring
alike. It first restricts every input to what was public on T (metrics by filed_date, filings
by filing date, macro by release date), then derives everything from that. The leakage test
recomputes features after deleting all later data and requires identical output.

Ratios use book values only (no market prices): Altman Z'' is the book-equity variant
(Z'' = 6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import polars as pl

from tripwire_ml.macro import macro_features

FEATURE_SET_VERSION = "v1"

INSTANT_METRICS = [
    "cash",
    "short_term_investments",
    "accounts_receivable",
    "inventory",
    "current_assets",
    "total_assets",
    "accounts_payable",
    "current_liabilities",
    "total_liabilities",
    "short_term_debt",
    "long_term_debt",
    "retained_earnings",
    "stockholders_equity",
]
FLOW_METRICS = [
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "sga_expense",
    "operating_income",
    "interest_expense",
    "pretax_income",
    "income_tax_expense",
    "net_income",
    "depreciation_amortization",
    "operating_cash_flow",
    "capex",
]

REPORT_FORMS = ("10-K", "10-K/A", "10-K405", "10-KT", "10-KT/A", "10-Q", "10-Q/A")
ACTIVE_FILER_DAYS = 460  # a 10-K / 10-Q filed in the last ~15 months
FRESH_BALANCE_SHEET_DAYS = 550
YEAR_AGO = (330, 400)  # a "year-ago" period ends this many days before the latest one
DAY = dt.timedelta(days=1)

# Feature name -> (plain-English label, display format). Drives explanations and the model card.
FEATURES: dict[str, tuple[str, str]] = {
    "log_total_assets": ("Total assets", "log_usd"),
    "log_revenue_ttm": ("Trailing-12-month revenue", "log_usd"),
    "altman_z2": ("Altman Z'' score", "num"),
    "wc_ta": ("Working capital / total assets", "ratio"),
    "re_ta": ("Retained earnings / total assets", "ratio"),
    "ebit_ta": ("EBIT / total assets", "ratio"),
    "bookeq_tl": ("Book equity / total liabilities", "ratio"),
    "debt_assets": ("Debt / total assets", "ratio"),
    "liabilities_assets": ("Total liabilities / total assets", "ratio"),
    "debt_ebitda": ("Debt / EBITDA", "x"),
    "ebitda_nonpositive": ("EBITDA is zero or negative", "flag"),
    "interest_coverage": ("Interest coverage (EBIT / interest)", "x"),
    "current_ratio": ("Current ratio", "x"),
    "quick_ratio": ("Quick ratio", "x"),
    "cash_assets": ("Cash / total assets", "ratio"),
    "cash_runway_q": ("Cash runway (quarters of free-cash-flow burn)", "num"),
    "net_margin": ("Net margin", "pct"),
    "op_margin": ("Operating margin", "pct"),
    "gross_margin": ("Gross margin", "pct"),
    "fcf_margin": ("Free-cash-flow margin", "pct"),
    "roa": ("Return on assets", "pct"),
    "revenue_yoy": ("Revenue growth, year over year", "pct"),
    "op_margin_chg": ("Change in operating margin, year over year", "pct"),
    "cash_yoy": ("Change in cash, year over year", "pct"),
    "debt_yoy": ("Change in debt, year over year", "pct"),
    "total_assets_yoy": ("Change in total assets, year over year", "pct"),
    "equity_chg_ta": ("Change in book equity / total assets, year over year", "ratio"),
    "revenue_slope_4q": ("Revenue trend, last 4 quarters", "ratio"),
    "cash_slope_4q": ("Cash trend, last 4 quarters", "ratio"),
    "debt_slope_4q": ("Debt trend, last 4 quarters", "ratio"),
    "negative_equity": ("Book equity is negative", "flag"),
    "accumulated_deficit": ("Accumulated deficit (negative retained earnings)", "flag"),
    "staleness_days": ("Days since the latest balance sheet date", "days"),
    "late_filings_24m": ("Late-filing notices (NT 10-K / NT 10-Q), last 24 months", "count"),
    "auditor_changes_24m": ("Auditor changes, last 24 months", "count"),
    "non_reliance_24m": ("Non-reliance on prior financials (8-K 4.02), last 24 months", "count"),
    "exec_departures_12m": ("Director / officer departures (8-K 5.02), last 12 months", "count"),
    "delisting_notices_24m": ("Delisting notices (8-K 3.01), last 24 months", "count"),
    "eightk_spike": ("8-K filing pace, last 3 months vs. the past year", "x"),
    "filing_lag_days": ("Days from period end to filing (latest 10-K / 10-Q)", "days"),
    "filing_lag_chg": ("Change in filing lag vs. a year earlier", "days"),
    "days_since_report": ("Days since the latest 10-K / 10-Q", "days"),
    "missing_revenue": ("No revenue reported", "flag"),
    "missing_interest": ("No interest expense reported", "flag"),
    "missing_debt": ("No debt figures reported", "flag"),
    "missing_current_split": ("No current assets / liabilities split", "flag"),
    "macro_baa10y": ("Baa corporate credit spread", "num"),
    "macro_baa10y_chg_3m": ("Change in Baa credit spread, 3 months", "num"),
    "macro_dgs10": ("10-year Treasury yield", "num"),
    "macro_dgs10_chg_12m": ("Change in 10-year Treasury yield, 12 months", "num"),
    "macro_fedfunds": ("Fed funds rate", "num"),
    "macro_fedfunds_chg_12m": ("Change in fed funds rate, 12 months", "num"),
    "macro_unrate": ("Unemployment rate", "num"),
    "macro_unrate_chg_12m": ("Change in unemployment rate, 12 months", "num"),
    "macro_indpro_yoy": ("Industrial production growth, year over year", "pct"),
}
FEATURE_COLUMNS = list(FEATURES)


@dataclass(frozen=True)
class Inputs:
    """Everything features may read. Each frame is filtered to the as-of date inside
    features_as_of; callers never pre-filter (the leakage test relies on that)."""

    metrics: pl.LazyFrame  # cik, metric, period_type, period_end, value, filed_date, version
    filings: pl.LazyFrame  # prepare_filings() output
    macro: pl.DataFrame  # series_id, observation_date, release_date, value
    universe: frozenset[str]  # CIKs in scope (sector exclusions already applied)


def prepare_filings(raw: pl.LazyFrame) -> pl.LazyFrame:
    """Typed filing dates and event flags, computed once (not per as-of date)."""
    items = pl.col("items").fill_null("").str.split(",").list.eval(pl.element().str.strip_chars())
    is_8k = pl.col("form").is_in(["8-K", "8-K/A"])
    return raw.select(
        "cik",
        "form",
        filed=pl.col("filingDate").str.to_date(strict=False),
        report=pl.col("reportDate").str.to_date(strict=False),
        is_report=pl.col("form").is_in(REPORT_FORMS),
        is_late_notice=pl.col("form").str.starts_with("NT 10"),
        is_8k=is_8k,
        auditor_change=is_8k & items.list.contains("4.01"),
        non_reliance=is_8k & items.list.contains("4.02"),
        exec_departure=is_8k & items.list.contains("5.02"),
        delisting=is_8k & items.list.contains("3.01"),
    )


# --- helpers ---


def _div(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.when(b.is_not_null() & (b != 0)).then(a / b)


def _pos_div(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    return pl.when(b.is_not_null() & (b > 0)).then(a / b)


def _growth(now: pl.Expr, then: pl.Expr) -> pl.Expr:
    return pl.when(then.is_not_null() & (then > 0)).then((now / then - 1).clip(-1, 10))


def _slope4(values: pl.Expr) -> pl.Expr:
    """Least-squares slope of the last 4 values, per step, relative to their mean size."""
    y = [values.list.get(i, null_on_oob=True) for i in range(4)]
    slope = (-1.5 * y[0] - 0.5 * y[1] + 0.5 * y[2] + 1.5 * y[3]) / 5
    scale = (y[0].abs() + y[1].abs() + y[2].abs() + y[3].abs()) / 4
    return pl.when((values.list.len() == 4) & (scale > 0)).then((slope / scale).clip(-2, 2))


def _year_ago(col: str) -> pl.Expr:
    last = pl.col("period_end").last()
    window = pl.col("period_end").is_between(last - YEAR_AGO[1] * DAY, last - YEAR_AGO[0] * DAY)
    return pl.col(col).filter(window).last()


def latest_known(metrics: pl.LazyFrame, as_of: dt.date) -> pl.DataFrame:
    """Each (company, metric, period) as known on `as_of`: its latest version filed by then."""
    return (
        metrics.filter(pl.col("filed_date") <= as_of)
        .sort("filed_date", "version")
        .group_by("cik", "metric", "period_type", "period_end")
        .agg(pl.col("value").last(), pl.col("filed_date").last())
        .collect()
    )


def _instant_block(known: pl.DataFrame) -> pl.DataFrame:
    g = (
        known.filter(pl.col("metric").is_in(INSTANT_METRICS))
        .sort("period_end")
        .group_by("cik", "metric")
        .agg(
            cur=pl.col("value").last(),
            cur_end=pl.col("period_end").last(),
            yago=_year_ago("value"),
            last4=pl.col("value").tail(4),
        )
    )
    out = g.select("cik").unique()
    for metric in INSTANT_METRICS:
        part = g.filter(pl.col("metric") == metric).select(
            "cik",
            pl.col("cur").alias(metric),
            pl.col("cur_end").alias(f"{metric}_end"),
            pl.col("yago").alias(f"{metric}_yago"),
            pl.col("last4").alias(f"{metric}_last4"),
        )
        out = out.join(part, on="cik", how="left")
    return out


def _flow_block(known: pl.DataFrame) -> pl.DataFrame:
    flows = known.filter(pl.col("metric").is_in(FLOW_METRICS))
    q = (
        flows.filter(pl.col("period_type") == "Q")
        .sort("period_end")
        .group_by("cik", "metric")
        .agg(vals=pl.col("value").tail(8), ends=pl.col("period_end").tail(8))
    )
    span = (
        pl.col("ends").list.get(-1, null_on_oob=True)
        - pl.col("ends").list.get(-4, null_on_oob=True)
    ).dt.total_days()
    prev_span = (
        pl.col("ends").list.get(-5, null_on_oob=True)
        - pl.col("ends").list.get(-8, null_on_oob=True)
    ).dt.total_days()
    q = q.with_columns(
        q_ttm=pl.when((pl.col("vals").list.len() >= 4) & span.is_between(250, 300)).then(
            pl.col("vals").list.tail(4).list.sum()
        ),
        q_ttm_prev=pl.when((pl.col("vals").list.len() == 8) & prev_span.is_between(250, 300)).then(
            pl.col("vals").list.head(4).list.sum()
        ),
        q_end=pl.col("ends").list.get(-1, null_on_oob=True),
        q_last4=pl.col("vals").list.tail(4),
    )
    fy = (
        flows.filter(pl.col("period_type") == "FY")
        .sort("period_end")
        .group_by("cik", "metric")
        .agg(
            fy=pl.col("value").last(),
            fy_end=pl.col("period_end").last(),
            fy_prev=_year_ago("value"),
        )
    )
    both = q.join(fy, on=["cik", "metric"], how="full", coalesce=True)
    # Trailing 12 months: the latest fiscal year if it is at least as recent as the latest
    # quarter (annual-only filers, or right after a 10-K), else the last 4 quarters.
    use_fy = pl.col("fy_end").is_not_null() & (
        pl.col("q_end").is_null() | (pl.col("fy_end") >= pl.col("q_end"))
    )
    both = both.with_columns(
        ttm=pl.when(use_fy).then(pl.col("fy")).otherwise(pl.col("q_ttm")),
        ttm_prev=pl.when(use_fy).then(pl.col("fy_prev")).otherwise(pl.col("q_ttm_prev")),
    )
    out = both.select("cik").unique()
    for metric in FLOW_METRICS:
        part = both.filter(pl.col("metric") == metric).select(
            "cik",
            pl.col("ttm").alias(f"{metric}_ttm"),
            pl.col("ttm_prev").alias(f"{metric}_ttm_prev"),
            pl.col("q_last4").alias(f"{metric}_q_last4"),
        )
        out = out.join(part, on="cik", how="left")
    return out


def _filing_block(filings: pl.LazyFrame, as_of: dt.date) -> pl.DataFrame:
    known = filings.filter(pl.col("filed") <= as_of)

    def since(days: int) -> pl.Expr:
        return pl.col("filed") > as_of - days * DAY

    counts = (
        known.filter(since(730))
        .group_by("cik")
        .agg(
            late_filings_24m=(pl.col("is_late_notice")).sum(),
            auditor_changes_24m=pl.col("auditor_change").sum(),
            non_reliance_24m=pl.col("non_reliance").sum(),
            delisting_notices_24m=pl.col("delisting").sum(),
            exec_departures_12m=(pl.col("exec_departure") & since(365)).sum(),
            eightk_3m=(pl.col("is_8k") & since(91)).sum(),
            eightk_12m=(pl.col("is_8k") & since(365)).sum(),
        )
    )
    reports = known.filter(pl.col("is_report") & pl.col("report").is_not_null()).sort("filed")
    lag = (pl.col("filed") - pl.col("report")).dt.total_days()
    latest = reports.group_by("cik").agg(
        last_report=pl.col("filed").last(),
        filing_lag_days=lag.last(),
        last_form=pl.col("form").last(),
    )
    prior = (
        reports.filter(pl.col("filed").is_between(as_of - 450 * DAY, as_of - 280 * DAY))
        .group_by("cik", "form")
        .agg(prior_lag=lag.last())
    )
    latest = latest.join(prior, left_on=["cik", "last_form"], right_on=["cik", "form"], how="left")
    return (
        latest.join(counts, on="cik", how="left")
        .with_columns(
            days_since_report=(pl.lit(as_of) - pl.col("last_report")).dt.total_days(),
            filing_lag_chg=pl.col("filing_lag_days") - pl.col("prior_lag"),
            eightk_spike=pl.col("eightk_3m") / (pl.col("eightk_12m") / 4 + 1),
        )
        .collect()
    )


def features_as_of(inputs: Inputs, as_of: dt.date) -> pl.DataFrame:
    """One row per in-scope, active company with a recent balance sheet, as known on `as_of`."""
    known = latest_known(inputs.metrics, as_of)
    known = known.filter(pl.col("cik").is_in(list(inputs.universe)))
    source_dates = known.group_by("cik").agg(max_source_filed_date=pl.col("filed_date").max())
    filing = _filing_block(inputs.filings, as_of)
    wide = (
        _instant_block(known)
        .join(_flow_block(known), on="cik", how="left")
        .join(filing, on="cik", how="inner")
        .join(source_dates, on="cik", how="left")
        .filter(
            (pl.col("last_report") > as_of - ACTIVE_FILER_DAYS * DAY)
            & pl.col("total_assets").is_not_null()
            & (pl.col("total_assets_end") >= as_of - FRESH_BALANCE_SHEET_DAYS * DAY)
        )
    )
    c = pl.col
    debt_parts = pl.concat_list(c("short_term_debt"), c("long_term_debt")).list.drop_nulls()
    debt = pl.when(debt_parts.list.len() > 0).then(debt_parts.list.sum())
    debt_yago_parts = pl.concat_list(
        c("short_term_debt_yago"), c("long_term_debt_yago")
    ).list.drop_nulls()
    debt_yago = pl.when(debt_yago_parts.list.len() > 0).then(debt_yago_parts.list.sum())
    ebit = pl.coalesce(
        c("operating_income_ttm"), c("pretax_income_ttm") + c("interest_expense_ttm").fill_null(0)
    )
    ebitda = ebit + c("depreciation_amortization_ttm").fill_null(0)
    fcf = c("operating_cash_flow_ttm") - c("capex_ttm").fill_null(0)
    liquid = c("cash").fill_null(0) + c("short_term_investments").fill_null(0)
    ta, tl, rev = c("total_assets"), c("total_liabilities"), c("revenue_ttm")
    wide = wide.with_columns(debt=debt, debt_yago=debt_yago, ebit=ebit, ebitda=ebitda, fcf=fcf)
    c = pl.col
    wc_ta = _pos_div(c("current_assets") - c("current_liabilities"), ta)
    re_ta = _pos_div(c("retained_earnings"), ta)
    ebit_ta = _pos_div(c("ebit"), ta)
    bookeq_tl = _pos_div(c("stockholders_equity"), tl).clip(-50, 50)
    burn = pl.when(c("fcf") < 0).then(-c("fcf") / 4)
    debt_series = pl.concat_list(c("short_term_debt_last4"), c("long_term_debt_last4"))
    result = wide.with_columns(
        log_total_assets=ta.clip(1, None).log10(),
        log_revenue_ttm=pl.when(rev > 0).then(rev.log10()),
        wc_ta=wc_ta.clip(-10, 10),
        re_ta=re_ta.clip(-50, 10),
        ebit_ta=ebit_ta.clip(-10, 10),
        bookeq_tl=bookeq_tl,
        altman_z2=(6.56 * wc_ta + 3.26 * re_ta + 6.72 * ebit_ta + 1.05 * bookeq_tl).clip(-100, 100),
        debt_assets=_pos_div(c("debt"), ta).clip(0, 20),
        liabilities_assets=_pos_div(tl, ta).clip(0, 20),
        debt_ebitda=pl.when(c("ebitda") > 0).then((c("debt") / c("ebitda")).clip(0, 100)),
        ebitda_nonpositive=pl.when(c("ebitda").is_not_null()).then(
            (c("ebitda") <= 0).cast(pl.Int8)
        ),
        interest_coverage=_pos_div(c("ebit"), c("interest_expense_ttm")).clip(-50, 50),
        current_ratio=_pos_div(c("current_assets"), c("current_liabilities")).clip(0, 50),
        quick_ratio=_pos_div(
            liquid + c("accounts_receivable").fill_null(0), c("current_liabilities")
        ).clip(0, 50),
        cash_assets=_pos_div(liquid, ta).clip(0, 1),
        cash_runway_q=pl.when(c("fcf").is_null())
        .then(None)
        .when(c("fcf") >= 0)
        .then(40.0)
        .otherwise((liquid / burn).clip(0, 40)),
        net_margin=_pos_div(c("net_income_ttm"), rev).clip(-10, 1),
        op_margin=_pos_div(c("ebit"), rev).clip(-10, 1),
        gross_margin=_pos_div(c("gross_profit_ttm"), rev).clip(-10, 1),
        fcf_margin=_pos_div(c("fcf"), rev).clip(-10, 1),
        roa=_pos_div(c("net_income_ttm"), ta).clip(-10, 10),
        revenue_yoy=_growth(rev, c("revenue_ttm_prev")),
        op_margin_chg=(
            _pos_div(c("ebit"), rev)
            - _pos_div(c("operating_income_ttm_prev"), c("revenue_ttm_prev"))
        ).clip(-10, 10),
        cash_yoy=_growth(c("cash"), c("cash_yago")),
        debt_yoy=_growth(c("debt"), c("debt_yago")),
        total_assets_yoy=_growth(ta, c("total_assets_yago")),
        equity_chg_ta=_pos_div(c("stockholders_equity") - c("stockholders_equity_yago"), ta).clip(
            -10, 10
        ),
        revenue_slope_4q=_slope4(c("revenue_q_last4")),
        cash_slope_4q=_slope4(c("cash_last4")),
        debt_slope_4q=pl.when(c("long_term_debt_last4").is_not_null()).then(
            _slope4(c("long_term_debt_last4"))
        ),
        negative_equity=pl.when(c("stockholders_equity").is_not_null()).then(
            (c("stockholders_equity") < 0).cast(pl.Int8)
        ),
        accumulated_deficit=pl.when(c("retained_earnings").is_not_null()).then(
            (c("retained_earnings") < 0).cast(pl.Int8)
        ),
        staleness_days=(pl.lit(as_of) - c("total_assets_end")).dt.total_days(),
        missing_revenue=rev.is_null().cast(pl.Int8),
        missing_interest=c("interest_expense_ttm").is_null().cast(pl.Int8),
        missing_debt=(c("short_term_debt").is_null() & c("long_term_debt").is_null()).cast(pl.Int8),
        missing_current_split=(
            c("current_assets").is_null() | c("current_liabilities").is_null()
        ).cast(pl.Int8),
        late_filings_24m=c("late_filings_24m").fill_null(0),
        auditor_changes_24m=c("auditor_changes_24m").fill_null(0),
        non_reliance_24m=c("non_reliance_24m").fill_null(0),
        exec_departures_12m=c("exec_departures_12m").fill_null(0),
        delisting_notices_24m=c("delisting_notices_24m").fill_null(0),
        eightk_spike=c("eightk_spike").fill_null(0),
    )
    del debt_series
    for name, value in macro_features(inputs.macro, as_of).items():
        result = result.with_columns(pl.lit(value, dtype=pl.Float64).alias(name))
    return result.select(
        "cik",
        pl.lit(as_of).alias("as_of_date"),
        *[pl.col(f).cast(pl.Float64) for f in FEATURE_COLUMNS],
        "max_source_filed_date",
    ).sort("cik")


def quarter_ends(start: dt.date, end: dt.date) -> list[dt.date]:
    """Calendar quarter-end dates in [start, end]."""
    dates = []
    for year in range(start.year, end.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = dt.date(year, month, day)
            if start <= d <= end:
                dates.append(d)
    return dates
