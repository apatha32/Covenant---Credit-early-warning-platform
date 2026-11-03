"""LLM layer commands (Phase 5). Scope: the demo portfolio's 10-K / 10-Q filings.

    uv run python -m tripwire_llm sections            # split filings into sections, Q&A chunks
    uv run python -m tripwire_llm estimate            # token count and cost of pending extraction
    uv run python -m tripwire_llm signals --submit    # Batch API extraction (refuses over budget)
    uv run python -m tripwire_llm signals --collect   # wait for the batch, verify, load signals
    uv run python -m tripwire_llm eval                # evals/text_signals.jsonl on each eval model

Filing documents are cached in the local data directory (they are too large for the hosted
Storage budget). DATABASE_URL picks the target database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import anthropic
import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from tripwire_common.config import (
    DomainPack,
    ModelsConfig,
    SourcesConfig,
    TextSignals,
    load_demo_portfolio,
    load_domain_pack,
    load_models_config,
    load_sources_config,
    load_text_signals,
)
from tripwire_common.db import connect
from tripwire_common.logging import configure_logging
from tripwire_common.runs import RunLog, ingest_run
from tripwire_common.settings import REPO_ROOT, Settings, get_settings
from tripwire_common.storage import LocalStorage
from tripwire_ingest.sec import sec_client
from tripwire_llm import signals as sig
from tripwire_llm.sections import find_sections, html_to_paragraphs, split_paragraphs
from tripwire_ml.documents import fetch_document

log = logging.getLogger(__name__)

EVAL_PATH = REPO_ROOT / "evals" / "text_signals.jsonl"
EVAL_REPORT = REPO_ROOT / "reports" / "text_signal_eval.md"
FORMS = ("10-K", "10-Q")


@dataclass(frozen=True)
class Context:
    conn: psycopg.Connection
    settings: Settings
    sources: SourcesConfig
    pack: DomainPack
    config: TextSignals
    models: ModelsConfig

    @property
    def workdir(self) -> Path:
        path = self.settings.tripwire_data_dir / "llm"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def sections_path(self) -> Path:
        return self.workdir / "sections.parquet"


SECTION_SCHEMA = {"cik": pl.String, "accession": pl.String, "form": pl.String,
                  "filed_date": pl.Date, "company": pl.String, "section": pl.String,
                  "part": pl.Int32, "text": pl.String}  # fmt: skip


# --- sections ---


def _filings(ctx: Context, ciks: list[str], since: dt.date) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        """select f.cik, f.accession, f.form_type, f.filed_date, f.primary_document, c.name
           from staging.filings f join curated.company c using (cik)
           where f.cik = any(%s) and f.form_type = any(%s) and f.filed_date >= %s
             and f.primary_document is not null
           order by f.cik, f.filed_date""",
        [ciks, list(FORMS), since],
    ).fetchall()
    keys = ["cik", "accession", "form", "filed_date", "document", "company"]
    return [dict(zip(keys, r, strict=True)) for r in rows]


def cmd_sections(ctx: Context, ciks: list[str]) -> None:
    done: pl.DataFrame = (
        pl.read_parquet(ctx.sections_path) if ctx.sections_path.exists()
        else pl.DataFrame(schema=SECTION_SCHEMA)
    )  # fmt: skip
    seen = set(done["accession"].unique())
    storage = LocalStorage(ctx.settings.tripwire_data_dir)
    rows: list[dict[str, Any]] = []
    with (
        sec_client(ctx.settings, ctx.sources.sec) as client,
        ingest_run(ctx.conn, "llm_sections", [client]) as run,
    ):
        filings = _filings(ctx, ciks, ctx.config.signals_since)
        run.counts["filings"] = len(filings)
        for f in filings:
            if f["accession"] in seen:
                run.counts["already_split"] += 1
                continue
            try:
                body = fetch_document(None, storage, client, ctx.sources.sec, f["cik"],
                                      f["accession"], f["document"], run)  # fmt: skip
                if body is None:
                    continue
                text = html_to_paragraphs(body)
                wanted = ctx.config.sections[f["form"]]
                found, missing = find_sections(text, f["form"], wanted)
                for name in missing:
                    _quarantine(ctx.conn, "section_not_found", {**_lineage(f), "section": name},
                                run)  # fmt: skip
                for s in found:
                    parts = split_paragraphs(s.text, ctx.config.max_section_chars)
                    for i, part in enumerate(parts):
                        rows.append({**_lineage(f), "company": f["company"], "section": s.name,
                                     "part": i, "text": part})  # fmt: skip
                    run.counts[f"section_{s.name}"] += 1
                run.counts["filings_split"] += 1
            except Exception as exc:
                run.item_failed(f"{f['cik']} {f['accession']}", exc)
        new = pl.DataFrame(rows, schema=SECTION_SCHEMA)
        allsec = pl.concat([done, new])
        allsec.write_parquet(ctx.sections_path)
        run.counts["chunks_inserted"] = _load_chunks(ctx, allsec)


def _lineage(f: dict[str, Any]) -> dict[str, Any]:
    return {"cik": f["cik"], "accession": f["accession"], "form": f["form"],
            "filed_date": f["filed_date"]}  # fmt: skip


def _quarantine(conn: psycopg.Connection, rule: str, record: dict[str, Any], run: RunLog) -> None:
    conn.execute(
        """insert into staging.quarantine (source, record, rule_violated, run_id)
           values ('llm', %s, %s, %s) on conflict do nothing""",
        [Jsonb(json.loads(json.dumps(record, default=str))), rule, run.id],
    )
    run.counts[f"quarantined_{rule}"] += 1


def _load_chunks(ctx: Context, sections: pl.DataFrame) -> int:
    """Q&A chunks for filings since chunks_since (Postgres storage budget)."""
    recent = sections.filter(pl.col("filed_date") >= ctx.config.chunks_since).sort(
        "cik", "accession", "section", "part"
    )
    rows = []
    for (cik, acc, form, filed, section), group in recent.group_by(
        "cik", "accession", "form", "filed_date", "section", maintain_order=True
    ):
        text = "\n".join(group["text"])
        for i, chunk in enumerate(split_paragraphs(text, ctx.config.chunk_chars)):
            rows.append((cik, acc, form, filed, section, i, chunk))
    existing = {(r[0], r[1], r[2]) for r in ctx.conn.execute(
        """select accession, section, chunk_index from curated.filing_chunk
           where cik = any(%s)""", [list({r[0] for r in rows})])}  # fmt: skip
    missing = [r for r in rows if (r[1], r[4], r[5]) not in existing]
    inserted = 0
    for start in range(0, len(missing), CHUNK_BATCH):
        inserted += _insert_with_retry(missing[start : start + CHUNK_BATCH])
    return inserted


CHUNK_BATCH = 50  # rows per write: large writes over a flaky TLS link get cut off


def _insert_with_retry(rows: list[tuple[Any, ...]], attempts: int = 4) -> int:
    """Insert on a fresh connection, retrying when the link drops (the insert is idempotent)."""
    for attempt in range(attempts):
        try:
            with connect(autocommit=True) as conn, conn.cursor() as cur:
                cur.executemany(
                    """insert into curated.filing_chunk (cik, accession, form_type, filed_date,
                         section, chunk_index, chunk_text)
                       values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing""",
                    rows,
                )
                return max(cur.rowcount, 0)
        except psycopg.OperationalError:
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt)
    return 0


# --- extraction ---


def _pending(ctx: Context) -> pl.DataFrame:
    """Section parts without a cached response for the extraction model + prompt version."""
    sections = pl.read_parquet(ctx.sections_path)
    model = ctx.models.llm.extraction_default
    cached = {(r[0], r[1]) for r in ctx.conn.execute(
        "select accession, section from staging.llm_cache where prompt_version = %s and model = %s",
        [ctx.config.prompt_version, model])}  # fmt: skip
    keep = [(r["accession"], f"{r['section']}#{r['part']}") not in cached
            for r in sections.iter_rows(named=True)]  # fmt: skip
    return sections.filter(pl.Series(keep, dtype=pl.Boolean))


def _message(r: dict[str, Any]) -> str:
    return sig.user_message(r["company"], r["form"], str(r["filed_date"]), r["section"], r["text"])


def _custom_id(r: dict[str, Any]) -> str:
    return f"{r['accession']}_{r['section']}_{r['part']}"  # [A-Za-z0-9_-], under 64 chars


def _spent(ctx: Context) -> float:
    total = 0.0
    for model, tin, tout, batch in ctx.conn.execute(
        """select model, coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0),
                  response ? 'batch'
           from staging.llm_cache group by model, response ? 'batch'"""
    ):
        if model in ctx.models.llm.prices:
            total += sig.cost_usd(ctx.models.llm.prices, model, tin, tout, batch=bool(batch))
    return total


def cmd_estimate(ctx: Context, client: anthropic.Anthropic | None) -> float:
    pending = _pending(ctx)
    model = ctx.models.llm.extraction_default
    system = sig.system_prompt(ctx.config)
    if client is not None and pending.height:
        # Exact count on a sample, scaled by characters (count_tokens is free but rate-limited).
        sample = pending.sample(min(20, pending.height), seed=7)
        tokens = sum(
            client.messages.count_tokens(model=model, system=system,
                                         messages=[{"role": "user", "content": _message(r)}]
                                         ).input_tokens
            for r in sample.iter_rows(named=True)
        )  # fmt: skip
        chars = sum(len(_message(r)) + len(system) for r in sample.iter_rows(named=True))
        per_char = tokens / chars
    else:
        per_char = 1 / 3.8  # rough English prose ratio, until a key is available
    total_chars = sum(len(_message(r)) + len(system) for r in pending.iter_rows(named=True))
    input_tokens = int(total_chars * per_char)
    output_tokens = pending.height * 600  # quotes + JSON + low-effort thinking, measured on eval
    usd = sig.cost_usd(ctx.models.llm.prices, model, input_tokens, output_tokens, batch=True)
    spent = _spent(ctx)
    print(f"pending section parts: {pending.height} ({pending['accession'].n_unique()} filings)")
    print(f"input tokens ~{input_tokens:,} ({'counted sample' if client else 'heuristic'}), "
          f"output ~{output_tokens:,}")  # fmt: skip
    print(f"estimated cost (Batch API, {model}): ${usd:.2f}; spent so far ${spent:.2f}; "
          f"budget ${ctx.config.budget_usd:.2f}")  # fmt: skip
    return usd


def cmd_submit(ctx: Context, client: anthropic.Anthropic) -> None:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    estimate = cmd_estimate(ctx, client)
    if _spent(ctx) + estimate > ctx.config.budget_usd:
        raise SystemExit("refusing: estimate would exceed budget_usd in text_signals.yaml")
    pending = _pending(ctx)
    if pending.height == 0:
        print("nothing to extract")
        return
    model = ctx.models.llm.extraction_default
    types = list(ctx.config.signals)
    requests = [
        Request(custom_id=_custom_id(r), params=MessageCreateParamsNonStreaming(
            **sig.request_params(ctx.config, model, types, _message(r))))  # type: ignore[typeddict-item]
        for r in pending.iter_rows(named=True)
    ]  # fmt: skip
    known = _recorded_batch_ids(ctx)
    for start in range(0, len(requests), SUBMIT_CHUNK):
        chunk = requests[start : start + SUBMIT_CHUNK]
        batch_id = _create_batch(client, chunk, known)
        known.add(batch_id)
        _record_batch(ctx, {"id": batch_id, "model": model, "requests": len(chunk),
                            "prompt_version": ctx.config.prompt_version,
                            "created": dt.datetime.now(dt.UTC).isoformat()})  # fmt: skip
        print(f"submitted batch {batch_id} ({start + len(chunk)}/{len(requests)} requests)")


SUBMIT_CHUNK = 50  # requests per batch (~3 MB): large uploads get cut off on flaky links


def _recorded_batch_ids(ctx: Context) -> set[str]:
    path = ctx.workdir / "batches.jsonl"
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text().splitlines() if line.strip()}


def _create_batch(
    client: anthropic.Anthropic, requests: list[Any], known: set[str], attempts: int = 4
) -> str:
    """Create one batch. If the upload errors, look for a batch that was created anyway (same
    request count, created since this attempt began, not one we already recorded) before trying
    again, so nothing is paid for twice."""
    for attempt in range(attempts):
        started = dt.datetime.now(dt.UTC)
        try:
            return client.messages.batches.create(requests=requests).id
        except anthropic.APIConnectionError:
            time.sleep(5)
            for b in client.messages.batches.list(limit=10):
                total = sum(dict(b.request_counts).values())
                if b.id not in known and b.created_at >= started and total == len(requests):
                    return b.id
            if attempt == attempts - 1:
                raise
    raise AssertionError("unreachable")


def _record_batch(ctx: Context, meta: dict[str, Any]) -> None:
    with (ctx.workdir / "batches.jsonl").open("a") as fh:
        fh.write(json.dumps(meta) + "\n")


def cmd_collect(ctx: Context, client: anthropic.Anthropic, batch_id: str | None) -> None:
    """Load every submitted batch not fully loaded yet (or just `batch_id`). Results already in
    staging.llm_cache are skipped, so this can be re-run after any interruption. Each batch gets a
    fresh database connection (long runs over a flaky link lose them)."""
    batches = [json.loads(line) for line in (ctx.workdir / "batches.jsonl").read_text().split("\n")
               if line]  # fmt: skip
    done_path = ctx.workdir / "batches_collected.txt"
    done = set(done_path.read_text().split()) if done_path.exists() else set()
    unique = list({b["id"]: b for b in batches}.values())
    todo = [b for b in unique if (b["id"] == batch_id if batch_id else b["id"] not in done)]
    sections = {_custom_id(r): r for r in pl.read_parquet(ctx.sections_path).iter_rows(named=True)}
    allowed = set(ctx.config.signals)
    for meta in todo:
        while (b := client.messages.batches.retrieve(meta["id"])).processing_status != "ended":
            log.info("batch running", extra={"id": b.id, "processing": b.request_counts.processing})
            time.sleep(60)
        results = list(client.messages.batches.results(meta["id"]))
        for attempt in range(4):
            try:
                with connect(autocommit=True) as conn:
                    failed = _load_batch(replace(ctx, conn=conn), meta, results, sections, allowed)
                break
            except psycopg.OperationalError:
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
        if failed == 0:
            with done_path.open("a") as fh:
                fh.write(meta["id"] + "\n")
        print(f"loaded batch {meta['id']} ({failed} failed)")


def _load_batch(ctx: Context, meta: dict[str, Any], results: list[Any],
                sections: dict[str, dict[str, Any]], allowed: set[str]) -> int:  # fmt: skip
    cached = {(r[0], r[1]) for r in ctx.conn.execute(
        """select accession, section from staging.llm_cache
           where prompt_version = %s and model = %s""",
        [meta["prompt_version"], meta["model"]])}  # fmt: skip
    with ingest_run(ctx.conn, "llm_signals") as run:
        run.counts["batch_requests"] = meta["requests"]
        for result in results:
            r = sections.get(result.custom_id)
            if r is None or result.result.type != "succeeded":
                run.item_failed(result.custom_id, RuntimeError(result.result.type))
                continue
            if (r["accession"], f"{r['section']}#{r['part']}") in cached:
                run.counts["already_loaded"] += 1
                continue
            msg = result.result.message
            try:
                text = next(b.text for b in msg.content if b.type == "text")
                raw = sig.parse_response_text(text)
            except (StopIteration, ValueError) as exc:  # malformed response: skip, record
                run.item_failed(result.custom_id, exc)
                continue
            usage = msg.usage
            _store(ctx, run, r, meta["model"], raw, usage.input_tokens, usage.output_tokens,
                   allowed, batch=True)  # fmt: skip
        return int(run.counts["failed"])


def _store(ctx: Context, run: RunLog, r: dict[str, Any], model: str, raw: dict[str, Any],
           tokens_in: int, tokens_out: int, allowed: set[str], batch: bool) -> None:  # fmt: skip
    checked = sig.check_evidence(raw, r["text"], allowed)
    section_key = f"{r['section']}#{r['part']}"
    with ctx.conn.transaction():
        ctx.conn.execute(
            """insert into staging.llm_cache (cik, accession, section, prompt_version, model,
                 response, input_tokens, output_tokens)
               values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing""",
            [r["cik"], r["accession"], section_key, ctx.config.prompt_version, model,
             Jsonb({**raw, **({"batch": True} if batch else {})}), tokens_in, tokens_out],
        )  # fmt: skip
        for s in checked.kept:
            ctx.conn.execute(
                """insert into curated.text_signal (cik, accession, filed_date, section,
                     signal_type, value, evidence_quote, confidence, prompt_version, model)
                   values (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s) on conflict do nothing""",
                [r["cik"], r["accession"], r["filed_date"], r["section"], s["signal_type"],
                 s["evidence_quote"], s["confidence"], ctx.config.prompt_version, model],
            )  # fmt: skip
        for s in checked.rejected:
            _quarantine(ctx.conn, f"signal_{s['reason']}",
                        {"cik": r["cik"], "accession": r["accession"], "section": section_key,
                         "model": model, **s}, run)  # fmt: skip
    run.counts["signals_kept"] += len(checked.kept)
    run.counts["signals_rejected"] += len(checked.rejected)
    run.counts["responses"] += 1


# --- eval ---


RESULTS_DIR = REPO_ROOT / "evals" / "results"


def cmd_eval(ctx: Context, client: anthropic.Anthropic, models: list[str]) -> None:
    """Run the current prompt version on the eval set for each model, save one results file per
    (prompt version, model), and re-render the report from every saved run."""
    from tripwire_llm.evaluate import render

    passages = [json.loads(line) for line in EVAL_PATH.read_text().splitlines() if line.strip()]
    types = list(ctx.config.signals)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for model in models:
        predictions, tokens_in, tokens_out, rejected = [], 0, 0, 0
        for p in passages:
            message = sig.user_message(p["company"], p["form"], p["filed_date"], "passage",
                                       p["text"])  # fmt: skip
            msg = client.messages.create(**sig.request_params(ctx.config, model, types, message))
            tokens_in += msg.usage.input_tokens
            tokens_out += msg.usage.output_tokens
            text = next((b.text for b in msg.content if b.type == "text"), '{"signals": []}')
            checked = sig.check_evidence(sig.parse_response_text(text), p["text"], set(types))
            rejected += len(checked.rejected)
            predictions.append({"id": p["id"], "rejected": len(checked.rejected),
                                "predicted": [s["signal_type"] for s in checked.kept]})  # fmt: skip
        run = {"prompt_version": ctx.config.prompt_version, "model": model,
               "date": dt.date.today().isoformat(), "input_tokens": tokens_in,
               "output_tokens": tokens_out, "rejected_quotes": rejected,
               "cost_usd": sig.cost_usd(ctx.models.llm.prices, model, tokens_in, tokens_out),
               "predictions": predictions}  # fmt: skip
        path = RESULTS_DIR / f"{ctx.config.prompt_version}__{model}.json"
        path.write_text(json.dumps(run, indent=1))
        print(f"{path.name}: ${run['cost_usd']:.3f}")
    runs = [json.loads(f.read_text()) for f in sorted(RESULTS_DIR.glob("*.json"))]
    EVAL_REPORT.parent.mkdir(exist_ok=True)
    EVAL_REPORT.write_text(render(ctx.config, passages, runs, dt.date.today()))
    print(f"report written: {EVAL_REPORT}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tripwire_llm",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["sections", "estimate", "signals", "eval"])
    parser.add_argument("--cik", type=int, nargs="+", help="sections: these CIKs (default: demo)")
    parser.add_argument("--submit", action="store_true", help="signals: submit a batch")
    parser.add_argument("--collect", action="store_true", help="signals: load batch results")
    parser.add_argument("--batch", help="signals --collect: batch id (default: the latest)")
    parser.add_argument("--models", nargs="+", help="eval: models to run (default: eval_models)")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging("llm")
    pack = load_domain_pack()
    ciks = [f"{c:010d}" for c in args.cik] if args.cik else load_demo_portfolio(pack).ciks()
    key = settings.anthropic_api_key
    client = anthropic.Anthropic(api_key=key.get_secret_value()) if key else None
    with connect(autocommit=True) as conn:
        ctx = Context(conn, settings, load_sources_config(), pack, load_text_signals(pack),
                      load_models_config())  # fmt: skip
        if args.command == "sections":
            cmd_sections(ctx, ciks)
        elif args.command == "estimate":
            cmd_estimate(ctx, client)
        else:
            if client is None:
                raise SystemExit("ANTHROPIC_API_KEY is not set (see .env.example)")
            if args.command == "eval":
                cmd_eval(ctx, client, args.models or ctx.models.llm.eval_models)
            elif args.submit:
                cmd_submit(ctx, client)
            elif args.collect:
                cmd_collect(ctx, client, args.batch)
            else:
                parser.error("signals needs --submit or --collect")
