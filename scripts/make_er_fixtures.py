"""Generate the messy borrower CSVs used to tune and test entity resolution.

Two files with disjoint companies and different seeds:
  borrowers_tune.csv     - for setting thresholds in config/entity_resolution.yaml
  borrowers_holdout.csv  - the Phase 3 acceptance test; never used for tuning

Each row carries the answer (`expected_cik`, empty = not an SEC company) and the kind of mess
applied (`case`). Rows are built from the committed universe fixture, so the files are
reproducible:

    uv run python scripts/make_er_fixtures.py tests/entity/fixtures
"""

from __future__ import annotations

import csv
import gzip
import io
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Per file: (case, rows). 100 rows in total.
PLAN = [
    ("exact_name_and_ticker", 10),
    ("name_variant", 15),
    ("typo_no_ticker", 15),
    ("typo_with_ticker", 5),
    ("former_name", 10),
    ("ticker_only", 10),
    ("abbreviated", 5),
    ("messy_ticker_format", 5),
    ("wrong_ticker", 5),
    ("unknown_ticker", 5),
    ("formerly_pattern", 5),
    ("not_an_sec_company", 10),
]

# Private businesses / made-up borrowers. None of these should ever be auto-matched.
NOT_SEC = [
    "Smith Family Dental Group LLC", "Riverside Orchard Partners", "Blue Heron Logistics of Ohio",
    "Kowalski Brothers Plumbing Inc", "Northgate Veterinary Clinics", "Sunrise Bakery Holdings LLC",
    "Patel & Sons Hospitality", "Granite Peak Landscaping Co", "Maple Leaf Daycare Centers",
    "Harborview Marine Supply LLC", "Desert Bloom Nurseries", "Oakwood Senior Living Partners",
    "Brightline Auto Body Inc", "Cedar Ridge Golf Club LLC", "Lakeshore Printing Company",
    "Ironwood Fabrication Ltd", "Silver Creek Dairy Farms", "Hudson Valley Storage LLC",
    "Pinecrest Pediatrics PC", "Copperfield Craft Brewing Co",
]  # fmt: skip


@dataclass(frozen=True)
class Company:
    cik: str
    name: str
    tickers: list[str]
    former_names: list[str]


def load_universe(path: Path) -> list[Company]:
    text = gzip.decompress(path.read_bytes()).decode()
    rows = list(csv.DictReader(io.StringIO(text)))
    companies = []
    for r in rows:
        former = [f for f in r["former_names"].split("|") if f and f != r["name"]]
        companies.append(Company(r["cik"], r["name"], [t for t in r["tickers"].split("|") if t],
                                 former))  # fmt: skip
    return companies


def typo(rng: random.Random, name: str) -> str:
    """One or two character edits inside words (never the first letter)."""
    chars = list(name)
    letters = [i for i, ch in enumerate(chars) if ch.isalpha() and i > 0 and chars[i - 1].isalpha()]
    for _ in range(rng.choice([1, 1, 2])):
        if not letters:
            break
        i = rng.choice(letters)
        edit = rng.choice(["swap", "drop", "double", "replace"])
        if edit == "swap" and i + 1 < len(chars) and chars[i + 1].isalpha():
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
        elif edit == "drop":
            chars[i] = ""
        elif edit == "double":
            chars[i] = chars[i] * 2
        else:
            chars[i] = rng.choice("aeiourstln")
    return "".join(chars)


def variant(rng: random.Random, name: str) -> str:
    """Same company, written differently: case, punctuation, legal suffix, 'The'."""
    swaps = [(r"\bInc\.?$", "Incorporated"), (r"\bCorp\.?$", "Corporation"),
             (r"\bCorporation$", "Corp."), (r"\bCo\.?$", "Company"), (r",", ""),
             (r"\bLtd\.?$", "Limited"), (r"\bIncorporated$", "Inc")]  # fmt: skip
    out = name
    for pattern, repl in rng.sample(swaps, k=len(swaps)):
        if re.search(pattern, out):
            out = re.sub(pattern, repl, out)
            break
    out = rng.choice([out.upper(), out.lower(), out.title(), out])
    if rng.random() < 0.3:
        out = f"The {out}"
    return f"  {out} " if rng.random() < 0.3 else out


