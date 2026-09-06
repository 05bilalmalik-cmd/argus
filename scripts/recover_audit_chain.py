"""Honest recovery for a forked audit chain (forensic root cause 12).

The production database forked at event 4819: two sessions read the same
head and appended children with identical parent hashes.  Rewriting old
hashes to manufacture validity is forbidden.  Instead:

1. the broken prefix is preserved byte-for-byte in the live table;
2. this script writes an immutable provenance record documenting the fork
   (event ids, hashes, detection date);
3. a NEW verified epoch begins at the first event after the fork point:
   its ``previous_hash`` is anchored to the LAST VALID event before the
   fork, so the honest history remains cryptographically chained up to the
   break, and everything after is clearly marked as recovered.

Run:  python scripts/recover_audit_chain.py [--apply]
Without --apply it only diagnoses and prints the recovery plan.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.config import Settings
from app.db import Database
from app.security.audit import (
    AuditInput,
    _hash_event,
    append_audit,
    install_chain_trigger,
    verify_audit_chain,
)


def diagnose(session) -> dict:
    """Locate the fork: last valid event id/hash and all orphaned events."""

    events = list(
        session.execute(
            text(
                "SELECT id, created_at, actor, event_type, entity_type,"
                " entity_id, details_json, previous_hash, event_hash"
                " FROM audit_events ORDER BY id"
            )
        ).mappings()
    )
    previous_hash = ""
    fork_at = None
    last_valid = None
    for event in events:
        expected = _hash_event(
            previous_hash,
            event["created_at"],
            event["actor"],
            event["event_type"],
            event["entity_type"],
            event["entity_id"],
            event["details_json"],
        )
        if (
            fork_at is None
            and (
                event["previous_hash"] != previous_hash
                or event["event_hash"] != expected
            )
        ):
            fork_at = event["id"]
        if fork_at is None:
            last_valid = event
        previous_hash = event["event_hash"]
    return {
        "total_events": len(events),
        "forked_at_event": fork_at,
        "last_valid_event_id": last_valid["id"] if last_valid else None,
        "last_valid_hash": last_valid["event_hash"] if last_valid else "",
    }


def recover(settings: Settings, *, apply: bool) -> dict:
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()  # installs the integrity trigger on new appends

    with database.session_scope() as session:
        report = diagnose(session)

    if not apply:
        return {"mode": "diagnose", **report}

    anchor = report["last_valid_hash"]
    with database.session_scope() as session:
        append_audit(
            session,
            AuditInput(
                "recovery",
                "audit.epoch_recovered",
                "audit_chain",
                f"fork_at_{report['forked_at_event']}",
                {
                    "detected_broken_event": report["forked_at_event"],
                    "anchor_last_valid_event": report["last_valid_event_id"],
                    "anchor_hash": anchor,
                    "note": (
                        "Events from the fork onward are preserved as historical "
                        "evidence; they do not chain to this epoch's head. New "
                        "events continue from the last valid pre-fork hash."
                    ),
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                },
            ),
        )

    # The recovery event itself was appended with parent = current committed
    # head, which may be one of the orphaned post-fork events.  For an honest
    # epoch we must anchor to the last VALID hash instead.  Rewrite ONLY the
    # recovery event's own linkage (it belongs to this recovery, not history).
    with database.engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT id FROM audit_events WHERE event_type='audit.epoch_recovered'"
                " ORDER BY id DESC LIMIT 1"
            )
        ).first()
        if row is not None:
            connection.execute(
                text("UPDATE audit_events SET previous_hash=:h WHERE id=:i"),
                {"h": anchor, "i": row[0]},
            )
            # recompute its own hash over corrected parent
            ev = connection.execute(
                text(
                    "SELECT created_at, actor, event_type, entity_type,"
                    " entity_id, details_json, previous_hash, event_hash"
                    " FROM audit_events WHERE id=:i"
                ),
                {"i": row[0]},
            ).first()
            fixed = _hash_event(anchor, *ev[:6])
            connection.execute(
                text("UPDATE audit_events SET event_hash=:e WHERE id=:i"),
                {"e": fixed, "i": row[0]},
            )

    # Future appends must chain from THIS recovery event, not from the
    # orphaned tail.  The trigger enforces previous == committed max(id)
    # hash, so also verify what we can and report honestly.
    with database.session_scope() as session:
        verification = verify_audit_chain(session)
    return {
        "mode": "applied",
        **report,
        "chain_verification_after": {
            "valid": verification.valid,
            "broken_event_id": verification.broken_event_id,
            # Events after the recovery record may still not chain if the
            # orphaned tail has higher ids; the epoch marker documents this.
            "expected_limitation": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the recovery record")
    args = parser.parse_args()
    settings = Settings.load()
    result = recover(settings, apply=args.apply)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
