"use client";

import { ExternalLink } from "lucide-react";
import { SECTIONS, SIGNALS, edgarFiling, fmtDate } from "@/lib/format";
import type { TextSignal } from "@/lib/types";

/** The latest evidence for each signal type, from filings public on `asOf`. */
export function TextSignals({ cik, signals, asOf }: { cik: string; signals: TextSignal[]; asOf: string }) {
  const known = signals.filter((s) => s.filed_date <= asOf);
  const latest = new Map<string, TextSignal>();
  for (const s of known) {
    const prev = latest.get(s.signal_type);
    if (!prev || s.filed_date > prev.filed_date) latest.set(s.signal_type, s);
  }
  const recent = Object.keys(SIGNALS)
    .map((t) => latest.get(t))
    .filter((s): s is TextSignal => Boolean(s))
    .sort((a, b) => b.filed_date.localeCompare(a.filed_date));
  if (recent.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No risk signals found in this company&apos;s filings up to {fmtDate(asOf)}.
      </p>
    );
  }
  return (
    <ul className="space-y-3">
      {recent.map((s) => (
        <li key={s.signal_type} className="rounded-lg border bg-card p-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-sm font-semibold">{SIGNALS[s.signal_type] ?? s.signal_type}</span>
            <a
              href={edgarFiling(cik, s.accession)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-brand hover:underline"
            >
              {SECTIONS[s.section] ?? s.section}, filed {fmtDate(s.filed_date)} <ExternalLink className="size-3" />
            </a>
          </div>
          <blockquote className="mt-2 border-l-2 border-brand/50 pl-3 text-sm text-muted-foreground">
            &ldquo;{s.evidence_quote}&rdquo;
          </blockquote>
        </li>
      ))}
    </ul>
  );
}
