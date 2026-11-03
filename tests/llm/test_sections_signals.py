from __future__ import annotations

import math

from tripwire_common.config import load_domain_pack, load_text_signals
from tripwire_llm.evaluate import score
from tripwire_llm.sections import find_sections, html_to_paragraphs, split_paragraphs
from tripwire_llm.signals import check_evidence, output_schema, request_params

CONFIG = load_text_signals(load_domain_pack())

TEN_K = (
    b"""<html><body>
<table><tr><td>Item 1A.</td><td>Risk Factors</td><td>9</td></tr>
<tr><td>Item 7.</td><td>Management's Discussion and Analysis</td><td>30</td></tr></table>
<p>PART I</p><p>Item 1. Business</p><p>We fly planes. See Item 7. Management&#8217;s Discussion
and Analysis for more.</p>
<p>ITEM 1A. RISK FACTORS</p>"""
    + b"<p>If we fail to comply with covenants, lenders may act.</p>" * 40
    + b"""
<p>Item 1B. Unresolved Staff Comments</p><p>None.</p>
<p>ITEM 7. MANAGEMENT&#8217;S DISCUSSION AND ANALYSIS</p>"""
    + (
        b"<p>Liquidity: we obtained a waiver of the leverage covenant from our lenders in May.</p>"
        * 40
    )
    + b"""<p>Item 7A. Quantitative and Qualitative Disclosures</p>
<p>Item 8. Financial Statements</p>
<p>Report of Independent Registered Public Accounting Firm (PCAOB ID 42)</p><p>51</p>
<p>REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM</p>
<p>To the Shareholders and the Board of Directors</p>"""
    + (
        b"<p>The Company has suffered recurring losses that raise substantial doubt about its "
        b"ability to continue as a going concern.</p>" * 12
    )
    + b"""<p>/s/ Ernst &amp; Young LLP</p><p>We have served as auditor since 2006.</p>
<p>Consolidated Balance Sheets</p><p>Cash 100</p></body></html>"""
)


def test_sections_skip_toc_and_cross_references() -> None:
    text = html_to_paragraphs(TEN_K)
    found, missing = find_sections(text, "10-K", ["risk_factors", "mdna", "auditor_report"])
    assert missing == []
    by = {s.name: s.text for s in found}
    assert by["risk_factors"].startswith("ITEM 1A. RISK FACTORS")
    assert "Unresolved" not in by["risk_factors"]
    assert by["mdna"].startswith("ITEM 7. MANAGEMENT\u2019S DISCUSSION")
    assert "waiver of the leverage covenant" in by["mdna"] and "Item 7A" not in by["mdna"]
    # the index line (no addressee) is skipped; the report ends after the signature
    assert by["auditor_report"].startswith("REPORT OF INDEPENDENT")
    assert "going concern" in by["auditor_report"]
    assert "Cash 100" not in by["auditor_report"]


def test_inline_tags_do_not_split_words() -> None:
    assert html_to_paragraphs(b"<p>Item 1A. Ri<span>sk</span> Factors</p>") == (
        "Item 1A. Risk Factors"
    )


def test_missing_section_is_reported() -> None:
    _, missing = find_sections("Item 2. Properties\nNone.", "10-Q", ["mdna"])
    assert missing == ["mdna"]


def test_split_paragraphs_respects_limit() -> None:
    parts = split_paragraphs("\n".join(["word " * 50] * 30), 1_000)
    assert all(len(p) <= 1_000 for p in parts) and len(parts) > 1


SOURCE = "On May 3 we obtained a waiver of the leverage covenant from our lenders. Cash fell."


def test_evidence_must_be_verbatim() -> None:
    raw = {"signals": [
        {"signal_type": "covenant_breach", "confidence": 0.9,
         "evidence_quote": "we obtained a waiver of the leverage\n covenant from our lenders"},
        {"signal_type": "liquidity_concern", "confidence": 0.7,
         "evidence_quote": "we may run out of cash next year"},
        {"signal_type": "covenant_breach", "confidence": 0.5,
         "evidence_quote": "On May 3 we obtained a waiver"},
        {"signal_type": "made_up", "confidence": 0.5, "evidence_quote": SOURCE},
    ]}  # fmt: skip
    checked = check_evidence(raw, SOURCE, set(CONFIG.signals))
    assert [s["signal_type"] for s in checked.kept] == ["covenant_breach"]
    assert [r["reason"] for r in checked.rejected] == [
        "quote_not_in_source", "duplicate_signal_type", "unknown_signal_type"]  # fmt: skip


def test_curly_quotes_and_dashes_match_straight_ones() -> None:
    quote = "the Company's ability to continue - as a going concern"
    raw = {"signals": [{"signal_type": "going_concern", "confidence": 2, "evidence_quote": quote}]}
    source = "about the Company\u2019s ability to continue \u2014 as a going concern"
    checked = check_evidence(raw, source, set(CONFIG.signals))
    assert checked.kept[0]["confidence"] == 1.0  # clamped


def test_request_uses_strict_schema() -> None:
    params = request_params(CONFIG, "claude-haiku-5-5", list(CONFIG.signals), "hi")
    schema = params["output_config"]["format"]["schema"]
    assert schema == output_schema(list(CONFIG.signals))
    assert schema["additionalProperties"] is False
    assert "thinking" not in params and "temperature" not in params


def test_score_counts_per_type() -> None:
    passages = [{"id": 1, "labels": ["going_concern"]}, {"id": 2, "labels": []},
                {"id": 3, "labels": ["covenant_breach", "liquidity_concern"]}]  # fmt: skip
    preds = [{"id": 1, "predicted": ["going_concern"]}, {"id": 2, "predicted": ["going_concern"]},
             {"id": 3, "predicted": ["covenant_breach"]}]  # fmt: skip
    s = score(passages, preds, list(CONFIG.signals))
    assert s["going_concern"]["precision"] == 0.5 and s["going_concern"]["recall"] == 1.0
    assert s["liquidity_concern"]["recall"] == 0.0
    assert math.isnan(s["customer_concentration"]["precision"])
    assert (s["all"]["tp"], s["all"]["fp"], s["all"]["fn"]) == (2, 1, 1)
