"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { signInWithPassword, signUp } from "@/lib/supabase";
import { cn } from "@/lib/utils";

export default function LoginPage() {
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "signin") {
        await signInWithPassword(email, password);
      } else if (!(await signUp(email, password))) {
        setNotice("Check your email for a confirmation link, then come back and sign in.");
        setMode("signin");
        return;
      }
      window.location.href = "/portfolio"; // full reload: every query re-runs as the new user
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm space-y-5 pt-12">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">{mode === "signin" ? "Sign in" : "Create an account"}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          An account gets its own workspace: upload a borrower list, review the matches, and get alerts when a
          borrower&apos;s risk rises. Teammates you add see the same workspace; nobody else does.
        </p>
      </div>
      <div className="grid grid-cols-2 rounded-lg border bg-card p-1 text-sm">
        {(["signin", "signup"] as const).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setMode(m)}
            className={cn("rounded-md py-1.5", mode === m ? "bg-secondary font-semibold" : "text-muted-foreground")}
          >
            {m === "signin" ? "Sign in" : "Create account"}
          </button>
        ))}
      </div>
      <Card className="py-0">
        <CardContent className="p-5">
          <form onSubmit={submit} className="space-y-3">
            <label className="block space-y-1 text-sm">
              <span className="font-medium">Email</span>
              <input
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="h-9 w-full rounded-md border bg-background px-3"
              />
            </label>
            <label className="block space-y-1 text-sm">
              <span className="font-medium">Password</span>
              <input
                type="password"
                required
                minLength={8}
                autoComplete={mode === "signin" ? "current-password" : "new-password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="h-9 w-full rounded-md border bg-background px-3"
              />
            </label>
            {error && <p className="text-sm text-risk">{error}</p>}
            {notice && <p className="text-sm text-brand">{notice}</p>}
            <Button type="submit" className="w-full" disabled={busy}>
              {busy ? "Working…" : mode === "signin" ? "Sign in" : "Create account"}
            </Button>
          </form>
        </CardContent>
      </Card>
      <p className="text-center text-sm text-muted-foreground">
        Just looking?{" "}
        <Link href="/watchlist" className="text-brand hover:underline">
          Open the demo without an account
        </Link>
      </p>
    </div>
  );
}
