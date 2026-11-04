"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo } from "react";
import { ChevronLeft, ChevronRight, History, Sparkles } from "lucide-react";
import { Sparkline, TierBadge } from "@/components/risk";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { STORY, TIERS, asOfLabel, fmtDate, monthsBetween, pct } from "@/lib/format";
import { useQuery } from "@/lib/supabase";
import { useWorkspace } from "@/lib/workspace";
import type { Tier, WatchlistRow } from "@/lib/types";
import { cn } from "@/lib/utils";

const TIER_ORDER: Tier[] = ["high", "elevated", "low"];

export default function WatchlistPage() {
  return (
    <Suspense fallback={<Loading />}>
      <Watchlist />
    </Suspense>
  );
}

function Watchlist() {
  const ws = useWorkspace();
  if (ws.loading) return <Loading />;
  if (!ws.tenant) return <NoWorkspace />;
  return <WatchlistFor tenantId={ws.tenant.id} tenantName={ws.tenant.name} isDemo={ws.isDemo} />;
}

function NoWorkspace() {
  return (
    <div className="mx-auto max-w-md space-y-3 pt-16 text-center">
      <h1 className="text-2xl font-bold">No workspace yet</h1>
      <p className="text-sm text-muted-foreground">
        Create a workspace and upload your borrower list to get a watchlist.
      </p>
      <Button asChild>
        <Link href="/portfolio">Set up a workspace →</Link>
      </Button>
    </div>
  );
}

