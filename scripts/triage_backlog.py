"""Backlog triage: un-stick BLOCKED / FAILED_RETRYABLE applications.

Uses ONLY legal state-machine transitions:
  BLOCKED -> ELIGIBILITY_CHECKED/QUEUED and FAILED_RETRYABLE -> QUEUED via
  ApplicationService.queue() (the API's POST /api/applications/{id}/queue path).

Categories handled:
  - field_fill_domain_not_trusted residue: allowlist was fixed at runtime;
    these rows pre-date the fix and are safe to requeue.
  - "Review accidental requeue repair before retry" residue of the
    2026-08-22 batch: per-app queue() is the documented repair.
NOT touched:
  - unknown_ats generic adapter pages (human-only today)
  - anything whose blocked reasons include CAPTCHA/legal/sensitive markers

Run with the ARGUS server STOPPED or via a second process is unsafe on SQLite:
default is --dry-run; pass --apply to write. Idempotent: already-open states
are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

REQUEUEABLE_REASON_MARKERS = (
    "field_fill_domain_not_trusted",
    "destination_greenhouse_unverified",
    "destination_lever_unverified",
    "destination_workday_unverified",
    "destination_*_unverified",
)
HUMAN_MARKERS = ("captcha", "legal", "sensitive", "demographic", "unknown_ats")


def classify(reasons: list[str]) -> str:
    joined = "|".join(reasons).casefold()
    if any(marker in joined for marker in HUMAN_MARKERS):
        return "human"
    if any(marker in joined for marker in REQUEUEABLE_REASON_MARKERS):
        return "requeueable"
    if not reasons:
        # no reasons recorded: legacy rows from before reason capture
        return "legacy"
    return "review"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="max apps to requeue (0 = all)")
    args = parser.parse_args(argv)

    from sqlalchemy import select

    from app.config import Settings
    from app.db import configure_database
    from app.domain.states import ApplicationState
    from app.models import Application
    from app.security.crypto import CryptoBox
    from app.services.applications import (
        ApplicationBlockedError,
        ApplicationService,
    )

    settings = Settings.load()
    database = configure_database(settings)
    crypto = CryptoBox.from_path(settings.secret_key_path)

    counts: Counter[str] = Counter()
    with database.session_scope() as session:
        rows = session.execute(
            select(Application).where(
                Application.state.in_(
                    [
                        ApplicationState.BLOCKED.value,
                        ApplicationState.FAILED_RETRYABLE.value,
                    ]
                )
            )
        ).scalars().all()
        print(f"candidates: {len(rows)}")
        for application in rows:
            reasons = _reasons_for(application)
            category = classify(reasons)
            counts[category] += 1
            if category != "requeueable" and category != "legacy":
                continue
            if args.limit and counts["queued"] >= args.limit:
                break
            if not args.apply:
                counts["would_requeue"] += 1
                continue
            try:
                service = ApplicationService(session, settings, crypto)
                # BLOCKED -> ELIGIBILITY_CHECKED is the documented legal
                # re-entry transition; queue() only accepts the open states.
                if application.state == ApplicationState.BLOCKED.value:
                    application.next_action = "Queue application"
                    service.evaluate(application.opportunity_id)
                    self_session_application = session.get(
                        Application, application.id
                    )
                    if (
                        self_session_application is not None
                        and self_session_application.state
                        == ApplicationState.ELIGIBILITY_CHECKED.value
                    ):
                        service.queue(self_session_application.id)
                        session.commit()
                        counts["queued"] += 1
                    elif self_session_application is not None and self_session_application.state == ApplicationState.BLOCKED.value:
                        counts["still_blocked"] += 1
                    else:
                        counts["queued"] += 1
                    continue
                service.queue(application.id)
                session.commit()
                counts["queued"] += 1
            except ApplicationBlockedError as exc:
                counts["failed"] += 1
                print(f"  queue blocked {application.id}: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                counts["failed"] += 1
                print(f"  queue failed {application.id}: {exc}", file=sys.stderr)
                session.rollback()

    print(json.dumps(dict(counts), indent=2))
    return 0


def _reasons_for(application) -> list[str]:  # noqa: ANN001
    """Best-effort blocked-reason recovery from next_action text + eligibility."""
    text = (application.next_action or "").casefold()
    if "requeue repair" in text:
        return ["requeue_repair_residue"]
    try:
        data = json.loads(application.eligibility_json or "{}")
    except (TypeError, ValueError):
        data = {}
    reasons = data.get("blocked_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    return [str(item) for item in reasons]


if __name__ == "__main__":
    raise SystemExit(main())
