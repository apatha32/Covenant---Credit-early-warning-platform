import Anthropic from "@anthropic-ai/sdk";
import { createClient } from "@supabase/supabase-js";
import { NextResponse } from "next/server";
import { edgarFiling } from "@/lib/format";

// "Ask the filings": keyword retrieval over one company's filings (as public on the as-of date),
// then Claude answers from those excerpts only, citing them. Runs on the server: the Anthropic
// key never reaches the browser. The caller's Supabase session is forwarded, so retrieval runs
// under the same row-level security as the rest of the console.

export const runtime = "nodejs";
export const maxDuration = 60;

const QA_MODEL = process.env.QA_MODEL ?? "claude-sonnet-5-5"; // config/models.yaml llm.qa
const MAX_EXCERPTS = 8;
// ts_rank_cd(..., 32) is in [0, 1). Below this, the filings do not discuss the question well
// enough to answer it, so we refuse without calling the model.
const MIN_RANK = 0.05;

const STOPWORDS = new Set(
  (
    "a an and are as at be been by can could did do does for from had has have how in is it its " +
    "of on or should so than that the their them then there these they this to was we were what " +
    "when where which who why will with would about any company companys tell me say said"
  ).split(" "),
);

type Excerpt = {
  id: number;
  accession: string;
  form_type: string | null;
  filed_date: string;
  section: string;
  chunk_text: string;
  rank: number;
};

type Citation = { excerpt: number; quote: string };
type ModelAnswer = { answerable: boolean; answer: string; citations: Citation[] };

const SCHEMA = {
  type: "object",
  properties: {
    answerable: { type: "boolean" },
    answer: { type: "string" },
    citations: {
      type: "array",
      items: {
        type: "object",
        properties: { excerpt: { type: "integer" }, quote: { type: "string" } },
        required: ["excerpt", "quote"],
        additionalProperties: false,
      },
    },
  },
  required: ["answerable", "answer", "citations"],
  additionalProperties: false,
} as const;

const SYSTEM = `You answer a credit analyst's question about one company using only the numbered \
excerpts from its SEC filings that follow the question. Do not use outside knowledge.

- If the excerpts do not contain the answer, set answerable to false and say briefly what is \
missing.
- Otherwise answer in 2-5 plain sentences, and support every claim with citations: the excerpt \
number and a short quote copied character for character from that excerpt (no ellipses, no edits).
- Mention the filing (form and date) when it matters, e.g. "In its 10-Q filed 2024-05-06, ...".`;

function normalize(text: string): string {
  return text.replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[‑–—]/g, "-").replace(/\s+/g, " ").trim();
}

function keywords(question: string): string {
  const words = question
    .toLowerCase()
    .replace(/[^a-z0-9&\s-]/g, " ")
    .split(/\s+/)
    .filter((w) => w.length > 2 && !STOPWORDS.has(w));
  return [...new Set(words)].slice(0, 12).join(" or ");
}

function json(body: unknown, status = 200) {
  return NextResponse.json(body, { status });
}

export async function POST(request: Request) {
  const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!token || !url || !anonKey) return json({ error: "not_signed_in" }, 401);
  if (!process.env.ANTHROPIC_API_KEY) return json({ error: "qa_disabled" }, 503);

  const body = (await request.json().catch(() => null)) as {
    cik?: string;
    question?: string;
    as_of?: string;
  } | null;
  const cik = body?.cik ?? "";
  const question = (body?.question ?? "").trim();
  const asOf = body?.as_of ?? "";
  if (!/^\d{10}$/.test(cik) || !/^\d{4}-\d{2}-\d{2}$/.test(asOf) || question.length < 3 || question.length > 500) {
    return json({ error: "bad_request" }, 400);
  }

  const sb = createClient(url, anonKey, {
    db: { schema: "api" },
    global: { headers: { Authorization: `Bearer ${token}` } },
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const begin = await sb.rpc("qa_begin", { p_cik: cik, p_question: question, p_as_of: asOf });
  if (begin.error) return json({ error: "qa_begin_failed", detail: begin.error.message }, 500);
  const started = begin.data as { id?: number; cached?: boolean; answer?: unknown; error?: string; limit?: number };
  if (started.error) return json({ error: started.error, limit: started.limit }, 429);
  if (started.cached) return json({ ...(started.answer as object), cached: true });

  const finish = (answer: object, model: string | null) =>
    sb.rpc("qa_finish", { p_id: started.id, p_answer: answer, p_model: model });

  const query = keywords(question);
  const search = query
    ? await sb.rpc("search_filings", { p_cik: cik, p_query: query, p_as_of: asOf, p_limit: MAX_EXCERPTS })
    : { data: [], error: null };
  if (search.error) return json({ error: "search_failed", detail: search.error.message }, 500);
  const excerpts = (search.data ?? []) as Excerpt[];

  if (excerpts.length === 0 || excerpts[0].rank < MIN_RANK) {
    const answer = {
      answerable: false,
      answer:
        "I couldn't find passages in this company's filings (as public on this date) that address that question, so I won't guess.",
      citations: [],
    };
    await finish(answer, null);
    return json(answer);
  }

  const context = excerpts
    .map(
      (e, i) =>
        `<excerpt number="${i + 1}" form="${e.form_type ?? ""}" filed="${e.filed_date}" section="${e.section}">\n${e.chunk_text}\n</excerpt>`,
    )
    .join("\n\n");

  const client = new Anthropic();
  let parsed: ModelAnswer;
  let servedBy = QA_MODEL;
  try {
    const message = await client.beta.messages.create({
      model: QA_MODEL,
      max_tokens: 4000,
      system: SYSTEM,
      betas: ["server-side-fallback-2026-07-01"],
      fallbacks: "default",
      output_config: { effort: "low", format: { type: "json_schema", schema: SCHEMA } },
      messages: [{ role: "user", content: `Question: ${question}\n\n${context}` }],
    });
    servedBy = message.model;
    if (message.stop_reason === "refusal") {
      const answer = { answerable: false, answer: "This question can't be answered here.", citations: [] };
      await finish(answer, servedBy);
      return json(answer);
    }
    const text = message.content.find((b) => b.type === "text");
    parsed = JSON.parse(text && text.type === "text" ? text.text : "{}") as ModelAnswer;
  } catch (error) {
    if (error instanceof Anthropic.RateLimitError) return json({ error: "model_busy" }, 503);
    if (error instanceof Anthropic.APIError) return json({ error: "model_error", status: error.status }, 502);
    return json({ error: "model_output_invalid" }, 502);
  }

  // Keep only citations whose quote really appears in the excerpt they name.
  const citations = (parsed.citations ?? [])
    .map((c) => ({ c, e: excerpts[c.excerpt - 1] }))
    .filter(({ c, e }) => e && normalize(c.quote).length >= 10 && normalize(e.chunk_text).includes(normalize(c.quote)))
    .map(({ c, e }) => ({
      quote: normalize(c.quote),
      accession: e.accession,
      form_type: e.form_type,
      filed_date: e.filed_date,
      section: e.section,
      url: edgarFiling(cik, e.accession),
    }));

  const answer =
    parsed.answerable && citations.length === 0
      ? {
          answerable: false,
          answer: "I found related passages but couldn't verify an answer against them, so I won't guess.",
          citations: [],
        }
      : { answerable: Boolean(parsed.answerable), answer: parsed.answer ?? "", citations };
  await finish(answer, servedBy);
  return json(answer);
}
