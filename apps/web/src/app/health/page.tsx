"use client";

import { Card, CardContent } from "@/components/ui/card";
import { fmtDate } from "@/lib/format";
import { useQuery } from "@/lib/supabase";

type Run = {
  source: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  counts: Record<string, unknown>;
  error: string | null;
};
type Health = {
  runs: Run[];
  quarantine: { rule: string; source: string; total: number; last_30_days: number }[];
  served: Record<string, number>;
  max_companies: number;
  latest_prediction: string | null;
  latest_filing: string | null;
  companies_with_metrics: number;
  text_signals: number;
};

const SOURCES: Record<string, string> = {
  sec_submissions: "SEC filings index",
  sec_companyfacts: "SEC financial data (XBRL)",
  fred: "FRED macro series",
  pipeline_staging: "Cleaning (staging)",
  pipeline_curated: "Canonical metrics",
  pipeline_events: "Filing events",
  ml_demo_refresh: "Weekly re-score",
  ml_serve_new: "New-borrower scoring",
  ml_demo_seed: "Initial load",
};

function ago(iso: string): string {
  const hours = (Date.now() - new Date(iso).getTime()) / 3.6e6;
  if (hours < 1) return "under an hour ago";
  if (hours < 48) return `${Math.round(hours)} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export default function HealthPage() {
  const health = useQuery<Health>((sb) => sb.rpc("data_health"), []);
  const h = health.data;
  const served = h ? Object.values(h.served).reduce((a, b) => a + b, 0) - (h.served.excluded ?? 0) : 0;
  return (
    <div className="space-y-6 pt-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Data health</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          Is the data fresh and clean? Every pipeline step logs a run with row counts; rows that break a data rule are
          set aside in quarantine (never silently dropped).
        </p>
      </div>
      {health.error && <p className="text-sm text-risk">{health.error}</p>}
      {h && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Latest score date" value={h.latest_prediction ? fmtDate(h.latest_prediction) : "—"} />
            <Stat label="Newest filing seen" value={h.latest_filing ? fmtDate(h.latest_filing) : "—"} />
            <Stat label="Companies served" value={`${served} of ${h.max_companies}`} hint="free-tier capacity" />
            <Stat label="AI text signals" value={h.text_signals.toLocaleString()} />
          </div>
          <section className="space-y-2">
            <h2 className="text-lg font-semibold">Last run of each step</h2>
            <Card className="overflow-hidden py-0">
              <div className="divide-y">
                {h.runs.map((r) => (
                  <div key={r.source} className="flex flex-wrap items-center gap-3 px-4 py-2.5 text-sm">
                    <span
                      className={
                        r.status === "succeeded" ? "text-ok" : r.status === "running" ? "text-brand" : "text-risk"
                      }
                    >
                      ●
                    </span>
                    <span className="mr-auto font-medium">{SOURCES[r.source] ?? r.source}</span>
                    <span className="text-muted-foreground">{r.status}</span>
                    <span className="w-36 text-right text-muted-foreground" title={r.started_at}>
                      {ago(r.started_at)}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          </section>
          <section className="space-y-2">
            <h2 className="text-lg font-semibold">Quarantine by rule</h2>
            <Card className="overflow-hidden py-0">
              <div className="divide-y">
                {h.quarantine.length === 0 && <p className="p-4 text-sm text-muted-foreground">Nothing quarantined.</p>}
                {h.quarantine.map((q) => (
                  <div key={`${q.source}-${q.rule}`} className="flex flex-wrap items-center gap-3 px-4 py-2.5 text-sm">
                    <span className="mr-auto">
                      <code className="text-xs">{q.rule}</code>
                      <span className="ml-2 text-xs text-muted-foreground">{q.source}</span>
                    </span>
                    <span className="tabular-nums">{q.total.toLocaleString()} rows</span>
                    <span className="w-32 text-right text-xs text-muted-foreground">{q.last_30_days} in 30 days</span>
                  </div>
                ))}
              </div>
            </Card>
          </section>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card className="py-0">
      <CardContent className="p-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-1 text-xl font-bold tabular-nums">{value}</div>
        {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  );
}
