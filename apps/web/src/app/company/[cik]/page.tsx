"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo } from "react";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { EventTimeline } from "@/components/event-timeline";
import { MetricTimeline } from "@/components/metric-timeline";
import { TierBadge } from "@/components/risk";
import { ScoreChart } from "@/components/score-chart";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  STORY,
  asOfLabel,
  edgarCompany,
  edgarFiling,
  fmtDate,
  knownOn,
  METRICS,
  monthsBetween,
  pct,
} from "@/lib/format";
import { useQuery } from "@/lib/supabase";
import type { Company, FilingEvent, MetricPoint, ScorePoint } from "@/lib/types";

export default function CompanyPage() {
  return (
    <Suspense fallback={<p className="py-16 text-center text-sm text-muted-foreground">Loading…</p>}>
      <CompanyDetail />
    </Suspense>
  );
}

function CompanyDetail() {
  const { cik } = useParams<{ cik: string }>();
  const params = useSearchParams();
  const router = useRouter();

  const company = useQuery<Company>((sb) => sb.from("company").select("*").eq("cik", cik).maybeSingle(), [cik]);
  const scores = useQuery<ScorePoint[]>(
    (sb) =>
      sb
        .from("score_history")
        .select("as_of_date,probability,risk_tier,top_drivers")
        .eq("cik", cik)
        .order("as_of_date"),
    [cik],
  );
  const metrics = useQuery<MetricPoint[]>(
    (sb) =>
      sb
        .from("metric_history")
        .select("*")
        .eq("cik", cik)
        .in(
          "metric",
          METRICS.map((m) => m.key),
        )
        .gte("period_end", "2014-01-01")
        .order("period_end")
        .limit(1000),
    [cik],
  );
  const events = useQuery<FilingEvent[]>(
    (sb) => sb.from("filing_events").select("*").eq("cik", cik).order("filed_date", { ascending: false }).limit(1000),
    [cik],
  );

  const history = useMemo(() => scores.data ?? [], [scores.data]);
  const requested = params.get("as_of");
  // The requested date, else the latest scored date on or before it, else the latest one.
  const current =
    history.find((s) => s.as_of_date === requested) ??
    [...history].reverse().find((s) => !requested || s.as_of_date <= requested) ??
    history[history.length - 1];
  const asOf = current?.as_of_date ?? requested ?? STORY.asOf;

  // The newest filing whose data the score on `asOf` could use.
  const source = useMemo(() => {
    const known = (metrics.data ?? []).map((m) => knownOn(m, asOf)).filter((k) => k.state !== "future");
    return known.reduce<(typeof known)[number] | null>((a, b) => (!a || b.filed > a.filed ? b : a), null);
  }, [metrics.data, asOf]);

  if (company.error) return <p className="pt-10 text-sm text-risk">Could not load this company: {company.error}</p>;
  if (!company.loading && !company.data)
    return <p className="pt-10 text-sm text-muted-foreground">This company is not in the demo portfolio.</p>;

  const c = company.data;
  const nextCase = c?.bankruptcy_cases.find((k) => k.case_start > asOf);

  return (
    <div className="space-y-5 pt-6">
      <Link
        href={`/watchlist?as_of=${asOf}`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" /> Watchlist as of {fmtDate(asOf)}
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{c?.name ?? "…"}</h1>
          {c && (
            <p className="mt-1 text-sm text-muted-foreground">
              {[c.tickers?.join(", "), c.sector, c.sic_description, c.state].filter(Boolean).join(" · ")}
              {" · "}
              <a
                className="inline-flex items-center gap-0.5 text-brand hover:underline"
                href={edgarCompany(c.cik)}
                target="_blank"
                rel="noreferrer"
              >
                SEC name: {c.sec_name}, CIK {Number(c.cik)} <ExternalLink className="size-3" />
              </a>
            </p>
          )}
        </div>
        {current && (
          <Card className="min-w-64 py-0">
            <CardContent className="p-4">
              <div className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                As of {asOfLabel(asOf)}
              </div>
              <div className="mt-1 flex items-center gap-3">
                <span className="text-3xl font-bold tabular-nums">{pct(current.probability)}</span>
                <TierBadge tier={current.risk_tier} />
              </div>
              <div className="text-xs text-muted-foreground">12-month bankruptcy probability</div>
              {nextCase && (
                <div className="mt-2 text-sm text-risk">
                  Filed for bankruptcy {fmtDate(nextCase.case_start)},{" "}
                  {monthsBetween(asOf, nextCase.case_start).toFixed(1)} months later{" "}
                  <span className="text-muted-foreground">(hindsight)</span>
                </div>
              )}
            </CardContent>
          </Card>
        )}
      </div>

      <div className="grid gap-5 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>Score history</CardTitle>
            <CardDescription>
              Each point is the score on a quarter end, using only data public that day. Click a point to view that
              date.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {history.length > 0 ? (
              <ScoreChart
                history={history}
                asOf={asOf}
                cases={c?.bankruptcy_cases ?? []}
                onPick={(d) => router.replace(`/company/${cik}?as_of=${d}`, { scroll: false })}
              />
            ) : (
              <p className="py-10 text-center text-sm text-muted-foreground">
                {scores.loading ? "Loading…" : "No scores."}
              </p>
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Why this score</CardTitle>
            <CardDescription>
              Top features pushing risk up on {fmtDate(asOf)} (TreeSHAP), versus a year earlier.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {current?.top_drivers.length ? (
              <ol className="space-y-3">
                {current.top_drivers.map((d, i) => {
                  const max = current.top_drivers[0].contribution || 1;
                  return (
                    <li key={d.feature} className="space-y-1">
                      <div className="flex gap-2 text-sm">
                        <span className="w-4 shrink-0 text-muted-foreground tabular-nums">{i + 1}.</span>
                        <span className="font-medium">{d.text}</span>
                      </div>
                      <div className="ml-6 h-1.5 rounded-full bg-secondary">
                        <div
                          className="h-1.5 rounded-full bg-risk"
                          style={{ width: `${Math.max(6, (d.contribution / max) * 100)}%` }}
                        />
                      </div>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p className="text-sm text-muted-foreground">
                {scores.loading ? "Loading…" : "No risk-increasing drivers on this date."}
              </p>
            )}
            {source && c && (
              <p className="border-t pt-3 text-xs text-muted-foreground">
                Inputs: filings through {fmtDate(source.filed)}. Latest:{" "}
                <a
                  className="inline-flex items-center gap-0.5 text-brand hover:underline"
                  href={edgarFiling(c.cik, source.accession)}
                  target="_blank"
                  rel="noreferrer"
                >
                  {source.accession} <ExternalLink className="size-3" />
                </a>
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Key metrics</CardTitle>
          <CardDescription>
            As reported to the SEC (XBRL), latest version of each period. Hollow points are derived (Q4 = full year − 9
            months, or a formula); amber rings were restated later. Click any point or value to open its source filing.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {metrics.data && c ? (
            <MetricTimeline cik={c.cik} points={metrics.data} asOf={asOf} />
          ) : (
            <p className="py-8 text-center text-sm text-muted-foreground">{metrics.error ?? "Loading…"}</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Filing events</CardTitle>
          <CardDescription>From 8-K item codes and late-filing notices (NT 10-K / NT 10-Q) on EDGAR.</CardDescription>
        </CardHeader>
        <CardContent>
          {events.data && c ? (
            <EventTimeline cik={c.cik} events={events.data} asOf={asOf} />
          ) : (
            <p className="py-8 text-center text-sm text-muted-foreground">{events.error ?? "Loading…"}</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
