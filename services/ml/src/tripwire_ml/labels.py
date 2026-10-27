"""Labels: which companies had a distress event, and when (point in time).

1. classify: each primary-label filing (8-K Item 1.03) is classified from its text, because the
   tag alone is noisy (see config text_check and reports/item_103_review.md).
2. episodes: positive filings are grouped into bankruptcy cases. A case starts at its first
   positive filing; follow-ups (confirmation, emergence) are not new events. A new case needs a
   case-ending follow-up (e.g. emergence) in between.
3. label(cik, T) = 1 if a case starts in (T, T + horizon]. Rows where the company is already in
   bankruptcy at T (case started, not yet ended, as known at T) are excluded from training.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

import polars as pl

from tripwire_common.config import DomainPack, Labels, TextCheck
from tripwire_common.names import normalize_company_name

Kind = Literal["petition", "receivership", "subsidiary_petition", "other_entity", "follow_up",
               "no_bankruptcy_language", "no_item_section", "unclear"]  # fmt: skip

_QUOTES = re.compile(r"[\"\u201c\u201d\u2018\u2019'`]")  # straight and curly quotes
_PARENS = re.compile(r"\([^()]{0,200}\)")
# Periods inside common abbreviations are not sentence ends ("Intelsat S.A. filed ...").
_ABBREV = re.compile(
    r"\b(Inc|Corp|Co|Ltd|L\.P|LLC|S\.A|N\.V|S\.p\.A|No|Nos|U\.S|Mr|Ms|Jr|St|Bros)\."
)
_INITIAL = re.compile(r"\b([A-Z])\.")
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z])")
_GENERIC_NAME_TOKENS = {"holdings", "holding", "group", "global", "international", "the", "and",
                        "company", "corporation", "inc", "co", "corp", "new", "old", "former",
                        "trust", "partners", "lp", "llc"}  # fmt: skip


@dataclass(frozen=True)
class Classification:
    kind: Kind
    ends_case: bool
    snippet: str  # the sentence that decided it (for review)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _QUOTES.sub("", text))


def name_tokens(names: list[str]) -> set[str]:
    """Distinctive tokens of a company's current and former names."""
    tokens: set[str] = set()
    for name in names:
        words = normalize_company_name(name).split()
        tokens |= {t for t in words if t not in _GENERIC_NAME_TOKENS and len(t) > 2}
        # Filings often abbreviate the filer ("Pharmaceutical Formulations Inc (PFI)").
        raw_words = re.findall(r"[a-z0-9]+", name.lower())
        initials = "".join(w[0] for w in raw_words if w not in {"the", "and", "of"})
        if len(initials) >= 3:
            tokens.add(initials)
    return tokens