function WatchlistFor({ tenantId, tenantName, isDemo }: { tenantId: string; tenantName: string; isDemo: boolean }) {
  const router = useRouter();
  const params = useSearchParams();
  const tier = (params.get("tier") ?? "all") as Tier | "all";
  const sector = params.get("sector") ?? "all";

  const dates = useQuery<{ as_of_date: string }[]>(
    (sb) => sb.from("as_of_dates").select("as_of_date").order("as_of_date"),
    [],
  );
  const latest = dates.data?.at(-1)?.as_of_date;
  // The demo opens on the guided Spirit date; a lender's own book opens on the latest scores.
  const asOf = params.get("as_of") ?? (isDemo ? STORY.asOf : (latest ?? ""));
  const rows = useQuery<WatchlistRow[]>(
    (sb) =>
      asOf
        ? sb
            .from("watchlist")
            .select("*")
            .eq("tenant_id", tenantId)
            .eq("as_of_date", asOf)
            .order("probability", { ascending: false })
        : Promise.resolve({ data: [], error: null }),
    [asOf, tenantId],
  );

  function update(next: Record<string, string>) {
    const q = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(next)) {
      if (v === "all" || v === "") q.delete(k);
      else q.set(k, v);
    }
    router.replace(`/watchlist?${q.toString()}`, { scroll: false });
  }

  const allDates = useMemo(() => (dates.data ?? []).map((d) => d.as_of_date), [dates.data]);
  const index = allDates.indexOf(asOf);
  const ranked = useMemo(
    () =>
      [...new Map((rows.data ?? []).map((r) => [r.cik, r])).values()] // one row per company across portfolios
        .sort(
          (a, b) => TIER_ORDER.indexOf(a.risk_tier) - TIER_ORDER.indexOf(b.risk_tier) || b.probability - a.probability,
        ),
    [rows.data],
  );
  const sectors = useMemo(() => [...new Set(ranked.map((r) => r.sector))].sort(), [ranked]);
  const shown = ranked.filter(
    (r) => (tier === "all" || r.risk_tier === tier) && (sector === "all" || r.sector === sector),
  );
  const counts = Object.fromEntries(TIER_ORDER.map((t) => [t, ranked.filter((r) => r.risk_tier === t).length]));
  const story = ranked.find((r) => r.cik === STORY.cik);
  const storyOn = isDemo && asOf === STORY.asOf && story;

  return (
    <div className="space-y-5 pt-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-semibold tracking-[0.08em] text-brand uppercase">{tenantName}</p>
          <h1 className="text-3xl font-bold tracking-tight">Watchlist</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {isDemo ? (
              <>
                50 SEC filers: 25 of the largest companies that went bankrupt in 2022 or later (chosen by size, not by
                score, so misses stay in) and 25 healthy companies across sectors.
              </>
            ) : (
              <>
                Matched borrowers that have scores. New borrowers are scored within the hour; see{" "}
                <Link href="/portfolio" className="text-brand hover:underline">
                  Portfolio
                </Link>{" "}
                for each row&apos;s status.
              </>
            )}
          </p>
        </div>
      </div>

      {/* As-of rewind: the core of the demo */}
      <Card className="border-brand/40 bg-gradient-to-br from-brand-soft/70 to-card py-0 shadow-sm">
        <CardContent className="space-y-3 p-4 sm:p-5">
          <div className="flex flex-wrap items-center gap-3">
            <History className="size-5 text-brand" aria-hidden />
            <div className="mr-auto">
              <div className="text-xs font-semibold tracking-wide text-brand uppercase">As of</div>
              <div className="text-xl font-bold tabular-nums">{asOf ? asOfLabel(asOf) : "No scores yet"}</div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="icon"
                aria-label="Previous quarter"
                disabled={index <= 0}
                onClick={() => update({ as_of: allDates[index - 1] })}
              >
                <ChevronLeft />
              </Button>
              <select
                aria-label="As-of date"
                className="h-9 rounded-md border bg-card px-2 text-sm tabular-nums"
                value={asOf || ""}
                onChange={(e) => update({ as_of: e.target.value })}
              >
                {allDates.length === 0 && <option value={asOf}>{asOf}</option>}
                {allDates
                  .slice()
                  .reverse()
                  .map((d) => (
                    <option key={d} value={d}>
                      {asOfLabel(d)}
                    </option>
                  ))}
              </select>
              <Button
                variant="outline"
                size="icon"
                aria-label="Next quarter"
                disabled={index < 0 || index >= allDates.length - 1}
                onClick={() => update({ as_of: allDates[index + 1] })}
              >
                <ChevronRight />
              </Button>
            </div>
          </div>
          {allDates.length > 1 && (
            <input
              type="range"
              aria-label="Rewind through quarters"
              className="w-full accent-[var(--brand)]"
              min={0}
              max={allDates.length - 1}
              value={Math.max(index, 0)}
              onChange={(e) => update({ as_of: allDates[Number(e.target.value)] })}
            />
          )}
          <p className="text-sm text-muted-foreground">
            Rewind to any quarter. Every score uses only filings and macro data that were public on that date, so this
            is what an analyst would have seen then.
          </p>
        </CardContent>
      </Card>

      {storyOn && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-risk/30 bg-risk-soft/60 p-4">
          <Sparkles className="size-5 shrink-0 text-risk" aria-hidden />
          <p className="mr-auto max-w-3xl text-sm">
            <b>Guided demo.</b> It is {fmtDate(STORY.asOf)}. {STORY.name} files for Chapter 11 on{" "}
            {story.next_case_start ? fmtDate(story.next_case_start) : "Nov 18, 2024"}, about{" "}
            {story.next_case_start ? monthsBetween(STORY.asOf, story.next_case_start).toFixed(1) : "4.6"} months from
            now. Covenant already ranks it <b>{TIERS[story.risk_tier].label.toLowerCase()} risk</b>.
          </p>
          <Button asChild>
            <Link href={`/company/${STORY.cik}?as_of=${STORY.asOf}`}>Why? Open Spirit&apos;s drivers →</Link>
          </Button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {(["all", ...TIER_ORDER] as const).map((t) => (
          <button
            key={t}
            onClick={() => update({ tier: t })}
            className={cn(
              "rounded-full border px-3 py-1 text-sm transition-colors",
              tier === t ? "border-foreground bg-foreground text-background" : "bg-card hover:bg-secondary",
            )}
          >
            {t === "all" ? `All ${ranked.length}` : `${TIERS[t].label} ${counts[t] ?? 0}`}
          </button>
        ))}
        <select
          aria-label="Sector"
          className="ml-auto h-8 rounded-md border bg-card px-2 text-sm"
          value={sector}
          onChange={(e) => update({ sector: e.target.value })}
        >
          <option value="all">All sectors</option>
          {sectors.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      <Card className="overflow-hidden py-0">
        {rows.error ? (
          <ErrorNote message={rows.error} />
        ) : rows.loading && !rows.data ? (
          <Loading />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="bg-surface-2 hover:bg-surface-2">
                <TableHead className="w-10 text-right">#</TableHead>
                <TableHead>Company</TableHead>
                <TableHead>Risk tier</TableHead>
                <TableHead
                  className="text-right"
                  title="Calibrated probability of a bankruptcy filing in the next 12 months"
                >
                  12-mo prob.
                </TableHead>
                <TableHead className="hidden sm:table-cell">Trend</TableHead>
                <TableHead className="hidden md:table-cell">Top driver</TableHead>
                <TableHead className="hidden lg:table-cell" title="Hindsight from the labels. Never a model input.">
                  What happened next
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.map((r, i) => (
                <TableRow
                  key={r.cik}
                  className={cn(
                    "cursor-pointer",
                    storyOn && r.cik === STORY.cik && "bg-risk-soft/50 hover:bg-risk-soft/70",
                  )}
                  onClick={() => router.push(`/company/${r.cik}?as_of=${asOf}`)}
                >
                  <TableCell className="text-right text-muted-foreground tabular-nums">{i + 1}</TableCell>
                  <TableCell className="max-w-[16rem]">
                    <Link
                      href={`/company/${r.cik}?as_of=${asOf}`}
                      className="font-medium hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {r.name}
                    </Link>
                    <div className="truncate text-xs text-muted-foreground">
                      {[r.tickers?.[0], r.sector].filter(Boolean).join(" · ")}
                    </div>
                  </TableCell>
                  <TableCell>
                    <TierBadge tier={r.risk_tier} />
                  </TableCell>
                  <TableCell className="text-right font-medium tabular-nums">{pct(r.probability)}</TableCell>
                  <TableCell className="hidden sm:table-cell">
                    <Sparkline values={r.sparkline ?? []} tier={r.risk_tier} />
                  </TableCell>
                  <TableCell className="hidden max-w-[22rem] text-sm whitespace-normal text-muted-foreground md:table-cell">
                    {r.top_driver ?? "—"}
                  </TableCell>
                  <TableCell className="hidden text-sm whitespace-nowrap lg:table-cell">
                    {r.next_case_start ? (
                      <span
                        className={monthsBetween(asOf, r.next_case_start) <= 12 ? "text-risk" : "text-muted-foreground"}
                        title={
                          monthsBetween(asOf, r.next_case_start) <= 12
                            ? "Within the model's 12-month horizon"
                            : "Beyond the 12-month horizon"
                        }
                      >
                        Bankruptcy {fmtDate(r.next_case_start)}{" "}
                        <span className="text-muted-foreground">
                          (+{monthsBetween(asOf, r.next_case_start).toFixed(0)} mo)
                        </span>
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
              {shown.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="py-10 text-center text-muted-foreground">
                    {ranked.length === 0 && !isDemo
                      ? "No scored borrowers on this date yet. Upload borrowers on the Portfolio page."
                      : "No companies match these filters on this date."}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        )}
      </Card>
      <p className="text-xs text-muted-foreground">
        Tiers rank each company against all 4,400–5,100 SEC filers the model scores on that date (high = riskiest 5%).
        Red outcomes fall inside the model&apos;s 12-month horizon. Companies are missing on dates when they were inside
        a bankruptcy case or had stopped filing. &ldquo;What happened next&rdquo; is hindsight for the demo and is never
        a model input.
      </p>
    </div>
  );
}

function Loading() {
  return <div className="py-16 text-center text-sm text-muted-foreground">Loading…</div>;
}

function ErrorNote({ message }: { message: string }) {
  return <div className="p-6 text-sm text-risk">Could not load the data: {message}. Reload the page to try again.</div>;
}
