"""Read-only audit: would CV tag matching pick a firm-specific CV?

Replicates the ApplicationService.prepare desired-tag construction
(employer_slug + variant_tag + division/location slugs) and the
DocumentService.select_approved max-overlap selection against every approved
CV document, for each currently OPEN opportunity.

Opens the live database via a read-only SQLite URI (mode=ro) - safe to run
while the ARGUS server is running; never writes anything.

Usage:
    ./.venv/Scripts/python.exe scripts/cv_tag_audit.py [path/to/argus.db]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from app.config import Settings

DEFAULT_DB = Path.home() / "AppData" / "Local" / "ARGUS" / "argus.db"

# States in which an opportunity's application is still actionable - mirrors
# ScoutService.OPEN_STATES.
OPEN_APPLICATION_STATES = (
    "DISCOVERED",
    "ELIGIBILITY_CHECKED",
    "QUEUED",
    "PACKAGE_PREPARED",
    "FILLING",
    "FAILED_RETRYABLE",
    "READY_TO_SUBMIT",
)


def slugify(value: str) -> str:
    return value.strip().casefold().replace(" ", "-").replace(".", "")


def main(argv: list[str]) -> int:
    db_path = Path(argv[0]) if len(argv) > 1 else DEFAULT_DB
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cvs = [
            (json.loads(row["tags_json"] or "[]"))
            for row in conn.execute(
                "SELECT tags_json FROM documents WHERE kind='cv' AND approved=1"
            )
        ]
        opps = conn.execute(
            """
            SELECT DISTINCT o.id, o.employer, o.role_title, o.division,
                            o.programme_group, o.location
            FROM opportunities o
            JOIN applications a ON a.opportunity_id = o.id
            WHERE a.state IN (%s)
            """
            % ",".join("?" * len(OPEN_APPLICATION_STATES)),
            OPEN_APPLICATION_STATES,
        ).fetchall()
    finally:
        conn.close()

    if not cvs:
        print("No approved CV documents found - nothing to match against.")
        return 0
    cv_tag_sets = [frozenset(slugify(tag) for tag in tags) for tags in cvs]

    firm_hits = 0
    variant_only = 0
    no_match = 0
    unmatched_firms: Counter[str] = Counter()
    unmatched_desired: Counter[tuple[str, ...]] = Counter()

    for opp in opps:
        employer_slug = slugify(opp["employer"]) if opp["employer"].strip() else ""
        group = (opp["programme_group"] or "").casefold()
        variant_tag = "summer-cv" if group in {"summer", ""} else "yii-cv"
        desired = frozenset(
            value
            for value in (
                employer_slug,
                variant_tag,
                *(slugify(opp["division"]) if opp["division"].strip() else ""),
                *(slugify(opp["location"]) if opp["location"].strip() else ""),
            )
            if value
        )
        # select_approved picks the doc maximising |desired ∩ doc_tags|.
        best_overlap = 0
        best_doc_tags: frozenset[str] = frozenset()
        for tags in cv_tag_sets:
            overlap = len(desired & tags)
            if overlap > best_overlap:
                best_overlap, best_doc_tags = overlap, tags
        firm_specific = (
            best_overlap >= 2
            and employer_slug in desired
            and employer_slug in best_doc_tags
        )
        if firm_specific:
            firm_hits += 1
        elif best_overlap >= 1:
            variant_only += 1
            unmatched_firms[opp["employer"]] += 1
            unmatched_desired[tuple(sorted(desired))] += 1
        else:
            no_match += 1
            unmatched_firms[opp["employer"]] += 1
            unmatched_desired[tuple(sorted(desired))] += 1

    total = len(opps)
    print(f"Approved CVs: {len(cvs)}")
    print(f"Open opportunities: {total}")
    if total:
        pct = lambda n: f"{100.0 * n / total:.1f}%"
        print(f"  firm-specific CV match (>=2 tag overlap incl firm slug): "
              f"{firm_hits} ({pct(firm_hits)})")
        print(f"  variant-only fallback match:                              "
              f"{variant_only} ({pct(variant_only)})")
        print(f"  no match at all:                                          "
              f"{no_match} ({pct(no_match)})")
        print("\nTop firms without a firm-specific CV match:")
        for firm, count in unmatched_firms.most_common(10):
            print(f"  {count:>4}  {firm}")
        print("\nMost common unmatched desired-tag sets:")
        for tags, count in unmatched_desired.most_common(10):
            print(f"  {count:>4}  {', '.join(tags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
