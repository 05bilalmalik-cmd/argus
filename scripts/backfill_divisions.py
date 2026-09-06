"""Backfill opportunity.division for rows ingested before division inference.

WARNING: this script WRITES to the ARGUS database via the SQLAlchemy models.
It must be run while the ARGUS server is STOPPED (SQLite WAL allows concurrent
readers, but a writer racing the server's sessions risks lock contention and
stale reads).  By default it is a dry run: it only prints the planned changes.
Pass --apply to actually persist them.

Idempotent by construction: it only ever touches rows where division = '' and
never overwrites a non-empty value, so running it twice is safe.

Usage:
    ./.venv/Scripts/python.exe scripts/backfill_divisions.py            # dry run
    ./.venv/Scripts/python.exe scripts/backfill_divisions.py --apply    # write
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.models import Opportunity
from app.scouting.divisions import DEFAULT_DIVISION, infer_division


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="persist changes (default: dry run, print planned changes only)",
    )
    args = parser.parse_args(argv)

    settings = Settings.load()
    settings.ensure_directories()
    db = Database(settings)

    with db.session_scope() as session:
        rows = session.scalars(
            select(Opportunity).where(Opportunity.division == "")
        ).all()
        if not rows:
            print("No opportunities with an empty division. Nothing to do.")
            return 0

        changed = 0
        for opp in rows:
            inferred = infer_division(opp.role_title, opp.employer)
            print(f"[{opp.id}] {opp.employer!r} | {opp.role_title!r}: "
                  f"'' -> '{inferred}'")
            opp.division = inferred
            changed += 1

        fallbacks = sum(
            1 for opp in rows if opp.division == DEFAULT_DIVISION
        )
        print(f"\nPlanned updates: {changed} of {len(rows)} empty-division rows "
              f"({fallbacks} would remain on the '{DEFAULT_DIVISION}' fallback).")

        if not args.apply:
            session.rollback()
            print("DRY RUN - no changes written. Re-run with --apply to persist.")
            return 0

        # commit happens when the session_scope context exits cleanly
        print("Changes committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
