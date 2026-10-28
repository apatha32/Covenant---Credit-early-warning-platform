"use client";

import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EVENT_TYPES, edgarFiling, fmtDate } from "@/lib/format";
import type { FilingEvent } from "@/lib/types";

const ORDER = Object.keys(EVENT_TYPES);
const toTime = (iso: string) => new Date(`${iso}T00:00:00Z`).getTime();

/** Swimlanes (one per event type) over time, then the list of filings. */
export function EventTimeline({ cik, events, asOf }: { cik: string; events: FilingEvent[]; asOf: string }) {
  const [all, setAll] = useState(false);
  if (events.length === 0)
    return <p className="text-sm text-muted-foreground">No 8-K risk events or late-filing notices.</p>;

  const start = toTime("2014-01-01");
  const end = Math.max(toTime(asOf), ...events.map((e) => toTime(e.filed_date)));
  const x = (iso: string) => `${Math.min(100, Math.max(0, ((toTime(iso) - start) / (end - start)) * 100))}%`;
  const lanes = ORDER.filter((t) => events.some((e) => e.event_type === t));
  const years: number[] = [];
  for (let y = 2014; y <= new Date(end).getUTCFullYear(); y += 2) years.push(y);
  const known = events.filter((e) => e.filed_date <= asOf);
  const list = all ? events : known.slice(0, 12);

  return (
    <div className="space-y-5">
      <div className="space-y-1.5">
        {lanes.map((t) => (
          <div key={t} className="flex items-center gap-3">
            <div className="w-28 shrink-0 text-xs text-muted-foreground sm:w-36">{EVENT_TYPES[t].short}</div>
            <div className="relative h-6 flex-1 rounded bg-surface-2">
              <div className="absolute inset-y-0 w-px bg-brand" style={{ left: x(asOf) }} />
              {events
                .filter((e) => e.event_type === t && e.filed_date >= "2014-01-01")
                .map((e) => (
                  <a
                    key={e.accession}
                    href={edgarFiling(cik, e.accession)}
                    target="_blank"
                    rel="noreferrer"
                    title={`${EVENT_TYPES[t].label}, filed ${fmtDate(e.filed_date)}`}
                    className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-card"
                    style={{
                      left: x(e.filed_date),
                      background:
                        t === "bankruptcy"
                          ? "var(--risk)"
                          : t === "exec_departure"
                            ? "var(--muted-foreground)"
                            : "var(--brand)",
                      opacity: e.filed_date > asOf ? 0.35 : 1,
                    }}
                  />
                ))}
            </div>
          </div>
        ))}
        <div className="flex gap-3">
          <div className="w-28 shrink-0 sm:w-36" />
          <div className="relative h-4 flex-1 text-[11px] text-muted-foreground">
            {years.map((y) => (
              <span key={y} className="absolute -translate-x-1/2" style={{ left: x(`${y}-01-01`) }}>
                {y}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <div className="text-sm font-medium">
            {all ? `All ${events.length} events` : `Latest events public on ${fmtDate(asOf)}`}
          </div>
          <Button variant="ghost" size="sm" onClick={() => setAll(!all)}>
            {all ? "Show latest only" : "Show all, including later ones"}
          </Button>
        </div>
        <ul className="divide-y rounded-lg border">
          {list.map((e) => (
            <li
              key={`${e.accession}-${e.event_type}`}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm"
            >
              <span className="w-28 text-muted-foreground tabular-nums">{fmtDate(e.filed_date)}</span>
              <span className="mr-auto">
                {EVENT_TYPES[e.event_type]?.label ?? e.event_type}
                {e.filed_date > asOf && (
                  <span className="ml-2 text-xs text-muted-foreground">(after {fmtDate(asOf)})</span>
                )}
              </span>
              <a
                href={edgarFiling(cik, e.accession)}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-xs text-brand hover:underline"
              >
                {e.form_type}
                {e.raw_items ? ` · items ${e.raw_items}` : ""} <ExternalLink className="size-3" />
              </a>
            </li>
          ))}
          {list.length === 0 && <li className="px-3 py-3 text-sm text-muted-foreground">None before this date.</li>}
        </ul>
      </div>
    </div>
  );
}
