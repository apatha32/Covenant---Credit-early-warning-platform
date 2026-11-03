"""Split a 10-K / 10-Q into the sections the LLM layer reads.

Filings are converted to text with paragraph breaks kept, because headings are only trusted at
the start of a line: "see Item 7. Management's Discussion..." inside a sentence is a
cross-reference, not the section. A heading also appears in the table of contents, so of all
heading matches the one followed by the longest stretch of text (up to the next section's
heading) wins. Sections that cannot be found are reported, never guessed.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_HIDDEN = re.compile(r"<(script|style|ix:header|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_END = re.compile(r"<(?:br\s*/?|/(?:p|div|tr|li|h[1-6]|table|td|th))\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_INLINE_SPACE = re.compile(r"[^\S\n]+")
_BLANKS = re.compile(r"\n\s*\n+")

_SEP = "[\\s.:\\-\u2013\u2014]*"  # between "Item 7" and its title: dots, dashes, line breaks
_APOS = "(?:'|\u2019|`)?"


def html_to_paragraphs(raw: bytes) -> str:
    """Plain text with one paragraph (or table cell / heading) per line."""
    text = raw.decode("utf-8", errors="replace")
    text = _HIDDEN.sub(" ", text)
    text = _BLOCK_END.sub("\n", text)
    # Inline tags (spans, fonts) can sit inside a word ("Ri<span>sk"): drop them without a space.
    # Block ends were already turned into line breaks above.
    text = html.unescape(_TAG.sub("", text)).replace("\xa0", " ")
    text = _INLINE_SPACE.sub(" ", text)
    lines = (line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _heading(item: str, title: str) -> re.Pattern[str]:
    return re.compile(rf"^item\s*{item}\b{_SEP}{title}", re.IGNORECASE | re.MULTILINE)


def _next_item(*items: str) -> re.Pattern[str]:
    return re.compile(rf"^item\s*(?:{'|'.join(items)})\b", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class SectionRule:
    start: re.Pattern[str]
    end: re.Pattern[str]
    min_chars: int


_RISK = _heading("1a", r"risk\s+factors")
RULES: dict[str, dict[str, SectionRule]] = {
    "10-K": {
        "risk_factors": SectionRule(_RISK, _next_item("1b", "1c", "2"), 1_000),
        "mdna": SectionRule(
            _heading("7", rf"management{_APOS}s?\s+discussion"), _next_item("7a", "8"), 2_000
        ),
    },
    "10-Q": {
        "mdna": SectionRule(
            _heading("2", rf"management{_APOS}s?\s+discussion"), _next_item("3", "4"), 1_000
        ),
        "risk_factors": SectionRule(_RISK, _next_item("2", "3", "4", "5", "6"), 100),
    },
}

# The report on the financial statements opens with its addressee; index entries do not.
_AUDITOR = re.compile(
    r"^report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm\b[^\n]*\n+"
    r"\s*to\s+the\s+(?:share|stock)holders|^report\s+of\s+independent\s+registered\s+public"
    r"\s+accounting\s+firm\b[^\n]*\n+\s*to\s+the\s+board",
    re.IGNORECASE | re.MULTILINE,
)
_SIGNATURE = re.compile(r"^\s*/s/", re.MULTILINE)
_STATEMENTS = re.compile(r"^consolidated\s+balance\s+sheets?\b", re.IGNORECASE | re.MULTILINE)
AUDITOR_MAX_CHARS = 20_000
AUDITOR_AFTER_SIGNATURE = 400  # "We have served as the Company's auditor since ...", city, date


@dataclass(frozen=True)
class Section:
    name: str
    text: str
    start: int  # character offset in the filing text (for debugging / lineage)


def _longest(text: str, rule: SectionRule) -> tuple[int, int] | None:
    best: tuple[int, int] | None = None
    starts = list(rule.start.finditer(text))
    for i, m in enumerate(starts):
        end_match = rule.end.search(text, m.end())
        end = end_match.start() if end_match else len(text)
        # A table-of-contents entry must not swallow the real section that follows it.
        if i + 1 < len(starts):
            end = min(end, starts[i + 1].start())
        if best is None or end - m.start() > best[1] - best[0]:
            best = (m.start(), end)
    return best


def _auditor_report(text: str) -> tuple[int, int] | None:
    """The first auditor's report (the one on the financial statements; the internal-control
    report follows it), up to just after the auditor's signature."""
    m = _AUDITOR.search(text)
    if m is None:
        return None
    limit = min(len(text), m.start() + AUDITOR_MAX_CHARS)
    sig = _SIGNATURE.search(text, m.end(), limit)
    end = min(limit, sig.end() + AUDITOR_AFTER_SIGNATURE) if sig else limit
    statements = _STATEMENTS.search(text, m.end(), end)
    return m.start(), statements.start() if statements else end


def find_sections(text: str, form: str, wanted: list[str]) -> tuple[list[Section], list[str]]:
    """Sections found, and the names of wanted sections that were not found."""
    base_form = form.split("/")[0]
    rules = RULES.get(base_form, {})
    found: list[Section] = []
    missing: list[str] = []
    for name in wanted:
        if name == "auditor_report":
            span = _auditor_report(text) if base_form == "10-K" else None
            min_chars = 1_000
        elif name in rules:
            span = _longest(text, rules[name])
            min_chars = rules[name].min_chars
        else:
            span = None
            min_chars = 0
        if span is None or span[1] - span[0] < min_chars:
            missing.append(name)
            continue
        found.append(Section(name, text[span[0] : span[1]].strip(), span[0]))
    return found, missing


def split_paragraphs(text: str, max_chars: int) -> list[str]:
    """Consecutive paragraphs packed into parts of at most ~max_chars (a paragraph longer than
    that is split at sentence ends, or hard-split as a last resort)."""
    parts: list[str] = []
    current = ""
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        pieces = [para]
        if len(para) > max_chars:
            pieces = [p for p in re.split(r"(?<=[.;])\s+", para) if p]
            pieces = [p[i : i + max_chars] for p in pieces for i in range(0, len(p), max_chars)]
        for piece in pieces:
            if current and len(current) + 1 + len(piece) > max_chars:
                parts.append(current)
                current = ""
            current = f"{current}\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts
