import { readFileSync } from "node:fs";
import path from "node:path";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export const dynamic = "force-static";

export const metadata = { title: "Covenant · Model card" };

// The Phase 4 model card exactly as `python -m tripwire_ml train` wrote it (read at build time).
const CARD = path.join(process.cwd(), "..", "..", "reports", "model_eval.md");

export default function ModelCardPage() {
  const markdown = readFileSync(CARD, "utf8");
  return (
    <article className="prose-card max-w-4xl pt-8">
      <p className="mb-4 rounded-lg border bg-surface-2 px-4 py-3 text-sm text-muted-foreground">
        Rendered unchanged from{" "}
        <a
          className="text-brand hover:underline"
          href="https://github.com/apatha32/Covenant---Credit-early-warning-platform/blob/main/reports/model_eval.md"
        >
          reports/model_eval.md
        </a>
        . The demo serves the active model below (LightGBM, version 6).
      </p>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children }) => (
            <div className="table-wrap">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {markdown}
      </ReactMarkdown>
    </article>
  );
}
