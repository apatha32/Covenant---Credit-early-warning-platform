"""Matching cascade for one uploaded borrower row (pure, no I/O).

Order (first decisive step wins):
  0. prior decision  - a human already resolved this exact input for this tenant
  1. ticker          - exact ticker; cross-checked against the name when one is given
  2. exact name      - normalized name equals a current or former company name
  3. fuzzy name      - rapidfuzz similarity against current + former names

Every step yields a confidence in [0, 1]. Rows below the auto-match threshold go to review with
their top candidates; conflicts (ticker says one company, name another) and ambiguity (several
companies equally close) are always capped below the threshold.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz, process

from tripwire_common.config import EntityResolutionConfig
from tripwire_common.names import normalize_company_name

MatchStatus = Literal["auto_matched", "needs_review", "confirmed", "unmatched"]
NameKind = Literal["current", "former"]

# "Old Co (formerly New Co)", "X f/k/a Y", "X d/b/a Y": each part is matched separately.
_ALT_NAME = re.compile(r"\s*(?:\(|\b(?:f/?k/?a|formerly|d/?b/?a|aka)\b\.?:?)\s*", re.IGNORECASE)
_TICKER_PREFIX = re.compile(r"^(?:NYSE|NASDAQ|NYSEAMERICAN|AMEX|OTC|OTCMKTS|TSX)\s*[:\s]\s*")


@dataclass(frozen=True)
class Company:
    cik: str
    name: str
    tickers: tuple[str, ...] = ()
    former_names: tuple[str, ...] = ()
    is_excluded: bool = False


@dataclass(frozen=True)
class Candidate:
    cik: str
    name: str  # the company's current name
    score: float  # 0-100 similarity of the input to this company's best-matching name
    matched_name: str  # which of its names matched
    kind: NameKind | Literal["ticker"]


@dataclass(frozen=True)
class PriorDecision:
    cik: str | None  # None = a human said "no match"


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    cik: str | None
    confidence: float
    method: str
    candidates: list[Candidate] = field(default_factory=list)


def normalize_ticker(raw: str | None) -> str:
    """'nasdaq: aapl' -> 'AAPL'; 'BRK.B' / 'BRK/B' -> 'BRK-B' (SEC's form)."""
    if not raw:
        return ""
    text = _TICKER_PREFIX.sub("", raw.strip().upper().lstrip("$"))
    return re.sub(r"[./\s]+", "-", text).strip("-")


class Universe:
    """All companies a row can match, indexed for the cascade."""

    def __init__(self, companies: list[Company], config: EntityResolutionConfig) -> None:
        self.config = config
        self.companies = {c.cik: c for c in companies}
        self.by_ticker: dict[str, set[str]] = defaultdict(set)
        self.by_name: dict[str, set[tuple[str, NameKind]]] = defaultdict(set)
        for c in companies:
            for t in c.tickers:
                self.by_ticker[normalize_ticker(t)].add(c.cik)
            self.by_name[self.normalize(c.name)].add((c.cik, "current"))
            for former in c.former_names:
                self.by_name[self.normalize(former)].add((c.cik, "former"))
        self.by_name.pop("", None)
        self._choices = list(self.by_name)
        self._compact = [c.replace(" ", "") for c in self._choices]

    def normalize(self, name: str) -> str:
        tokens = normalize_company_name(name).split()
        expanded = [self.config.abbreviations.get(t, t) for t in tokens]
        return " ".join(expanded)

    def input_key(self, name: str | None, ticker: str | None) -> str:
        """Stable key for decision reuse: same normalized input -> same key."""
        return f"{self.normalize(name or '')}|{normalize_ticker(ticker)}"

    # --- cascade ---

    def match(
        self, name: str | None, ticker: str | None, prior: PriorDecision | None = None
    ) -> MatchResult:
        conf = self.config.confidence
        if prior is not None:
            if prior.cik is None:
                return MatchResult("unmatched", None, conf.prior_decision, "prior_decision")
            return MatchResult("confirmed", prior.cik, conf.prior_decision, "prior_decision")

        variants = [v for v in _ALT_NAME.split(name or "") if v and v.strip(" )")]
        variants = [v.strip(" )") for v in variants] or [""]
        results = [self._match_one(v, normalize_ticker(ticker)) for v in variants]
        return max(results, key=lambda r: r.confidence)

    def _match_one(self, name: str, ticker: str) -> MatchResult:
        norm = self.normalize(name) if name else ""
        name_candidates = self.name_candidates(norm) if norm else []
        if ticker and ticker in self.by_ticker:
            return self._ticker_match(ticker, norm, name_candidates)
        if not norm:
            return self._result(None, 0.0, "no_usable_input", [])
        return self._name_match(norm, name_candidates)

    def _ticker_match(
        self, ticker: str, norm: str, name_candidates: list[Candidate]
    ) -> MatchResult:
        conf = self.config.confidence
        ciks = sorted(self.by_ticker[ticker])
        ticker_candidates = [self._ticker_candidate(cik, norm) for cik in ciks]
        if len(ciks) > 1:
            return self._result(None, conf.ambiguous_name, "ticker_shared",
                                [*ticker_candidates, *name_candidates])  # fmt: skip
        (tc,) = ticker_candidates
        if not norm:
            return self._result(tc.cik, conf.ticker_only, "ticker_only", [tc])
        if tc.score >= self.config.ticker_name_agree_score:
            return self._result(tc.cik, conf.ticker_and_name, "ticker_and_name", [tc])
        others = [c for c in name_candidates if c.cik != tc.cik]
        return self._result(None, conf.ticker_name_conflict, "ticker_name_conflict", [tc, *others])

    def _name_match(self, norm: str, candidates: list[Candidate]) -> MatchResult:
        conf = self.config.confidence
        exact = self.by_name.get(norm, set())
        if exact:
            ciks = {cik for cik, _ in exact}
            if len(ciks) > 1:
                return self._result(None, conf.ambiguous_name, "ambiguous_name", candidates)
            ((cik, kind),) = sorted(exact, key=lambda e: e[1] != "current")[:1]
            confidence = conf.exact_name if kind == "current" else conf.exact_former_name
            method = "exact_name" if kind == "current" else "exact_former_name"
            return self._result(cik, confidence, method, candidates)
        if not candidates:
            return self._result(None, 0.0, "no_candidates", self.suggestions(norm))
        best = candidates[0]
        runner_up = candidates[1].score if len(candidates) > 1 else 0.0
        confidence = best.score / 100
        if best.score - runner_up < self.config.fuzzy_min_gap:
            confidence = min(confidence, self.config.fuzzy_ambiguous_cap)
        if confidence < self.config.auto_match_min_confidence:
            # Going to a human: show the closest strict matches, then looser suggestions.
            keep = max(self.config.max_candidates - 2, 1)
            candidates = [*candidates[:keep], *self.suggestions(norm), *candidates[keep:]]
        return self._result(best.cik, confidence, "fuzzy_name", candidates)

    def _result(
        self, cik: str | None, confidence: float, method: str, candidates: list[Candidate]
    ) -> MatchResult:
        unique: dict[str, Candidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.cik, candidate)
        top = list(unique.values())[: self.config.max_candidates]
        if cik is not None and confidence >= self.config.auto_match_min_confidence:
            return MatchResult("auto_matched", cik, round(confidence, 4), method, top)
        # Below threshold: keep the best guess as a suggestion for the reviewer, not a match.
        return MatchResult("needs_review", None, round(confidence, 4), method, top)

    # --- candidates ---

    def name_candidates(self, norm: str) -> list[Candidate]:
        """Distinct companies ranked by strict similarity of their best (current/former) name.

        Hits come from two indexes: word-order-insensitive (token_sort_ratio) and spacing-
        insensitive ("delta airlines" vs "delta air lines"); every hit is then re-scored with
        the same strict `similarity`, which is what confidence is based on.
        """
        limit = self.config.max_candidates * 4
        choices = {
            c
            for c, _, _ in process.extract(
                norm, self._choices, scorer=fuzz.token_sort_ratio, limit=limit
            )
        }
        compact = norm.replace(" ", "")
        choices |= {self._choices[i] for _, _, i in process.extract(
            compact, self._compact, scorer=fuzz.ratio, limit=limit)}  # fmt: skip
        return self._rank(norm, choices)

    def suggestions(self, norm: str) -> list[Candidate]:
        """Looser partial matches ("Meta" -> "Meta Platforms") to help a reviewer. Never used
        for confidence; these companies are scored with the strict similarity like any other."""
        hits = process.extract(norm, self._choices, scorer=fuzz.WRatio,
                               limit=self.config.max_candidates)  # fmt: skip
        return self._rank(norm, {c for c, _, _ in hits})

    def _rank(self, norm: str, choices: set[str]) -> list[Candidate]:
        best: dict[str, Candidate] = {}
        for choice in choices:
            score = similarity(norm, choice)
            for cik, kind in self.by_name[choice]:
                if cik not in best or score > best[cik].score:
                    company = self.companies[cik]
                    best[cik] = Candidate(cik, company.name, score, choice, kind)
        return sorted(best.values(), key=lambda c: (-c.score, c.cik))

    def _ticker_candidate(self, cik: str, norm: str) -> Candidate:
        company = self.companies[cik]
        if not norm:
            return Candidate(cik, company.name, 0.0, company.name, "ticker")
        names = [company.name, *company.former_names]
        score, matched = max((similarity(norm, self.normalize(n)), n) for n in names)
        return Candidate(cik, company.name, score, matched, "ticker")


def similarity(a: str, b: str) -> float:
    """0-100: the better of word-order-insensitive and spacing-insensitive similarity."""
    return float(
        max(fuzz.token_sort_ratio(a, b), fuzz.ratio(a.replace(" ", ""), b.replace(" ", "")))
    )
