"""Pure policy helpers for the candidate-started PREFILL path."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from app.automation.host_policy import normalise_hostname, origin_for_url
from app.domain.states import user_status_exclusion_reason
from app.domain.targets import TargetKind, validate_navigation_url


PREFILL_TARGET_STATUS = TargetKind.APPLICATION_FORM.value
PREFILL_APPLICATION_STATES = frozenset(
    {
        "PACKAGE_PREPARED",
        "FILLING",
        "NEEDS_USER",
        "FAILED_RETRYABLE",
        "READY_TO_SUBMIT",
        # BLOCKED deliberately excluded: the runner refuses BLOCKED rows
        # (``resume_not_allowed``).  A BLOCKED row should go through human
        # re-evaluation (``/resolve-blocker``) rather than being offered a
        # prefill button that would silently fail.  The candidate-facing
        # control therefore does not render for BLOCKED applications.
    }
)


class PrefillBlocked(RuntimeError):
    """A candidate start was refused before browser ownership."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def prefill_start_reason(
    *,
    target_status: object,
    user_status: object,
    application_state: object,
    live_session: bool = False,
) -> tuple[str, str] | None:
    """Return ``(code, wall)`` for a refused start, or ``None`` when eligible."""

    excluded = user_status_exclusion_reason(user_status)
    if excluded is not None:
        return "user_status_excluded", excluded

    actual_target = str(target_status or TargetKind.UNRESOLVED.value).strip().upper()
    if actual_target != PREFILL_TARGET_STATUS:
        return (
            "target_not_application_form",
            "Prepare this application requires target APPLICATION_FORM; "
            f"the actual target is {actual_target or TargetKind.UNRESOLVED.value}.",
        )

    actual_state = str(application_state or "MISSING").strip().upper()
    if live_session:
        return (
            "prefill_session_active",
            "A live Navigator session already exists for this application; "
            "use Continue rather than starting another run.",
        )
    if actual_state not in PREFILL_APPLICATION_STATES:
        return (
            "application_state_not_prepareable",
            "Prepare this application is unavailable because the application "
            f"is in {actual_state}.",
        )
    return None


def prefill_ui_allowed(
    *,
    target_status: object,
    user_status: object,
    application_state: object,
    live_session: bool = False,
) -> bool:
    """Project the same server start gate into a rendered candidate control."""

    return prefill_start_reason(
        target_status=target_status,
        user_status=user_status,
        application_state=application_state,
        live_session=live_session,
    ) is None


def row_application_url_allowlist(application_url: object) -> frozenset[str]:
    """Derive one exact host allowlist from the row's stored application URL."""

    raw = str(application_url or "").strip()
    if not raw:
        raise PrefillBlocked(
            "No stored application_url is available for PREFILL",
            code="prefill_application_url_missing",
        )
    try:
        validated = validate_navigation_url(raw)
        # ``validate_navigation_url`` preserves the row's URL semantics; the
        # strict origin check additionally rejects malformed/ambiguous hosts.
        origin_for_url(validated)
        hostname = normalise_hostname(urlsplit(validated).hostname or "")
    except (TypeError, ValueError) as exc:
        raise PrefillBlocked(
            "The stored application_url is invalid for PREFILL",
            code="prefill_application_url_invalid",
        ) from exc
    if not hostname:
        raise PrefillBlocked(
            "The stored application_url has no usable host for PREFILL",
            code="prefill_application_url_invalid",
        )
    return frozenset({hostname})


def _safe_blocked_request(raw: object) -> dict[str, str] | None:
    """Keep only bounded host/classification evidence; discard query data."""

    if not isinstance(raw, Mapping):
        return None
    host = normalise_hostname(raw.get("host", ""))
    if not host:
        return None
    path = str(raw.get("path") or "").split("?", 1)[0].split("#", 1)[0]
    return {
        "host": host[:253],
        "path": path[:2048],
        "classification": str(raw.get("classification") or "unknown")[:80],
        "impact": str(raw.get("impact") or "unknown")[:80],
        "reason": str(raw.get("reason") or "blocked")[:160],
    }


def sanitise_blocked_requests(value: object) -> tuple[dict[str, str], ...]:
    """Return safe egress evidence suitable for a candidate-facing response."""

    if not isinstance(value, (list, tuple)):
        return ()
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value[:32]:
        safe = _safe_blocked_request(item)
        if safe is None:
            continue
        identity = (safe["host"], safe["path"], safe["classification"])
        if identity in seen:
            continue
        seen.add(identity)
        output.append(safe)
    return tuple(output)


def prefill_wall(
    *,
    run_error: object = "",
    blocked_requests: Iterable[Mapping[str, object]] = (),
    state: object = "",
) -> str:
    """Build a plain, bounded wall without exposing request query values."""

    blocked = sanitise_blocked_requests(tuple(blocked_requests))
    if blocked:
        classes = ", ".join(
            dict.fromkeys(
                f"{item['host']} ({item['classification']})" for item in blocked
            )
        )
        return (
            "Egress wall: the browser blocked a request to "
            f"{classes} before delivery; filling stopped. Review the visible browser."
        )[:500]
    error = " ".join(str(run_error or "").split())[:400].rstrip()
    if error:
        return f"Prefill wall: {error}"
    actual_state = str(state or "").strip().upper()
    return (
        f"Prefill ended at {actual_state or 'an unknown state'}; "
        "no Submit click was issued."
    )


HUMAN_BOUNDARY_COPY = (
    "Visible PREFILL is ready for human review. Confirm employer and role, "
    "complete any CAPTCHA or blank fields yourself, then press Submit yourself. "
    "ARGUS will not press Submit."
)
