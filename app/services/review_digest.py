from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.states import ApplicationState
from app.models import Application
from app.services.notifications import NotificationService


# This module is intentionally NOT wired into app/main.py yet —
# that integration is a deliberate separate step, out of scope here.


def build_review_digest(session: Session) -> dict[str, int]:
    """Query application counts into review-relevant buckets using real ApplicationState values."""
    state_counts: dict[str, int] = dict(
        session.execute(
            select(Application.state, func.count(Application.id)).group_by(
                Application.state
            )
        ).all()
    )
    ready_for_review = (
        state_counts.get(ApplicationState.NEEDS_USER.value, 0)
        + state_counts.get(ApplicationState.NEEDS_OA.value, 0)
        + state_counts.get(ApplicationState.READY_TO_SUBMIT.value, 0)
    )
    blocked = state_counts.get(ApplicationState.BLOCKED.value, 0)
    failed = state_counts.get(ApplicationState.FAILED_RETRYABLE.value, 0)
    return {
        "ready_for_review": ready_for_review,
        "blocked": blocked,
        "failed": failed,
    }


def send_review_digest(app: Any) -> bool:
    """Build digest via existing patterns; send ONE summary via NotificationService if any count >0."""
    state = getattr(app, "state", None)
    if state is None:
        return False
    notifier = getattr(state, "notifier", None)
    settings = getattr(state, "settings", None)
    if notifier is None or not getattr(notifier, "enabled", False):
        if settings is None:
            return False
        notifier = NotificationService.from_settings(settings)
    if not getattr(notifier, "enabled", False):
        return False
    db = getattr(state, "db", None)
    if db is None:
        return False
    with db.session_scope() as session:
        digest = build_review_digest(session)
    total = sum(int(v) for v in digest.values())
    if total <= 0:
        return False
    message = (
        f"ARGUS review digest: {digest['ready_for_review']} ready_for_review, "
        f"{digest['blocked']} blocked, {digest['failed']} failed."
    )
    payload: dict[str, object] = {
        "event": "review_digest",
        "idempotency_key": uuid.uuid4().hex,
        "count": total,
        "message": message,
        "digest": digest,
    }
    try:
        # Reuse the already-constructed backend (stdout/fanout/ntfy etc) for delivery.
        # This avoids any parallel mechanism and matches how notify_discovery works.
        backend = getattr(notifier, "_backend", None)
        if backend is not None:
            backend.send(payload)
        return True
    except Exception:  # noqa: BLE001 - digest is never load-bearing
        return False
