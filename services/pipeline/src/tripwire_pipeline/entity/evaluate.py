"""Score the matcher against a labeled borrower file (expected_cik per row)."""

from __future__ import annotations

import csv
import gzip
import io
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from tripwire_pipeline.entity.matcher import Company, MatchResult, Universe


def load_universe_fixture(path: Path) -> list[Company]:
    text = gzip.decompress(path.read_bytes()).decode()
    return [
        Company(
            cik=r["cik"],
            name=r["name"],
            tickers=tuple(t for t in r["tickers"].split("|") if t),
            former_names=tuple(f for f in r["former_names"].split("|") if f),
            is_excluded=r["is_excluded"] == "True",
        )
        for r in csv.DictReader(io.StringIO(text))
    ]


def load_labeled(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass(frozen=True)
class Scored:
    row: dict[str, str]
    result: MatchResult

    @property
    def expected(self) -> str | None:
        return self.row["expected_cik"] or None

    @property
    def correct_auto(self) -> bool:
        return self.result.status == "auto_matched" and self.result.cik == self.expected

    @property
    def wrong_auto(self) -> bool:
        return self.result.status == "auto_matched" and self.result.cik != self.expected

    @property
    def answer_in_candidates(self) -> bool:
        return self.expected is not None and any(
            c.cik == self.expected for c in self.result.candidates
        )


@dataclass(frozen=True)
class Evaluation:
    scored: list[Scored]
    threshold: float

    @property
    def auto(self) -> list[Scored]:
        return [s for s in self.scored if s.result.status == "auto_matched"]

    @property
    def review(self) -> list[Scored]:
        return [s for s in self.scored if s.result.status == "needs_review"]

    @property
    def precision(self) -> float:
        return sum(s.correct_auto for s in self.auto) / len(self.auto) if self.auto else 1.0

    @property
    def auto_rate(self) -> float:
        return len(self.auto) / len(self.scored)


def evaluate(universe: Universe, rows: list[dict[str, str]]) -> Evaluation:
    scored = [Scored(r, universe.match(r["borrower_name"] or None, r["ticker"] or None))
              for r in rows]  # fmt: skip
    return Evaluation(scored, universe.config.auto_match_min_confidence)


def render(evaluation: Evaluation, title: str) -> str:
    ev = evaluation
    review_with_answer = [s for s in ev.review if s.expected]
    found = sum(s.answer_in_candidates for s in review_with_answer)
    lines = [
        f"# Entity resolution: {title}",
        "",
        f"- Rows: {len(ev.scored)}; auto-match threshold: {ev.threshold}",
        f"- **Auto-match precision: {ev.precision:.1%}** ({sum(s.correct_auto for s in ev.auto)}"
        f" correct of {len(ev.auto)} auto-matched)",
        f"- Auto-matched: {ev.auto_rate:.0%} of rows; sent to review: {len(ev.review)}",
        f"- Review rows whose correct company is among the suggested candidates: "
        f"{found}/{len(review_with_answer)}",
        f"- Non-SEC rows auto-matched (must be 0): {sum(1 for s in ev.auto if s.expected is None)}",
        "",
        "| Case | Rows | Auto correct | Auto wrong | Review |",
        "|---|---|---|---|---|",
    ]
    by_case: dict[str, Counter[str]] = defaultdict(Counter)
    for s in ev.scored:
        c = by_case[s.row["case"]]
        c["rows"] += 1
        c["auto_correct"] += s.correct_auto
        c["auto_wrong"] += s.wrong_auto
        c["review"] += s.result.status == "needs_review"
    for case, c in sorted(by_case.items()):
        lines.append(f"| {case} | {c['rows']} | {c['auto_correct']} | {c['auto_wrong']} "
                     f"| {c['review']} |")  # fmt: skip
    wrong = [s for s in ev.auto if not s.correct_auto]
    lines += ["", "## Wrong auto-matches", ""]
    lines += [f"- `{s.row['borrower_name']}` / `{s.row['ticker']}` -> {s.result.cik} "
              f"({s.result.method}, {s.result.confidence}); expected {s.expected or 'no match'}"
              for s in wrong] or ["None."]  # fmt: skip
    return "\n".join(lines) + "\n"
