"""Sample passages for the text-signal eval set -> evals/text_signals_candidates.jsonl
(then labelled into evals/text_signals.jsonl and deleted).

Labels are added by hand (by reading each passage against config text_signals.yaml) into
evals/text_signals.jsonl; this script only chooses the passages, deterministically:
  - 12 per signal type from paragraphs matching that type's keywords (so each type has
    positives, and keyword false positives such as hypothetical risk-factor language), and
  - 40 random paragraphs from MD&A and risk factors (mostly negatives).

    uv run python scripts/build_text_signal_eval.py   (needs data/llm/sections.parquet)
"""

from __future__ import annotations

import json
import random
import re

import polars as pl

from tripwire_common.settings import REPO_ROOT, get_settings

PER_TYPE = 12
RANDOM = 40
MAX_CHARS = 1_600
SEED = 11

HINTS = {
    "liquidity_concern": r"liquidity.{0,200}(not be sufficient|insufficient|preserve|"
    r"substantial doubt|unable to (meet|fund)|draw(n|ing) down)",
    "covenant_breach": r"covenant.{0,200}(waiver|amend|breach|not in compliance|forbearance|"
    r"default)",
    "going_concern": r"going concern",
    "refinancing_risk": r"(matur\w+).{0,200}refinanc|refinanc.{0,200}matur",
    "customer_concentration": r"(customer|client)s?.{0,120}(accounted for|represented)\s.{0,40}%"
    r"|\d+%.{0,80}(of (our )?(net )?(sales|revenue)).{0,80}customer",
}


def windows(text: str) -> list[str]:
    """Paragraph windows of up to MAX_CHARS (a paragraph plus its neighbours)."""
    paras = [p for p in text.split("\n") if len(p) > 120]
    out = []
    for i, p in enumerate(paras):
        w = p
        j = i + 1
        while j < len(paras) and len(w) + len(paras[j]) < MAX_CHARS:
            w = f"{w}\n{paras[j]}"
            j += 1
        out.append(w[:MAX_CHARS])
    return out


def main() -> None:
    rng = random.Random(SEED)  # noqa: S311 - sampling, not security
    sections = pl.read_parquet(get_settings().tripwire_data_dir / "llm" / "sections.parquet")
    rows = list(sections.iter_rows(named=True))
    rng.shuffle(rows)
    chosen: list[dict[str, object]] = []
    used: set[str] = set()

    def add(r: dict[str, object], text: str, hint: str) -> None:
        key = text[:200]
        if key in used:
            return
        used.add(key)
        chosen.append({
            "id": len(chosen) + 1, "cik": r["cik"], "company": r["company"],
            "accession": r["accession"], "form": r["form"], "filed_date": str(r["filed_date"]),
            "section": r["section"], "hint": hint, "text": text,
        })  # fmt: skip

    for kind, pattern in HINTS.items():
        rx = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        found = 0
        companies: dict[object, int] = {}
        for r in rows:
            if found == PER_TYPE:
                break
            if companies.get(r["cik"], 0) >= 2:  # spread across companies
                continue
            for w in windows(str(r["text"])):
                if rx.search(w):
                    add(r, w, kind)
                    found += 1
                    companies[r["cik"]] = companies.get(r["cik"], 0) + 1
                    break
    pool = [r for r in rows if r["section"] in ("mdna", "risk_factors")]
    while sum(c["hint"] == "random" for c in chosen) < RANDOM:
        r = rng.choice(pool)
        ws = windows(str(r["text"]))
        if ws:
            add(r, rng.choice(ws), "random")

    path = REPO_ROOT / "evals" / "text_signals_candidates.jsonl"
    path.write_text("".join(json.dumps(c) + "\n" for c in chosen))
    print(f"{len(chosen)} passages -> {path}")


if __name__ == "__main__":
    main()
