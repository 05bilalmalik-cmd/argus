"""One-shot navigation child used by the batch target resolver.

Protocol: one bounded JSON request on stdin, one bounded JSON response on
stdout, then an explicit parent ACK.  Keeping the root alive for the ACK gives
the parent an exact process-tree handle throughout browser teardown.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import fields
from types import MappingProxyType
from typing import Any, Mapping

from app.automation.targets import TargetResolution
from app.config import Settings
from app.db import Database
from app.services.batch_target_resolution import SingleUseNavigatorTargetResolver
from app.services.resolution_apply_click import (
    ApplyClickBudget,
    ApplyClickSafetyViolation,
)
from app.services.target_resolution import ResolutionContext


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _resolution_payload(value: TargetResolution | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "source_url": value.source_url,
        "final_url": value.final_url,
        "kind": value.kind.value,
        "provider": value.provider,
        "identity_verified": value.identity_verified,
        "form_verified": value.form_verified,
        "reason_codes": list(value.reason_codes),
        "evidence": _jsonable(value.evidence),
    }


def _run(request: Mapping[str, Any]) -> dict[str, Any]:
    allowed_context = {item.name for item in fields(ResolutionContext)}
    raw_context = request.get("context")
    if not isinstance(raw_context, Mapping) or set(raw_context) - allowed_context:
        raise ValueError("Invalid resolution context")
    context = ResolutionContext(**dict(raw_context))

    env = dict(os.environ)
    data_dir = str(request.get("data_dir") or "").strip()
    if not data_dir:
        raise ValueError("Missing child data directory")
    env["ARGUS_DATA_DIR"] = data_dir
    env["ARGUS_ENABLE_APPLY_CLICK"] = (
        "1" if bool(request.get("apply_click_authorized")) else "0"
    )
    env["ARGUS_APPLY_CLICK_RUN_CAP"] = "1"
    # Role-text matching is deliberately not activated by this protocol.
    env["ARGUS_ENABLE_ROLE_MATCH_V2"] = "0"
    settings = Settings.load(env)
    database = Database(settings)
    budget = ApplyClickBudget(1) if settings.apply_click_enabled else None
    resolver = SingleUseNavigatorTargetResolver(
        database,
        settings,
        apply_click_budget=budget,
    )
    raw = resolver(context)
    resolution: TargetResolution | None
    handoff: Mapping[str, object]
    if isinstance(raw, tuple) and len(raw) == 2:
        resolution = raw[0] if isinstance(raw[0], TargetResolution) else None
        handoff = raw[1] if isinstance(raw[1], Mapping) else MappingProxyType({})
    elif isinstance(raw, TargetResolution):
        resolution = raw
        handoff = MappingProxyType({})
    else:
        resolution = None
        handoff = raw if isinstance(raw, Mapping) else MappingProxyType({})
    return {
        "ok": True,
        "resolution": _resolution_payload(resolution),
        "handoff": _jsonable(handoff),
        "click_budget_consumed": budget.consumed if budget is not None else 0,
    }


def main() -> int:
    try:
        line = sys.stdin.readline(1_048_577)
        if not line or len(line.encode("utf-8")) > 1_048_576:
            raise ValueError("Missing or oversized child request")
        request = json.loads(line)
        if not isinstance(request, Mapping):
            raise ValueError("Child request must be an object")
        response = _run(request)
    except ApplyClickSafetyViolation as exc:
        # This is a safety stop, not a retryable browser error.  The parent
        # reconstructs the typed exception and halts the priority run.
        response = {
            "ok": False,
            "fatal_safety_violation": True,
            "error_type": "applyclicksafetyviolation",
            "apply_click": _jsonable(getattr(exc, "audit_evidence", {})),
        }
    except BaseException as exc:  # noqa: BLE001 - typed bounded child failure
        response = {
            "ok": False,
            "error_type": type(exc).__name__.casefold()[:120],
        }
    print(json.dumps(response, separators=(",", ":"), sort_keys=True), flush=True)
    ack = sys.stdin.readline(32)
    return 0 if ack.strip() == "ACK" else 3


if __name__ == "__main__":
    raise SystemExit(main())
