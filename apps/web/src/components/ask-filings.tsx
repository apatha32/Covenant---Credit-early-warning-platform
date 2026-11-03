"use client";

import { useState } from "react";
import { ExternalLink, Loader2, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SECTIONS, fmtDate } from "@/lib/format";
import { accessToken } from "@/lib/supabase";
import type { QaAnswer } from "@/lib/types";

const ERRORS: Record<string, string> = {
  user_limit: "You've used today's questions for this demo. Please come back tomorrow.",
  daily_limit: "The demo has reached today's question limit. Please come back tomorrow.",
  qa_disabled: "Ask the filings isn't switched on for this deployment.",
  model_busy: "The model is busy right now. Please try again in a minute.",
};

export function AskFilings({
  cik,
  name,
  asOf,
  suggestions,
}: {
  cik: string;
  name: string;
  asOf: string;
  suggestions: string[];
}) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<QaAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function ask(q: string) {
    if (q.trim().length < 3 || busy) return;
    setQuestion(q);
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${await accessToken()}` },
        body: JSON.stringify({ cik, question: q, as_of: asOf }),
      });
      const body = await res.json();
      if (!res.ok) setError(ERRORS[body.error] ?? "Something went wrong. Please try again.");
      else setAnswer(body as QaAnswer);
    } catch {
      setError("Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          maxLength={500}
          placeholder={`Ask about ${name}'s filings up to ${fmtDate(asOf)}`}
          className="h-9 min-w-0 flex-1 rounded-md border bg-card px-3 text-sm outline-none focus:ring-2 focus:ring-ring/40"
        />
        <Button type="submit" disabled={busy || question.trim().length < 3}>
          {busy ? <Loader2 className="animate-spin" /> : <Send />} Ask
        </Button>
      </form>
      <div className="flex flex-wrap gap-2">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => void ask(s)}
            disabled={busy}
            className="rounded-full border bg-card px-3 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            {s}
          </button>
        ))}
      </div>
      {error && <p className="text-sm text-risk">{error}</p>}
      {answer && (
        <div className="space-y-3 rounded-lg border bg-surface-2 p-4">
          <p className={answer.answerable ? "text-sm" : "text-sm text-muted-foreground"}>{answer.answer}</p>
          {answer.citations.length > 0 && (
            <ol className="space-y-2">
              {answer.citations.map((c, i) => (
                <li key={i} className="text-xs">
                  <blockquote className="border-l-2 border-brand/50 pl-3 text-muted-foreground">
                    &ldquo;{c.quote}&rdquo;
                  </blockquote>
                  <a
                    href={c.url}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-1 ml-3 inline-flex items-center gap-1 text-brand hover:underline"
                  >
                    {c.form_type} filed {fmtDate(c.filed_date)}, {SECTIONS[c.section] ?? c.section}
                    <ExternalLink className="size-3" />
                  </a>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Answers come only from this company&apos;s 10-K and 10-Q text public on {fmtDate(asOf)} (from mid-2022), each
        claim quoted and linked. A few questions per visitor per day.
      </p>
    </div>
  );
}
