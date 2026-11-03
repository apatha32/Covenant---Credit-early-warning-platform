"""Text-signal extraction: prompt, JSON schema, and the evidence check.

One request per (filing section part, model). The model returns, for each signal type it finds,
one verbatim quote. A signal is kept only if its quote is found in the section text (after
normalizing whitespace, quote marks and dashes); everything else is rejected and counted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from tripwire_common.config import TextSignals

EFFORT = "low"  # extraction is short, structured reading; measured on the eval set
MAX_TOKENS = 4_000
MAX_QUOTE_CHARS = 600
MIN_QUOTE_CHARS = 20

SECTION_NAMES = {
    "risk_factors": "Risk Factors",
    "mdna": "Management's Discussion and Analysis",
    "auditor_report": "Report of the independent auditor",
    "passage": "Excerpt",
}


def system_prompt(config: TextSignals) -> str:
    definitions = "\n".join(
        f"- {name}: {s.definition.strip()}" for name, s in config.signals.items()
    )
    return f"""You read sections of SEC filings for a credit analyst and report credit-risk \
signals about the filing company.

Signal types:
{definitions}

Rules:
- Report a signal only if the text states that the condition exists now or existed in the \
reported period for this company. Risk-factor language about what could happen ("if we fail \
to...", "we may be unable to...") is not a signal unless the text also says the condition \
currently exists.
- For each signal type you report, give one evidence quote: a contiguous passage copied \
character for character from the text, at most {MAX_QUOTE_CHARS} characters. Do not shorten \
it with ellipses, do not fix typos, do not paraphrase.
- Report each signal type at most once, with the clearest evidence.
- confidence: your probability (0 to 1) that the signal is present.
- If no signal is present, return an empty list."""


def output_schema(signal_types: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "signal_type": {"type": "string", "enum": signal_types},
                        "evidence_quote": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["signal_type", "evidence_quote", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["signals"],
        "additionalProperties": False,
    }


def user_message(company: str, form: str, filed: str, section: str, text: str) -> str:
    return (
        f"Company: {company}\nFiling: {form} filed {filed}\n"
        f"Section: {SECTION_NAMES.get(section, section)}\n\n<section>\n{text}\n</section>"
    )


def request_params(
    config: TextSignals, model: str, signal_types: list[str], message: str
) -> dict[str, Any]:
    """Keyword arguments for client.messages.create (also used as Batch API params)."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt(config),
        "output_config": {
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": output_schema(signal_types)},
        },
        "messages": [{"role": "user", "content": message}],
    }


# --- evidence check ---

_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                         "\u2013": "-", "\u2014": "-", "\u2011": "-", "\xa0": " "})  # fmt: skip
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.translate(_QUOTES)).strip()


@dataclass
class Checked:
    kept: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def check_evidence(raw: dict[str, Any], source_text: str, allowed: set[str]) -> Checked:
    """Keep signals whose quote really appears in the source; reject (with a reason) the rest."""
    source = normalize(source_text)
    out = Checked()
    seen: set[str] = set()
    for s in raw.get("signals", []):
        quote = normalize(str(s.get("evidence_quote", "")))
        kind = s.get("signal_type")
        reason = None
        if kind not in allowed:
            reason = "unknown_signal_type"
        elif kind in seen:
            reason = "duplicate_signal_type"
        elif len(quote) < MIN_QUOTE_CHARS:
            reason = "quote_too_short"
        elif quote not in source:
            reason = "quote_not_in_source"
        if reason:
            out.rejected.append({**s, "reason": reason})
            continue
        seen.add(kind)
        confidence = min(max(float(s.get("confidence", 0)), 0.0), 1.0)
        out.kept.append({"signal_type": kind, "evidence_quote": quote, "confidence": confidence})
    return out


def parse_response_text(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data


def cost_usd(prices: dict[str, tuple[float, float]], model: str, input_tokens: int,
             output_tokens: int, batch: bool = False) -> float:  # fmt: skip
    price_in, price_out = prices[model]
    usd = (input_tokens * price_in + output_tokens * price_out) / 1e6
    return usd / 2 if batch else usd