class TextRules:
    def __init__(self, rules: TextCheck) -> None:
        flags = re.IGNORECASE | re.DOTALL
        self.heading = re.compile(rules.section_heading, flags)
        self.section_start = re.compile(rules.section_start, flags)
        self.section_end = re.compile(rules.section_end, re.DOTALL)  # case matters for headings
        self.event = re.compile(rules.event_terms, flags)
        self.verb = re.compile(rules.filing_verbs, flags)
        self.receivership = re.compile(rules.receivership, flags)
        self.previously = re.compile(rules.previously_disclosed, flags)
        self.follow_up = re.compile(rules.follow_up, flags)
        self.ends_case = re.compile(rules.ends_case, flags)
        self.registrant = re.compile(rules.registrant, flags)
        self.weak_registrant = re.compile(rules.weak_registrant, flags)
        self.subsidiary = re.compile(rules.subsidiary, flags)
        self.subsidiary_of = re.compile(rules.subsidiary_of, flags)
        self.parent_with_subs = re.compile(rules.parent_with_subsidiaries, flags)
        self.hedged = re.compile(rules.hedged, flags)
        self.negated = re.compile(rules.negated, flags)
        self.new_case_max_days = rules.new_case_max_days

    def section(self, text: str) -> str | None:
        """Text of the Item 1.03 section: by its heading first, else a non-cross-reference."""
        for pattern in (self.heading, self.section_start):
            for start in pattern.finditer(text):
                body = text[start.end() : start.end() + 6000]
                end = self.section_end.search(body)
                section = body[: end.start()] if end else body
                # A heading in the form's item list is followed straight by the next item.
                if len(section) > 80:
                    return section
        return None

    def _mentions_registrant(self, sentence: str, tokens: set[str], strict: bool) -> bool:
        """strict: joint filing by another company (e.g. a sister utility); its parent-name
        token alone does not identify this filer."""
        if self.registrant.search(sentence):
            return True
        words = set(normalize_company_name(sentence).split())
        hits = len(tokens & words)
        return hits >= (2 if strict and len(tokens) > 1 else 1)

    def classify(
        self,
        text: str,
        names: list[str],
        filed: dt.date | None = None,
        strict: bool = False,
    ) -> Classification:
        """names: the filer's current and former names. filed: the 8-K's filing date."""
        section = self.section(text)
        if section is not None and len(section) < 400 and "incorporated" in section.lower():
            # "The information in Item 1.01 is incorporated by reference": read the whole 8-K.
            section = text[:20000]
        if section is None:
            return Classification("no_item_section", False, "")
        section = _clean(section)
        ends = bool(self.ends_case.search(section))
        follow = self.follow_up.search(section)
        sentences = _SENTENCE.split(_INITIAL.sub(r"\1", _ABBREV.sub(r"\1", section)))
        petitions = [
            s for s in sentences
            if self.event.search(s) and self.verb.search(s)
            and not self.hedged.search(s) and not self.negated.search(s)
        ]  # fmt: skip
        dated = [s for s in petitions if _DATE.search(s)]
        recent = [s for s in dated if _is_recent(s, filed, self.new_case_max_days)]
        if recent and not self.previously.search(recent[0]):
            return self._who_filed(recent[0], names, strict)
        if follow:
            return Classification("follow_up", ends, _around(section, follow.start()))
        undated = [s for s in petitions if not _DATE.search(s)]
        if not dated and undated and not self.previously.search(undated[0]):
            return self._who_filed(undated[0], names, strict)
        if petitions or self.previously.search(section):
            return Classification("follow_up", ends, (petitions[0] if petitions else section)[:300])
        receivership = self.receivership.search(section)
        if receivership:
            return Classification("receivership", False, _around(section, receivership.start()))
        if not self.event.search(section) and "bankrupt" not in section.lower():
            return Classification("no_bankruptcy_language", False, section[:300])
        return Classification("unclear", False, section[:300])

    def _who_filed(self, sentence: str, names: list[str], strict: bool) -> Classification:
        """The filer itself (with or without subsidiaries), only subsidiaries, or another entity."""
        snippet = sentence[:300]
        tokens = name_tokens(names)
        plain = _PARENS.sub(" ", sentence)  # defined-term parentheticals removed
        # "Subsidiaries of the Company filed": the parent counts only if named explicitly
        # (subsidiaries often share the parent's name, so name tokens are not enough).
        subsidiaries_only = (
            self.subsidiary_of.search(sentence)
            and not self.parent_with_subs.search(plain)
            and not self.registrant.search(self.subsidiary_of.sub(" ", sentence))
        )
        if subsidiaries_only:
            return Classification("subsidiary_petition", False, snippet)
        if self._mentions_registrant(sentence, tokens, strict) or (
            self.weak_registrant.search(sentence) and not self.subsidiary_of.search(sentence)
        ):
            return Classification("petition", False, snippet)
        return Classification("other_entity", False, snippet)


_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|"
           "december")  # fmt: skip
_DATE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}})\s*,?\s*(\d{{4}})", re.IGNORECASE)


def _is_recent(sentence: str, filed: dt.date | None, max_days: int) -> bool:
    """True if the sentence's first date is within max_days before the filing (or undated)."""
    m = _DATE.search(sentence)
    if m is None or filed is None:
        return True
    try:
        when = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
    except ValueError:
        return True
    return 0 <= (filed - when).days <= max_days