ABBREVIATIONS = [("International", "Intl"), ("Technologies", "Tech"), ("Holdings", "Hldgs"),
                 ("Group", "Grp"), ("Manufacturing", "Mfg"), ("National", "Natl"),
                 ("Services", "Svcs"), ("Systems", "Sys"), (" and ", " & ")]  # fmt: skip


def abbreviate(name: str) -> str | None:
    for long, short in ABBREVIATIONS:
        pattern = re.compile(re.escape(long), re.IGNORECASE)
        if pattern.search(name):
            return pattern.sub(short, name, count=1)
    return None


def build(universe: list[Company], seed: int, exclude: set[str]) -> list[dict[str, str]]:
    rng = random.Random(seed)  # noqa: S311  # test data, not crypto
    with_ticker = [c for c in universe if c.tickers and c.cik not in exclude and len(c.name) >= 6]
    with_former = [c for c in universe if c.former_names and c.cik not in exclude]
    rng.shuffle(with_ticker)
    rng.shuffle(with_former)
    used: set[str] = set()
    rows: list[dict[str, str]] = []

    def pick(pool: list[Company]) -> Company:
        for c in pool:
            if c.cik not in used:
                used.add(c.cik)
                return c
        raise RuntimeError("pool exhausted")

    def row(name: str, ticker: str, cik: str, case: str) -> None:
        rows.append({"borrower_name": name, "ticker": ticker, "expected_cik": cik, "case": case})

    not_sec = rng.sample(NOT_SEC, k=dict(PLAN)["not_an_sec_company"])
    for case, n in PLAN:
        for i in range(n):
            if case == "not_an_sec_company":
                row(not_sec[i], "", "", case)
                continue
            if case in ("former_name", "formerly_pattern"):
                c = pick(with_former)
                old = rng.choice(c.former_names)
                text = old if case == "former_name" else f"{c.name} (formerly {old})"
                row(text, "", c.cik, case)
                continue
            if case == "abbreviated":
                candidates = [x for x in with_ticker if x.cik not in used and abbreviate(x.name)]
                c = candidates[0]
                used.add(c.cik)
                row(abbreviate(c.name) or c.name, "", c.cik, case)
                continue
            c = pick(with_ticker)
            ticker = c.tickers[0]
            if case == "exact_name_and_ticker":
                row(c.name, ticker, c.cik, case)
            elif case == "name_variant":
                row(variant(rng, c.name), "", c.cik, case)
            elif case == "typo_no_ticker":
                row(typo(rng, c.name), "", c.cik, case)
            elif case == "typo_with_ticker":
                row(typo(rng, c.name), ticker, c.cik, case)
            elif case == "ticker_only":
                row("", ticker, c.cik, case)
            elif case == "messy_ticker_format":
                messy = rng.choice([f"NYSE: {ticker.lower()}", f"${ticker}", f" {ticker.lower()} ",
                                    f"NASDAQ:{ticker}"])  # fmt: skip
                row(c.name.upper(), messy, c.cik, case)
            elif case == "wrong_ticker":
                other = pick(with_ticker)  # a real ticker, but another company's
                row(c.name, other.tickers[0], c.cik, case)
            elif case == "unknown_ticker":
                row(c.name, "ZZ" + "".join(rng.choice("QXJ") for _ in range(2)), c.cik, case)
    rng.shuffle(rows)
    return rows


def write(rows: list[dict[str, str]], path: Path) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["borrower_name", "ticker", "expected_cik", "case"])
        writer.writeheader()
        writer.writerows(rows)


def main(out_dir: Path) -> None:
    universe = load_universe(out_dir / "universe.csv.gz")
    tune = build(universe, seed=101, exclude=set())
    holdout = build(universe, seed=202, exclude={r["expected_cik"] for r in tune})
    assert not ({r["expected_cik"] for r in tune} & {r["expected_cik"] for r in holdout}) - {""}
    write(tune, out_dir / "borrowers_tune.csv")
    write(holdout, out_dir / "borrowers_holdout.csv")
    print(f"wrote {len(tune)} tune rows and {len(holdout)} holdout rows")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
