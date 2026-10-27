from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from tripwire_common.config import load_domain_pack
from tripwire_common.settings import REPO_ROOT
from tripwire_ml.labels import Case, TextRules, build_cases, label_rows, name_tokens

PACK = load_domain_pack("finance", REPO_ROOT / "config")
assert PACK.labels.text_check is not None
RULES = TextRules(PACK.labels.text_check)
D = dt.date.fromisoformat
FIXTURE = Path(__file__).parent / "fixtures" / "item103_dev.jsonl.gz"


def _fixture() -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in gzip.decompress(FIXTURE.read_bytes()).decode().splitlines()
    ]


def test_reviewed_dev_sample_filings_keep_their_class() -> None:
    """Regression: 98 real Item 1.03 8-Ks, each class checked by hand."""
    rows = _fixture()
    assert len(rows) == 98
    wrong = []
    for r in rows:
        c = RULES.classify(r["text"], r["names"], D(r["filed"]), r["strict"])
        if (c.kind, c.ends_case) != (r["expected_kind"], r["expected_ends_case"]):
            wrong.append((r["names"], r["filed"], r["expected_kind"], c.kind))
    assert wrong == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Item 1.03 Bankruptcy or Receivership On May 22, 2020, Acme Corp (the “Company”) and "
         "certain of its subsidiaries filed voluntary petitions under chapter 11.", "petition"),
        ("Item 1.03 Bankruptcy or Receivership On May 22, 2020, five wholly-owned subsidiaries "
         "of the Company filed voluntary petitions under chapter 11 of the Bankruptcy Code.",
         "subsidiary_petition"),
        ("Item 1.03 Bankruptcy or Receivership On June 1, 2021, the Bankruptcy Court entered an "
         "order confirming the Plan. The Plan became effective on June 5, 2021.", "follow_up"),
        ("Item 1.03 Bankruptcy or Receivership As previously disclosed, on January 3, 2020, the "
         "Company filed voluntary petitions for relief under chapter 11 of the Bankruptcy Code.",
         "follow_up"),
        ("Item 1.03 Entry into a Material Definitive Agreement. On May 25, 2020 the Company "
         "amended its revolving credit agreement with its lenders and reduced commitments.",
         "no_bankruptcy_language"),
        ("Item 1.01 Material Agreement. See Item 1.03 below for more. Item 2.02 Results.",
         "no_item_section"),
    ],
)  # fmt: skip
def test_classify_core_patterns(text: str, kind: str) -> None:
    assert RULES.classify(text * 1, ["Acme Corp"], D("2020-05-26")).kind == kind


def test_sister_company_in_joint_filing_is_another_entity() -> None:
    text = (
        "Item 1.03 Bankruptcy or Receivership On September 23, 2005, Entergy New Orleans filed "
        "a voluntary petition in the United States Bankruptcy Court for relief under "
        "chapter 11."
    )
    c = RULES.classify(text, ["Entergy Arkansas LLC"], D("2005-09-28"), strict=True)
    assert c.kind == "other_entity"


def test_old_restated_petition_date_means_follow_up() -> None:
    text = (
        "Item 1.03 Bankruptcy or Receivership On May 15, 2020, J. C. Penney Company, Inc. "
        "(the “Company”) and certain of its subsidiaries commenced voluntary cases under "
        "chapter 11 of title 11 of the United States Code in the Bankruptcy "
        "Court."
    )
    assert RULES.classify(text, ["J C Penney Co Inc"], D("2020-12-16")).kind == "follow_up"
    assert RULES.classify(text, ["J C Penney Co Inc"], D("2020-05-18")).kind == "petition"


def test_name_tokens_include_initials() -> None:
    assert "pfi" in name_tokens(["Pharmaceutical Formulations Inc"])


def _events(rows: list[tuple[str, str, str, bool]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"cik": [r[0] for r in rows], "filed_date": [D(r[1]) for r in rows],
         "accession": [f"0000000001-00-{i:06d}" for i in range(len(rows))],
         "kind": [r[2] for r in rows], "ends_case": [r[3] for r in rows]},
    )  # fmt: skip


def test_cases_group_filings_and_detect_repeat_bankruptcies() -> None:
    cases = build_cases(_events([
        ("A", "2020-05-26", "petition", False),
        ("A", "2020-06-01", "petition", False),     # second 8-K about the same filing
        ("A", "2021-06-16", "follow_up", True),     # confirmation ends the case
        ("A", "2024-11-18", "petition", False),     # a new bankruptcy ("Chapter 22")
        ("B", "2013-05-03", "follow_up", True),     # prepackaged: petition 8-K never tagged
        ("C", "2005-09-28", "other_entity", False),
    ]), PACK.labels)  # fmt: skip
    assert sorted((c.cik, c.start, c.end) for c in cases) == [
        ("A", D("2020-05-26"), D("2021-06-16")),
        ("A", D("2024-11-18"), None),
        ("B", D("2013-05-03"), D("2013-05-03")),
    ]


def test_labels_look_forward_twelve_months_and_drop_in_bankruptcy_rows() -> None:
    cases = [Case("A", D("2020-05-26"), D("2021-06-16"), "0000000001-20-000001")]
    dates = [D("2019-03-31"), D("2019-06-30"), D("2020-09-30"), D("2021-09-30")]
    rows = pl.DataFrame({"cik": ["A"] * 4, "as_of_date": dates})
    out = label_rows(rows, cases, PACK.labels).sort("as_of_date")
    assert out["label"].to_list() == [0, 1, 0, 0]  # 2019-06-30 + 12 months covers May 2020
    assert out["in_bankruptcy"].to_list() == [False, False, True, False]
    assert out["label_source_accession"].to_list()[1] == "0000000001-20-000001"
