"""Apply supabase/migrations to a plain Postgres database (local Docker).

For a real Supabase project use the Supabase CLI (`supabase db push`), which tracks applied
migrations. This script is for throwaway local databases only: it does not track state.

    uv run python scripts/apply_migrations.py --with-shim
"""

from __future__ import annotations

import argparse
import logging

from tripwire_common.db import LOCAL_SHIM, apply_sql_files, connect, migration_files
from tripwire_common.logging import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-shim",
        action="store_true",
        help="first apply supabase/local/supabase_shim.sql (plain Postgres only)",
    )
    args = parser.parse_args()

    configure_logging("migrations")
    files = migration_files()
    if args.with_shim:
        files = [LOCAL_SHIM, *files]
    with connect() as conn:
        apply_sql_files(conn, files)
    logging.getLogger(__name__).info("migrations applied", extra={"files": len(files)})


if __name__ == "__main__":
    main()
