"use client";

import Link from "next/link";
import { useState } from "react";
import { BellRing, Check, X } from "lucide-react";
import { TierBadge } from "@/components/risk";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api } from "@/lib/api";
import { fmtDate, pct } from "@/lib/format";
import { useQuery } from "@/lib/supabase";
import type { Alert } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace";

export default function AlertsPage() {
  const ws = useWorkspace();
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [refresh, setRefresh] = useState(0);
  const tenantId = ws.tenant?.id ?? "";
  const alerts = useQuery<Alert[]>(
    (sb) =>
      tenantId
        ? sb.from("alerts").select("*").eq("tenant_id", tenantId).order("triggered_at", { ascending: false })
        : Promise.resolve({ data: [], error: null }),
    [tenantId, refresh],
  );
  const all = alerts.data ?? [];
  const shown = filter === "open" ? all.filter((a) => a.status === "open") : all;

  if (ws.loading) return <p className="py-16 text-center text-sm text-muted-foreground">Loading…</p>;
  return (
    <div className="space-y-5 pt-6">
      <div>
        <p className="text-xs font-semibold tracking-[0.08em] text-brand uppercase">
          {ws.tenant?.name ?? "No workspace"}
        </p>
        <h1 className="text-3xl font-bold tracking-tight">Alerts</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          An alert opens when a borrower&apos;s risk tier rises, or when a borrower is already high risk on the day
          it&apos;s added. Mark each one <b>Agree</b> (a real concern) or <b>Dismiss</b> with a reason; the decision is
          kept with who made it.
          {ws.isDemo && " Demo alerts are dated on the quarter the score changed; the demo is read-only."}
        </p>
      </div>
      <div className="flex gap-2">
        {(["open", "all"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={cn(
              "rounded-full border px-3 py-1 text-sm",
              filter === f ? "border-foreground bg-foreground text-background" : "bg-card hover:bg-secondary",
            )}
          >
            {f === "open" ? `Open ${all.filter((a) => a.status === "open").length}` : `All ${all.length}`}
          </button>
        ))}
      </div>
      {alerts.error && <p className="text-sm text-risk">{alerts.error}</p>}
      <div className="space-y-3">
        {shown.map((a) => (
          <AlertCard key={a.id} alert={a} canWrite={ws.canWrite} onDone={() => setRefresh((r) => r + 1)} />
        ))}
        {!alerts.loading && shown.length === 0 && (
          <p className="py-10 text-center text-sm text-muted-foreground">
            {filter === "open" ? "No open alerts." : "No alerts yet."}
          </p>
        )}
      </div>
    </div>
  );
}

function AlertCard({ alert: a, canWrite, onDone }: { alert: Alert; canWrite: boolean; onDone: () => void }) {
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function dispose(decision: "agree" | "dismiss") {
    setBusy(true);
    setError(null);
    try {
      await api(`/alerts/${a.id}/disposition`, { json: { decision, reason: reason || null } });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const asOf = a.as_of_date ?? a.triggered_at.slice(0, 10);
  return (
    <Card className={cn("py-0", a.status === "open" && "border-risk/30")}>
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <BellRing className={cn("size-4", a.status === "open" ? "text-risk" : "text-muted-foreground")} aria-hidden />
          <Link href={`/company/${a.cik}?as_of=${asOf}`} className="font-semibold hover:underline">
            {a.name ?? `CIK ${Number(a.cik)}`}
          </Link>
          {a.risk_tier && <TierBadge tier={a.risk_tier} />}
          {a.probability !== null && <span className="text-sm tabular-nums">{pct(a.probability)}</span>}
          <span className="ml-auto text-xs text-muted-foreground">Score as of {fmtDate(asOf)}</span>
        </div>
        <p className="text-sm">{a.reason}</p>
        {a.decision ? (
          <p className="text-sm text-muted-foreground">
            {a.decision === "agree" ? "Agreed" : "Dismissed"}
            {a.decided_by_me ? " by you" : " by a teammate"}
            {a.decided_at && ` on ${fmtDate(a.decided_at.slice(0, 10))}`}
            {a.disposition_reason && `: “${a.disposition_reason}”`}
          </p>
        ) : (
          canWrite &&
          a.status === "open" && (
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={busy} onClick={() => dispose("agree")}>
                <Check /> Agree
              </Button>
              {dismissing ? (
                <form
                  className="flex flex-1 flex-wrap items-center gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void dispose("dismiss");
                  }}
                >
                  <input
                    required
                    autoFocus
                    maxLength={1000}
                    placeholder="Why? e.g. fully secured, already on watch"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="h-8 min-w-60 flex-1 rounded-md border bg-background px-2 text-sm"
                  />
                  <Button size="sm" variant="outline" type="submit" disabled={busy}>
                    Dismiss
                  </Button>
                </form>
              ) : (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => setDismissing(true)}>
                  <X /> Dismiss…
                </Button>
              )}
            </div>
          )
        )}
        {error && <p className="text-sm text-risk">{error}</p>}
      </CardContent>
    </Card>
  );
}
