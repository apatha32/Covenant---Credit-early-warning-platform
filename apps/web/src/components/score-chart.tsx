"use client";

import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { TIERS, fmtDate, pct } from "@/lib/format";
import type { Company, ScorePoint } from "@/lib/types";

type Props = {
  history: ScorePoint[];
  asOf: string;
  cases: Company["bankruptcy_cases"];
  onPick: (asOf: string) => void;
};

const toTime = (iso: string) => new Date(`${iso}T00:00:00Z`).getTime();

export function ScoreChart({ history, asOf, cases, onPick }: Props) {
  const data = history.map((h) => ({ ...h, t: toTime(h.as_of_date) }));
  const first = data[0]?.t ?? 0;
  const last = data[data.length - 1]?.t ?? 0;
  const shownCases = cases.filter((k) => toTime(k.case_start) >= first && toTime(k.case_start) <= last + 400 * 864e5);
  const domainEnd = Math.max(last, ...shownCases.map((k) => toTime(k.case_start)));

  return (
    <div className="w-full">
      <div className="h-72 w-full">
        <ResponsiveContainer>
          <LineChart
            data={data}
            margin={{ top: 18, right: 12, bottom: 0, left: 0 }}
            onClick={(e) => {
              const i = e?.activeTooltipIndex;
              if (typeof i === "number" && data[i]) onPick(data[i].as_of_date);
            }}
          >
            <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={[first, domainEnd]}
              tickFormatter={(t: number) => new Date(t).getUTCFullYear().toString()}
              stroke="var(--muted-foreground)"
              fontSize={12}
              tickLine={false}
            />
            <YAxis
              tickFormatter={(v: number) => pct(v, 0)}
              stroke="var(--muted-foreground)"
              fontSize={12}
              width={44}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              cursor={{ stroke: "var(--muted-foreground)", strokeDasharray: "3 3" }}
              content={({ active, payload }) => {
                const p = payload?.[0]?.payload as (ScorePoint & { t: number }) | undefined;
                if (!active || !p) return null;
                return (
                  <div className="rounded-lg border bg-popover px-3 py-2 text-xs shadow-md">
                    <div className="font-semibold">{fmtDate(p.as_of_date)}</div>
                    <div className="tabular-nums">
                      {pct(p.probability)} · {TIERS[p.risk_tier].label}
                    </div>
                  </div>
                );
              }}
            />
            {shownCases.map((k) => (
              <ReferenceLine key={k.case_start} x={toTime(k.case_start)} stroke="var(--risk)" strokeWidth={2} />
            ))}
            <ReferenceLine x={toTime(asOf)} stroke="var(--brand)" strokeDasharray="4 3" />
            <Line
              dataKey="probability"
              stroke="var(--chart-line)"
              strokeWidth={2}
              isAnimationActive={false}
              dot={(props: { cx?: number; cy?: number; payload?: ScorePoint }) => {
                const { cx, cy, payload } = props;
                if (cx == null || cy == null || !payload) return <g key={`${cx}-${cy}`} />;
                const on = payload.as_of_date === asOf;
                return (
                  <circle
                    key={payload.as_of_date}
                    cx={cx}
                    cy={cy}
                    r={on ? 5.5 : 3}
                    fill={TIERS[payload.risk_tier].dot}
                    stroke={on ? "var(--foreground)" : "var(--card)"}
                    strokeWidth={on ? 2 : 1}
                    style={{ cursor: "pointer" }}
                  />
                );
              }}
              activeDot={{ r: 6 }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-3 w-0.5 bg-risk" /> Bankruptcy filing
          {shownCases.length ? `: ${shownCases.map((k) => fmtDate(k.case_start)).join(", ")}` : ""}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-3 w-0 border-l border-dashed border-brand" /> Viewing {fmtDate(asOf)}
        </span>
        <span>Dot color = risk tier on that date</span>
      </div>
    </div>
  );
}
