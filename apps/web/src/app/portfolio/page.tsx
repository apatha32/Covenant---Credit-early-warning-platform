"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Check, FileUp, Search, Trash2, UserPlus, X } from "lucide-react";
import { TierBadge } from "@/components/risk";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { WAKE_HINT, api, apiConfigured, wakeApi } from "@/lib/api";
import { pct } from "@/lib/format";
import { useQuery } from "@/lib/supabase";
import type { Borrower, Candidate, Coverage, MatchReview, Member, Portfolio, Role } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace";

type UploadResult = {
  counts: Record<string, number>;
  skipped: { row: number; reason: string }[];
  over_capacity: string[];
  new_alerts: number;
};

const STATUS: Record<Borrower["match_status"], { label: string; className: string }> = {
  auto_matched: { label: "Auto-matched", className: "text-ok" },
  confirmed: { label: "Confirmed", className: "text-ok" },
  needs_review: { label: "Needs review", className: "text-brand" },
  pending: { label: "Pending", className: "text-muted-foreground" },
  rejected: { label: "Rejected", className: "text-muted-foreground" },
  unmatched: { label: "Not an SEC filer", className: "text-muted-foreground" },
};

const COVERAGE: Record<Coverage, string> = {
  scored: "Scored",
  queued: "Queued: scored within the hour",
  no_data: "No recent financials to score",
  excluded: "Sector not covered by the model",
  failed: "Scoring failed; retried next run",
  over_capacity: "Not scored: demo capacity reached",
};

function none<T>() {
  return Promise.resolve({ data: [] as T[], error: null });
}

