"""READ-ONLY Risk-0 root-cause audit of NEEDS_USER / blocked applications.

Aggregates ``question_records`` (mapping_status blocked/omitted) from the
LIVE database, excluding genuinely human handoffs (CAPTCHA, legal
declarations, protected demographic questions), and prints a
label -> count -> proposed answer-key report so the answer bank can be
expanded where it is safe to do so.

This script never writes: the SQLite connection is opened in read-only
URI mode (``mode=ro``), so it is safe to run while the ARGUS server is up.

Usage (from the ARGUS repo root):
    ./.venv/Scripts/python.exe scripts/risk0_audit.py
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_answers import propose_key  # noqa: E402

DEFAULT_DB = Path.home() / "AppData" / "Local" / "ARGUS" / "argus.db"

# Reasons that are correctly paused for a human - not answer-bank problems.
_HUMAN_REASON_PATTERNS = (
    "%CAPTCHA detected%",
    "%Legal declaration requires human approval%",
    "%Protected or sensitive demographic question%",
)

_UI_FIELD_TYPES = frozenset({"search", "password", "file"})


def audit_rows(db_path: Path) -> list[tuple[str, str, str, int]]:
    """Return (label, field_type, reason, count) for fixable blocked/omitted labels."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cursor = con.cursor()
        query = "SELECT label, field_type, reason, COUNT(*) FROM question_records WHERE mapping_status IN ('blocked', 'omitted')"
        for pattern in _HUMAN_REASON_PATTERNS:
            query += f" AND reason NOT LIKE '{pattern}'"
        query += " GROUP BY label, field_type, reason ORDER BY 4 DESC"
        return list(cursor.execute(query).fetchall())
    finally:
        con.close()


def main() -> int:
    db_path = DEFAULT_DB
    if not db_path.exists():
        print(f"live database not found: {db_path}", file=sys.stderr)
        return 1

    rows = audit_rows(db_path)
    counts: Counter[str] = Counter()
    meta: dict[str, tuple[str, str]] = {}
    human_total = 0
    ui_total = 0

    # Human-handoff volume (context only, excluded from the fix table).
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    for pattern in _HUMAN_REASON_PATTERNS:
        (n,) = con.execute(
            "SELECT COUNT(*) FROM question_records WHERE mapping_status IN "
            "('blocked','omitted') AND reason LIKE ?",
            (pattern,),
        ).fetchone()
        human_total += n
    con.close()

    for label, field_type, _reason, count in rows:
        proposal = propose_key(label, field_type)
        if proposal == "(site UI, not a question)":
            ui_total += count
            continue
        key = (label, field_type)
        counts[key] = count
        meta[key] = (_reason, proposal)

    total_fixable = sum(counts.values())
    actionable = sorted(
        (
            ((label, ft), c, meta[(label, ft)])
            for (label, ft), c in counts.items()
            if not meta[(label, ft)][1].startswith("(")
        ),
        key=lambda item: -item[1],
    )
    human_review = [
        ((label, ft), c)
        for (label, ft), c in counts.items()
        if meta[(label, ft)][1].startswith("(human review")
    ]
    no_proposal = sorted(
        (
            ((label, ft), c)
            for (label, ft), c in counts.items()
            if meta[(label, ft)][1] == "(no deterministic proposal)"
        ),
        key=lambda item: -item[1],
    )

    print("=== Risk-0 NEEDS_USER root-cause audit "
          f"(live DB: {db_path.name}, read-only) ===")
    print(f"genuinely human (correctly paused, CAPTCHA/legal/demographic): "
          f"{human_total}")
    print(f"site-UI artefacts misread as questions (search/password/file): "
          f"{ui_total}")
    print(f"unmapped question instances total: {total_fixable}\n")

    header = f"{'count':>5}  {'field':<9} {'proposed key':<24} label"
    print("--- A. answerable now (seed/answer-bank candidates) ---")
    print(header)
    print("-" * len(header))
    for (label, field_type), count, (_reason, proposal) in actionable:
        short_label = label if len(label) <= 55 else label[:52] + "..."
        print(f"{count:>5}  {field_type:<9} {proposal:<24} {short_label}")
    print(f"\n=> {len(actionable)} labels covering "
          f"{sum(c for _, c, _ in actionable)} instances are answerable; "
          "expand via scripts/seed_answers.py")

    print("\n--- B. stay with a human (protected/factual) ---")
    for (label, _ft), count in sorted(human_review, key=lambda kv: -kv[1])[:10]:
        short_label = label if len(label) <= 60 else label[:57] + "..."
        print(f"{count:>5}  {short_label}")
    if len(human_review) > 10:
        more = human_review[10:]
        print(f"     (+{len(more)} more labels, "
              f"{sum(c for _, c in more)} instances)")

    print("\n--- C. no deterministic proposal (top 20) ---")
    for (label, ft), count in no_proposal[:20]:
        short_label = label if len(label) <= 55 else label[:52] + "..."
        print(f"{count:>5}  {ft:<9} {short_label}")
    if len(no_proposal) > 20:
        rest = no_proposal[20:]
        print(f"     (+{len(rest)} more labels, "
              f"{sum(c for _, c in rest)} instances)")
    print(f"\nno-proposal instances total: "
          f"{sum(c for _, c in no_proposal)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
