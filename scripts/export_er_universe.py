"""Export the entity-resolution candidate universe (curated.company + former names) to a
fixture, so the matcher can be evaluated offline and in CI. Public SEC data only.

    uv run python scripts/export_er_universe.py tests/entity/fixtures/universe.csv.gz
"""

from __future__ import annotations

import csv
import gzip
import io
import sys
from pathlib import Path

from tripwire_common.db import connect


def main(out: Path) -> None:
    with connect() as conn:
        rows = conn.execute(
            """select c.cik, c.name, array_to_string(c.tickers, '|'),
                      coalesce(string_agg(a.alias, '|' order by a.alias)
                               filter (where a.alias_type = 'former_name'), ''),
                      c.is_excluded
               from curated.company c
               left join curated.company_alias a on a.cik = c.cik
               group by c.cik order by c.cik"""
        ).fetchall()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["cik", "name", "tickers", "former_names", "is_excluded"])
    writer.writerows(rows)
    # mtime=0 keeps the gzip bytes deterministic across exports.
    out.write_bytes(gzip.compress(buffer.getvalue().encode(), mtime=0))
    print(f"wrote {len(rows)} companies to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