export default function PortfolioPage() {
  const ws = useWorkspace();
  const [refresh, setRefresh] = useState(0);
  const [portfolioId, setPortfolioId] = useState<string | null>(null);
  const tenantId = ws.tenant?.id ?? "";

  useEffect(() => {
    if (ws.canWrite) wakeApi();
  }, [ws.canWrite]);

  const portfolios = useQuery<Portfolio[]>(
    (sb) =>
      tenantId ? sb.from("portfolios").select("*").eq("tenant_id", tenantId).order("created_at") : none<Portfolio>(),
    [tenantId, refresh],
  );
  const portfolio = portfolios.data?.find((p) => p.id === portfolioId) ?? portfolios.data?.[0] ?? null;
  const pid = portfolio?.id ?? "";
  const borrowers = useQuery<Borrower[]>(
    (sb) =>
      pid
        ? sb
            .from("borrowers")
            .select("*")
            .eq("portfolio_id", pid)
            .order("row_number", { ascending: true, nullsFirst: false })
        : none<Borrower>(),
    [pid, refresh],
  );
  const reviews = useQuery<MatchReview[]>(
    (sb) =>
      pid ? sb.from("match_reviews").select("*").eq("portfolio_id", pid).order("row_number") : none<MatchReview>(),
    [pid, refresh],
  );
  const reload = () => setRefresh((r) => r + 1);

  if (ws.loading) return <Note>Loading…</Note>;
  if (!ws.tenant) {
    return ws.user && !ws.user.isAnonymous ? <CreateWorkspace onDone={ws.reload} /> : <Note>Loading…</Note>;
  }

  return (
    <div className="space-y-6 pt-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-semibold tracking-[0.08em] text-brand uppercase">{ws.tenant.name}</p>
          <h1 className="text-3xl font-bold tracking-tight">Portfolio</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            Upload a borrower list (CSV with a name and/or ticker column). Each row is matched to an SEC filer;
            confident matches are accepted automatically and the rest wait for you below. Matched companies get scored
            and appear on the watchlist.
          </p>
        </div>
        {(portfolios.data?.length ?? 0) > 1 && (
          <select
            aria-label="Portfolio"
            className="h-9 rounded-md border bg-card px-2 text-sm"
            value={pid}
            onChange={(e) => setPortfolioId(e.target.value)}
          >
            {portfolios.data?.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
      </div>

      {ws.isDemo && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-brand/30 bg-brand-soft/60 p-4">
          <p className="mr-auto max-w-3xl text-sm">
            This is the demo workspace, so it&apos;s read-only. <b>Create a free account</b> to upload your own borrower
            list and review its matches.
          </p>
          <Button asChild>
            <Link href="/login">Create an account →</Link>
          </Button>
        </div>
      )}

      {ws.canWrite && portfolio && <UploadCard portfolioId={portfolio.id} onDone={reload} />}

      {(reviews.data?.length ?? 0) > 0 && (
        <section className="space-y-3">
          <h2 className="text-lg font-semibold">
            Needs review <span className="text-muted-foreground">({reviews.data?.length})</span>
          </h2>
          {reviews.data?.map((r) => (
            <ReviewItem key={r.id} review={r} canWrite={ws.canWrite} onDone={reload} />
          ))}
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">
          Borrowers <span className="text-muted-foreground">({borrowers.data?.length ?? 0})</span>
        </h2>
        <BorrowerTable rows={borrowers.data ?? []} loading={borrowers.loading} canWrite={ws.canWrite} onDone={reload} />
        {borrowers.error && <p className="text-sm text-risk">{borrowers.error}</p>}
      </section>

      {!ws.isDemo && <TeamCard tenantId={ws.tenant.id} canManage={ws.canManage} myRole={ws.tenant.role} />}
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <div className="py-16 text-center text-sm text-muted-foreground">{children}</div>;
}

function ErrorText({ error }: { error: string | null }) {
  return error ? <p className="text-sm text-risk">{error}</p> : null;
}

function CreateWorkspace({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(wakeApi, []);
  return (
    <div className="mx-auto max-w-md space-y-4 pt-12">
      <h1 className="text-2xl font-bold tracking-tight">Create your workspace</h1>
      <p className="text-sm text-muted-foreground">
        A workspace holds your portfolios, review decisions and alerts. Only members you add can see it.
      </p>
      <form
        className="space-y-3"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            await api("/tenants", { json: { name } });
            onDone();
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        <input
          required
          minLength={2}
          maxLength={80}
          placeholder="e.g. Northwind Credit"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="h-9 w-full rounded-md border bg-background px-3 text-sm"
        />
        <ErrorText error={error} />
        <Button type="submit" disabled={busy || !apiConfigured}>
          {busy ? "Creating…" : "Create workspace"}
        </Button>
        {busy && <p className="text-xs text-muted-foreground">{WAKE_HINT}</p>}
        {!apiConfigured && <p className="text-xs text-risk">NEXT_PUBLIC_API_URL is not set.</p>}
      </form>
    </div>
  );
}

function UploadCard({ portfolioId, onDone }: { portfolioId: string; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  return (
    <Card className="py-0">
      <CardContent className="space-y-3 p-4 sm:p-5">
        <div className="flex flex-wrap items-center gap-3">
          <FileUp className="size-5 text-brand" aria-hidden />
          <div className="mr-auto">
            <div className="font-semibold">Upload borrowers</div>
            <div className="text-xs text-muted-foreground">
              CSV, up to 5,000 rows. Headers like <code>borrower_name</code>, <code>company</code>, <code>name</code>,{" "}
              <code>ticker</code>. Re-uploading the same rows adds nothing twice.{" "}
              <a href="/sample-borrowers.csv" download className="text-brand hover:underline">
                Download a messy sample
              </a>
            </div>
          </div>
        </div>
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!file) return;
            setBusy(true);
            setError(null);
            setResult(null);
            try {
              const form = new FormData();
              form.append("file", file);
              setResult(await api<UploadResult>(`/portfolios/${portfolioId}/upload`, { form }));
              onDone();
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="max-w-full text-sm file:mr-3 file:rounded-md file:border file:bg-card file:px-3 file:py-1.5 file:text-sm"
          />
          <Button type="submit" disabled={!file || busy || !apiConfigured}>
            {busy ? "Matching…" : "Upload and match"}
          </Button>
        </form>
        {busy && <p className="text-xs text-muted-foreground">{WAKE_HINT}</p>}
        <ErrorText error={error} />
        {result && (
          <div className="rounded-lg border bg-surface-2 p-3 text-sm">
            <b>Done.</b>{" "}
            {[
              ["auto_matched", "auto-matched"],
              ["needs_review", "need review"],
              ["confirmed", "matched by an earlier decision"],
              ["unmatched", "remembered as not an SEC filer"],
              ["duplicate", "already in this portfolio"],
            ]
              .filter(([k]) => result.counts[k])
              .map(([k, label]) => `${result.counts[k]} ${label}`)
              .join(", ")}
            .{result.skipped.some((s) => s.reason === "empty row") && " Empty rows were skipped."}
            {result.over_capacity.length > 0 &&
              ` ${result.over_capacity.length} matched companies won't be scored: the free demo database is full.`}
            {result.new_alerts > 0 && (
              <>
                {" "}
                <Link href="/alerts" className="font-medium text-risk hover:underline">
                  {result.new_alerts} new alert{result.new_alerts > 1 ? "s" : ""} →
                </Link>
              </>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReviewItem({ review, canWrite, onDone }: { review: MatchReview; canWrite: boolean; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<Candidate[] | null>(null);

  async function decide(decision: "accept" | "reject" | "no_match", cik?: string) {
    setBusy(true);
    setError(null);
    try {
      await api(`/matches/${review.id}/decision`, { json: { decision, cik: cik ? String(Number(cik)) : null } });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const candidates = found ?? review.candidate_ciks;
  return (
    <Card className="py-0">
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-xs text-muted-foreground tabular-nums">Row {review.row_number ?? "—"}</span>
          <span className="font-semibold">{review.uploaded_name ?? "(no name)"}</span>
          {review.uploaded_ticker && <span className="text-sm text-muted-foreground">{review.uploaded_ticker}</span>}
        </div>
        <div className="space-y-1.5">
          <div className="text-xs font-medium text-muted-foreground">
            {found ? "Search results" : "Closest SEC companies"}
          </div>
          {candidates.length === 0 && <p className="text-sm text-muted-foreground">No candidates.</p>}
          {candidates.map((c) => (
            <div key={c.cik} className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-sm">
              <span className="mr-auto">
                <span className="font-medium">{c.name}</span>
                <span className="ml-2 text-xs text-muted-foreground">
                  CIK {Number(c.cik)}
                  {c.matched_name && c.matched_name !== c.name && ` · matched “${c.matched_name}”`}
                  {c.kind === "former" && " (former name)"}
                  {c.kind === "ticker" && " · ticker"}
                  {c.is_excluded && " · sector not covered"}
                </span>
              </span>
              <span className="text-xs text-muted-foreground tabular-nums">{c.score.toFixed(0)}% similar</span>
              {canWrite && (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => decide("accept", c.cik)}>
                  <Check /> This one
                </Button>
              )}
            </div>
          ))}
        </div>
        {canWrite && (
          <div className="flex flex-wrap items-center gap-2">
            <form
              className="flex items-center gap-1"
              onSubmit={async (e) => {
                e.preventDefault();
                if (!query.trim()) return setFound(null);
                setError(null);
                try {
                  setFound(await api<Candidate[]>(`/companies/search?q=${encodeURIComponent(query)}`));
                } catch (err) {
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              <input
                placeholder="Search name or ticker"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="h-8 w-52 rounded-md border bg-background px-2 text-sm"
              />
              <Button size="sm" variant="outline" type="submit" aria-label="Search SEC companies">
                <Search />
              </Button>
            </form>
            <div className="ml-auto flex gap-2">
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => decide("reject")}>
                <X /> None of these
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                title="Remembered for this workspace: the same name won't be asked again"
                onClick={() => decide("no_match")}
              >
                Not an SEC filer
              </Button>
            </div>
          </div>
        )}
        <ErrorText error={error} />
      </CardContent>
    </Card>
  );
}

function BorrowerTable({
  rows,
  loading,
  canWrite,
  onDone,
}: {
  rows: Borrower[];
  loading: boolean;
  canWrite: boolean;
  onDone: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  if (loading && rows.length === 0) return <Note>Loading…</Note>;
  return (
    <Card className="overflow-hidden py-0">
      <Table>
        <TableHeader>
          <TableRow className="bg-surface-2 hover:bg-surface-2">
            <TableHead className="w-12 text-right">Row</TableHead>
            <TableHead>Uploaded</TableHead>
            <TableHead>SEC company</TableHead>
            <TableHead>Match</TableHead>
            <TableHead className="hidden md:table-cell">Scoring</TableHead>
            <TableHead className="text-right">Latest risk</TableHead>
            {canWrite && <TableHead className="w-10" />}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((b) => (
            <TableRow key={b.id}>
              <TableCell className="text-right text-muted-foreground tabular-nums">{b.row_number ?? "—"}</TableCell>
              <TableCell className="max-w-[14rem]">
                <div className="truncate font-medium">{b.uploaded_name ?? "—"}</div>
                {b.uploaded_ticker && <div className="text-xs text-muted-foreground">{b.uploaded_ticker}</div>}
              </TableCell>
              <TableCell className="max-w-[16rem] text-sm">
                {b.cik && b.sec_name ? (
                  b.coverage === "scored" ? (
                    <Link href={`/company/${b.cik}`} className="hover:underline">
                      {b.sec_name}
                    </Link>
                  ) : (
                    b.sec_name
                  )
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="text-sm whitespace-nowrap">
                <span className={STATUS[b.match_status].className}>{STATUS[b.match_status].label}</span>
                {b.match_confidence !== null && b.match_status === "auto_matched" && (
                  <span className="ml-1 text-xs text-muted-foreground">{pct(b.match_confidence, 0)}</span>
                )}
              </TableCell>
              <TableCell
                className="hidden text-xs whitespace-normal text-muted-foreground md:table-cell"
                title={b.coverage_detail ?? undefined}
              >
                {b.coverage ? COVERAGE[b.coverage] : "—"}
              </TableCell>
              <TableCell className="text-right whitespace-nowrap">
                {b.risk_tier && b.probability !== null ? (
                  <span className="inline-flex items-center gap-2">
                    <span className="text-sm tabular-nums">{pct(b.probability)}</span>
                    <TierBadge tier={b.risk_tier} />
                  </span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              {canWrite && (
                <TableCell>
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={`Remove ${b.uploaded_name ?? "borrower"}`}
                    onClick={async () => {
                      if (!confirm(`Remove ${b.uploaded_name ?? "this borrower"} from the portfolio?`)) return;
                      try {
                        await api(`/borrowers/${b.id}`, { method: "DELETE" });
                        onDone();
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err));
                      }
                    }}
                  >
                    <Trash2 />
                  </Button>
                </TableCell>
              )}
            </TableRow>
          ))}
          {rows.length === 0 && (
            <TableRow>
              <TableCell colSpan={7} className="py-10 text-center text-muted-foreground">
                No borrowers yet.
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
      {error && <p className="p-3 text-sm text-risk">{error}</p>}
    </Card>
  );
}

function TeamCard({ tenantId, canManage, myRole }: { tenantId: string; canManage: boolean; myRole: Role | null }) {
  const [refresh, setRefresh] = useState(0);
  const members = useQuery<Member[]>((sb) => sb.rpc("tenant_members", { p_tenant_id: tenantId }), [tenantId, refresh]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("analyst");
  const [error, setError] = useState<string | null>(null);
  const roles = useMemo<Role[]>(
    () => (myRole === "owner" ? ["viewer", "analyst", "admin", "owner"] : ["viewer", "analyst", "admin"]),
    [myRole],
  );
  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">Team</h2>
      <Card className="py-0">
        <CardContent className="space-y-3 p-4">
          <ul className="divide-y text-sm">
            {(members.data ?? []).map((m) => (
              <li key={m.user_id} className="flex items-center justify-between py-1.5">
                <span>{m.email}</span>
                <span className="text-muted-foreground capitalize">{m.role}</span>
              </li>
            ))}
          </ul>
          {canManage && (
            <form
              className={cn("flex flex-wrap items-center gap-2 border-t pt-3")}
              onSubmit={async (e) => {
                e.preventDefault();
                setError(null);
                try {
                  await api(`/tenants/${tenantId}/members`, { json: { email, role } });
                  setEmail("");
                  setRefresh((r) => r + 1);
                } catch (err) {
                  setError(err instanceof Error ? err.message : String(err));
                }
              }}
            >
              <input
                type="email"
                required
                placeholder="teammate@lender.com (must have an account)"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="h-8 min-w-64 flex-1 rounded-md border bg-background px-2 text-sm"
              />
              <select
                aria-label="Role"
                value={role}
                onChange={(e) => setRole(e.target.value as Role)}
                className="h-8 rounded-md border bg-card px-2 text-sm"
              >
                {roles.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <Button size="sm" type="submit">
                <UserPlus /> Add
              </Button>
            </form>
          )}
          <ErrorText error={error} />
          <p className="text-xs text-muted-foreground">
            Viewers read; analysts also upload, review matches and handle alerts; admins and owners manage the team.
          </p>
        </CardContent>
      </Card>
    </section>
  );
}
