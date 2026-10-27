"""Freeze the dev sample's Item 1.03 filings as a regression fixture for the label classifier.

Writes tests/ml/fixtures/item103_dev.jsonl.gz: per filing, the document text (first 25k chars,
public SEC data) and the class it was reviewed to have. Run after `python -m tripwire_ml labels`
has fetched the documents; review any changed class by hand before committing.

    uv run python scripts/make_label_fixture.py
"""

from __future__ import annotations

import gzip
import json

from tripwire_common.config import load_domain_pack
from tripwire_common.db import connect
from tripwire_common.settings import REPO_ROOT
from tripwire_common.storage import get_storage
from tripwire_ml.documents import document_key, html_to_text
from tripwire_ml.labels import TextRules

OUT = REPO_ROOT / "tests" / "ml" / "fixtures" / "item103_dev.jsonl.gz"


def main() -> None:
    pack, storage = load_domain_pack(), get_storage()
    assert pack.labels.text_check is not None
    rules = TextRules(pack.labels.text_check)
    rows = []
    with connect() as conn:
        operating = {r[0] for r in conn.execute("select cik from curated.company")}
        found = conn.execute(
            """select f.cik, c.name,
                      coalesce(array_agg(a.alias)
                               filter (where a.alias_type = 'former_name'), '{}'),
                      f.accession, f.filed_date, f.form_type, f.primary_document
               from staging.filings f join curated.company c using (cik)
               left join curated.company_alias a on a.cik = f.cik
               where f.form_type in ('8-K', '8-K/A') and '1.03' = any(f.items)
               group by 1, 2, 4, 5, 6, 7 order by 1, 5, 4"""
        ).fetchall()
        for cik, name, former, accession, filed, form, document in found:
            text = html_to_text(storage.read_bytes(document_key(cik, accession, document)))[:25000]
            strict = accession[:10] != cik and accession[:10] in operating
            c = rules.classify(text, [name, *former], filed, strict)
            rows.append({"cik": cik, "names": [name, *former], "accession": accession,
                         "filed": filed.isoformat(), "form": form, "strict": strict,
                         "expected_kind": c.kind, "expected_ends_case": c.ends_case,
                         "text": text})  # fmt: skip
    OUT.write_bytes(gzip.compress("\n".join(json.dumps(r) for r in rows).encode(), mtime=0))
    print(f"wrote {len(rows)} filings to {OUT}")


if __name__ == "__main__":
    main()
