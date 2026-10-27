import type { MetricPoint, Tier } from "@/lib/types";

/** The guided demo: Spirit Airlines, about five months before its first Chapter 11 filing. */
export const STORY = { cik: "0001498710", asOf: "2024-06-30", name: "Spirit Airlines" };

export function pct(p: number, digits = 1): string {
  return `${(p * 100).toFixed(digits)}%`;
}

export function usd(v: number): string {
  const a = Math.abs(v);
  const sign = v < 0 ? "−" : "";
  for (const [unit, size] of [
    ["T", 1e12],
    ["B", 1e9],
    ["M", 1e6],
    ["K", 1e3],
  ] as const) {
    if (a >= size) return `${sign}$${(a / size).toFixed(1)}${unit}`;
  }
  return `${sign}$${a.toFixed(0)}`;
}

const DATE = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const QUARTER_DATE = new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric", timeZone: "UTC" });

export function fmtDate(iso: string): string {
  return DATE.format(new Date(`${iso}T00:00:00Z`));
}

export function fmtMonth(iso: string): string {
  return QUARTER_DATE.format(new Date(`${iso}T00:00:00Z`));
}

export function isQuarterEnd(iso: string): boolean {
  return /-(03-31|06-30|09-30|12-31)$/.test(iso);
}

/** "Q2 2024" for quarter ends, otherwise the date (weekly refresh scores). */
export function asOfLabel(iso: string): string {
  if (!isQuarterEnd(iso)) return `${fmtDate(iso)} (weekly refresh)`;
  const [y, m] = iso.split("-");
  return `Q${Math.ceil(Number(m) / 3)} ${y} · ${fmtDate(iso)}`;
}

export function monthsBetween(fromIso: string, toIso: string): number {
  const ms = new Date(`${toIso}T00:00:00Z`).getTime() - new Date(`${fromIso}T00:00:00Z`).getTime();
  return ms / (1000 * 60 * 60 * 24 * 30.4375);
}

export function edgarFiling(cik: string, accession: string): string {
  return `https://www.sec.gov/Archives/edgar/data/${Number(cik)}/${accession.replaceAll("-", "")}/${accession}-index.htm`;
}

export function edgarCompany(cik: string): string {
  return `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${cik}&type=&dateb=&owner=include&count=40`;
}

export const TIERS: Record<Tier, { label: string; hint: string; className: string; dot: string }> = {
  high: {
    label: "High",
    hint: "Riskiest 5% of all SEC filers on this date",
    className: "bg-risk-soft text-risk border-risk/25",
    dot: "var(--risk)",
  },
  elevated: {
    label: "Elevated",
    hint: "Next 10% (top 5–15% of all SEC filers)",
    className: "bg-brand-soft text-brand border-brand/25",
    dot: "var(--brand)",
  },
  low: {
    label: "Low",
    hint: "Everyone else",
    className: "bg-ok-soft text-ok border-ok/25",
    dot: "var(--ok)",
  },
};

export const EVENT_TYPES: Record<string, { label: string; short: string }> = {
  bankruptcy: { label: "Bankruptcy or receivership (8-K 1.03)", short: "Bankruptcy" },
  auditor_change: { label: "Auditor change (8-K 4.01)", short: "Auditor change" },
  non_reliance: { label: "Prior financials not to be relied on (8-K 4.02)", short: "Non-reliance" },
  delisting_notice: { label: "Delisting notice (8-K 3.01)", short: "Delisting notice" },
  late_filing_annual: { label: "Late annual report (NT 10-K)", short: "NT 10-K" },
  late_filing_quarterly: { label: "Late quarterly report (NT 10-Q)", short: "NT 10-Q" },
  exec_departure: { label: "Director / officer change (8-K 5.02)", short: "Officer change" },
};

/** A period's value as it was known on `asOf`: the latest version if it was filed by then, else
 * the first-filed version (if that was), else not public yet. Intermediate versions are rare. */
export function knownOn(p: MetricPoint, asOf: string) {
  if (p.filed_date <= asOf)
    return { value: p.value, accession: p.accession, filed: p.filed_date, state: "latest" as const };
  if (p.original_filed_date <= asOf)
    return {
      value: p.original_value,
      accession: p.original_accession,
      filed: p.original_filed_date,
      state: "original" as const,
    };
  return { value: p.value, accession: p.accession, filed: p.filed_date, state: "future" as const };
}

export const METRICS: { key: string; label: string; kind: "flow" | "instant" }[] = [
  { key: "revenue", label: "Revenue (quarter)", kind: "flow" },
  { key: "net_income", label: "Net income (quarter)", kind: "flow" },
  { key: "operating_cash_flow", label: "Operating cash flow (quarter)", kind: "flow" },
  { key: "cash", label: "Cash", kind: "instant" },
  { key: "long_term_debt", label: "Long-term debt", kind: "instant" },
  { key: "total_liabilities", label: "Total liabilities", kind: "instant" },
];