def _around(text: str, pos: int, width: int = 300) -> str:
    return text[max(0, pos - 100) : pos + width - 100]


# --- episodes and labels ---


@dataclass(frozen=True)
class Case:
    cik: str
    start: dt.date  # first positive filing
    end: dt.date | None  # first case-ending follow-up, if any
    accession: str


def build_cases(events: pl.DataFrame, labels: Labels) -> list[Case]:
    """events: cik, filed_date, accession, kind, ends_case (classified primary-label filings)."""
    rules = labels.text_check
    positive = set(rules.positive_kinds if rules else ["petition"])
    lookback = dt.timedelta(days=round((rules.infer_case_lookback_months if rules else 24) * 30.44))
    cases: list[Case] = []
    for (cik,), group in events.sort("filed_date", "accession").group_by(
        "cik", maintain_order=True
    ):
        open_start: dt.date | None = None
        open_accession = ""
        last_case_start: dt.date | None = None
        for row in group.iter_rows(named=True):
            filed: dt.date = row["filed_date"]
            if row["kind"] in positive:
                if open_start is None:
                    open_start, open_accession = filed, row["accession"]
                # A positive filing inside an open case is the same case.
            elif row["kind"] == "follow_up":
                if (
                    open_start is None
                    and "inferred_from_follow_up" in positive
                    and (last_case_start is None or filed - last_case_start > lookback)
                ):
                    # A confirmation with no petition on file: the petition 8-K was not tagged
                    # (common for prepackaged cases). Start the case at this filing.
                    open_start, open_accession = filed, row["accession"]
                if open_start is not None and row["ends_case"]:
                    cases.append(Case(str(cik), open_start, filed, open_accession))
                    last_case_start, open_start = open_start, None
        if open_start is not None:
            cases.append(Case(str(cik), open_start, None, open_accession))
    return cases


def label_rows(rows: pl.DataFrame, cases: list[Case], labels: Labels) -> pl.DataFrame:
    """rows: cik, as_of_date. Adds `label`, `label_source_accession`, `in_bankruptcy`."""
    horizon = dt.timedelta(days=round(labels.horizon_months * 365.25 / 12))
    max_case = dt.timedelta(days=round((labels.text_check.max_case_months if labels.text_check
                                        else 36) * 365.25 / 12))  # fmt: skip
    case_frame = pl.DataFrame(
        {"cik": [c.cik for c in cases], "case_start": [c.start for c in cases],
         "case_end": [c.end for c in cases], "case_accession": [c.accession for c in cases]},
        schema={"cik": pl.String, "case_start": pl.Date, "case_end": pl.Date,
                "case_accession": pl.String},
    )  # fmt: skip
    joined = rows.join(case_frame, on="cik", how="left")
    t, start, end = pl.col("as_of_date"), pl.col("case_start"), pl.col("case_end")
    positive = (start > t) & (start <= t + horizon)
    # In bankruptcy at T: case started on or before T, and (as known at T) it has not ended.
    in_case = (start <= t) & (end.is_null() | (end > t)) & (t < start + max_case)
    flagged = joined.with_columns(
        hit=positive.fill_null(False),
        in_case=in_case.fill_null(False),
        source=pl.when(positive).then(pl.col("case_accession")),
    )
    return flagged.group_by("cik", "as_of_date", maintain_order=True).agg(
        label=pl.col("hit").any().cast(pl.Int8),
        label_source_accession=pl.col("source").drop_nulls().first(),
        in_bankruptcy=pl.col("in_case").any(),
    )


def primary_items(pack: DomainPack) -> list[str]:
    """8-K item codes whose event type is a primary label (e.g. ['1.03'])."""
    primary = set(pack.labels.primary_events)
    return sorted(code for code, ev in pack.event_codes.eight_k_items.items()
                  if ev.event_type in primary)  # fmt: skip
