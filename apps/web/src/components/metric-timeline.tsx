"use client";

import { ExternalLink } from "lucide-react";
import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { METRICS, edgarFiling, fmtDate, knownOn, usd } from "@/lib/format";
import type { MetricPoint } from "@/lib/types";

const toTime = (iso: string) => new Date(`${iso}T00:00:00Z`).getTime();

/** One series per metric: quarterly values for flows; for balance-sheet items, the quarter-end
 * value (from the 10-Q, or the 10-K at fiscal year end). */
function series(points: MetricPoint[], key: string, kind: "flow" | "instant"): MetricPoint[] {
  const mine = points.filter((p) => p.metric === key);
  if (kind === "flow") return mine.filter((p) => p.period_type === "Q");
  const byEnd = new Map<string, MetricPoint>();
  for (const p of mine) {
    const prev = byEnd.get(p.period_end);
    if (!prev || (prev.period_type === "FY" && p.period_type === "Q")) byEnd.set(p.period_end, p);
  }
  return [...byEnd.values()].sort((a, b) => a.period_end.localeCompare(b.period_end));
}

export function MetricTimeline({ cik, points, asOf }: { cik: string; points: MetricPoint[]; asOf: string }) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {METRICS.map((m) => {
        const data = series(points, m.key, m.kind).map((p) => {
          const k = knownOn(p, asOf);
          return { ...p, t: toTime(p.period_end), shown: k.value, known: k };
        });
        type Row = (typeof data)[number];
        const known = data.filter((p) => p.known.state !== "future");
        const latest = known[known.length - 1];
        return (
          <div key={m.key} className="rounded-lg border bg-card p-3">
            <div className="flex items-baseline justify-between gap-2">
              <div className="text-sm font-medium">{m.label}</div>
              {latest && (
                <a
                  href={edgarFiling(cik, latest.known.accession)}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-0.5 text-sm font-semibold text-brand tabular-nums hover:underline"
                  title={`Period ending ${fmtDate(latest.period_end)}, filed ${fmtDate(latest.known.filed)}`}
                >
                  {usd(latest.shown)} <ExternalLink className="size-3" />
                </a>
              )}
            </div>
            <div className="text-xs text-muted-foreground">
              {latest ? `Known on ${fmtDate(asOf)}: period ending ${fmtDate(latest.period_end)}` : "Not reported"}
            </div>
            {data.length > 1 ? (
              <div className="mt-2 h-28">
                <ResponsiveContainer>
                  <LineChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
                    <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} hide />
                    <YAxis hide domain={["auto", "auto"]} />
                    <ReferenceLine x={toTime(asOf)} stroke="var(--brand)" strokeDasharray="4 3" />
                    <Tooltip
                      content={({ active, payload }) => {
                        const p = payload?.[0]?.payload as Row | undefined;
                        if (!active || !p) return null;
                        return (
                          <div className="max-w-64 rounded-lg border bg-popover px-3 py-2 text-xs shadow-md">
                            <div className="font-semibold tabular-nums">{usd(p.shown)}</div>
                            <div>
                              {p.period_type === "Q" ? "Quarter" : "Year"} ending {fmtDate(p.period_end)}
                            </div>
                            <div className="text-muted-foreground">
                              Filed {fmtDate(p.known.filed)} · {p.source_tag}
                            </div>
                            {p.is_derived && (
                              <div className="text-muted-foreground">Derived from other reported values</div>
                            )}
                            {p.known.state === "original" && (
                              <div className="text-brand">
                                As first filed; restated to {usd(p.value)} on {fmtDate(p.filed_date)}
                              </div>
                            )}
                            {p.known.state === "latest" && p.is_restated && (
                              <div className="text-brand">Restated: first reported as {usd(p.original_value)}</div>
                            )}
                            {p.known.state === "future" && (
                              <div className="text-muted-foreground">Not yet public on {fmtDate(asOf)}</div>
                            )}
                            <div className="mt-1 text-brand">Click to open the filing on EDGAR</div>
                          </div>
                        );
                      }}
                    />
                    <Line
                      dataKey="shown"
                      stroke="var(--muted-foreground)"
                      strokeOpacity={0.5}
                      strokeWidth={1.5}
                      isAnimationActive={false}
                      dot={(props: { cx?: number; cy?: number; payload?: Row }) => {
                        const { cx, cy, payload: p } = props;
                        if (cx == null || cy == null || !p) return <g key={`${cx}-${cy}`} />;
                        const future = p.known.state === "future";
                        return (
                          <circle
                            key={`${p.period_type}-${p.period_end}`}
                            cx={cx}
                            cy={cy}
                            r={p.is_restated ? 3.6 : 2.6}
                            fill={p.is_derived ? "var(--card)" : "var(--chart-line)"}
                            stroke={p.is_restated ? "var(--brand)" : "var(--chart-line)"}
                            strokeWidth={p.is_restated ? 2 : 1.25}
                            opacity={future ? 0.3 : 1}
                            style={{ cursor: "pointer" }}
                            onClick={() => window.open(edgarFiling(cik, p.known.accession), "_blank", "noopener")}
                          />
                        );
                      }}
                      activeDot={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <div className="mt-2 h-28" />
            )}
            <div className="mt-1 flex gap-3 text-[11px] text-muted-foreground">
              <span>{data.filter((p) => p.is_derived).length} derived</span>
              <span>{data.filter((p) => p.is_restated).length} restated</span>
              <span>faded = filed after {fmtDate(asOf)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
