"""Sterile, resolution-only Apply click primitives.

This module intentionally has no dependency on candidate profiles, documents,
answer banks, application filling, run modes, or submission authority.  It can
consume only a page-like browser handle plus the bounded public metadata of one
previously classified Apply affordance.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
import unicodedata

from app.services.requisition_identity import (
    RequisitionIdentity,
    canonical_requisition,
    identity_matches_url,
)


_DENIED_ACTION_NAME = re.compile(
    r"\b(?:submit|send|confirm|delete|withdraw|sign\s+out|payment|pay|checkout|"
    r"finish|continue|next)\b",
    re.IGNORECASE,
)
# This is deliberately an exact, normalized vocabulary.  In particular,
# "application", "autofill my application", and arbitrary labels containing
# the word "apply" are not enough to cross the click boundary.
_EXPLICIT_APPLY_NAMES = frozenset(
    {
        "apply",
        "apply now",
        "apply for this job",
        "apply for this position",
        "apply here",
        "apply on employer site",
        "apply online",
        "begin application",
        "start application",
        "apply with linkedin",
        "apply with indeed",
        "apply with google",
        "apply with dropbox",
    }
)
_ALLOWED_ROLES = frozenset({"button", "link"})


def _apply_name_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Decorative arrows, bullets, and punctuation are not part of the
    # accessible-name decision.  Words remain exact and ordered.
    return " ".join(
        "".join(character if character.isalnum() else " " for character in text)
        .split()
    )[:200]


def normalise_accessible_name(value: object) -> str:
    """Return the bounded, human-readable name used by the click gate."""

    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).split()
    ).strip()[:200]


def is_denied_action_name(value: object) -> bool:
    return bool(_DENIED_ACTION_NAME.search(_apply_name_key(value)))


def is_explicit_apply_name(value: object) -> bool:
    """Return whether a control name is an explicit non-submit Apply action."""

    name = _apply_name_key(value)
    return bool(name) and not is_denied_action_name(name) and name in _EXPLICIT_APPLY_NAMES


@dataclass(frozen=True, slots=True)
class ApplyCandidate:
    """Read-only candidate metadata used for deterministic ranking."""

    accessible_name: str
    role: str
    href: str = ""
    inside_job_root: bool = False
    first_party: bool = False
    third_party: bool = False
    selector: str = ""


def _candidate_value(candidate: ApplyCandidate | Mapping[str, object], key: str) -> object:
    if isinstance(candidate, ApplyCandidate):
        return getattr(candidate, key)
    return candidate.get(key)


def _coerce_candidate(candidate: ApplyCandidate | Mapping[str, object]) -> ApplyCandidate | None:
    if isinstance(candidate, ApplyCandidate):
        return candidate
    if not isinstance(candidate, Mapping):
        return None
    return ApplyCandidate(
        accessible_name=normalise_accessible_name(candidate.get("accessible_name")),
        role=_normalise_role(candidate.get("role")),
        href=normalise_accessible_name(candidate.get("href")),
        inside_job_root=bool(candidate.get("inside_job_root")),
        first_party=bool(candidate.get("first_party")),
        third_party=bool(candidate.get("third_party")),
        selector=normalise_accessible_name(candidate.get("selector")),
    )


def select_ranked_apply_candidate(
    candidates: Iterable[ApplyCandidate | Mapping[str, object]],
) -> ApplyCandidate | None:
    """Select one candidate only when the ordering is decisive.

    Exact duplicate destinations are collapsed.  First-party candidates and
    controls inside the bound job root outrank social/third-party candidates;
    a remaining tie is an explicit refusal rather than a guess.
    """

    normalised: list[ApplyCandidate] = []
    for raw in candidates:
        candidate = _coerce_candidate(raw)
        if candidate is None or not is_explicit_apply_name(candidate.accessible_name):
            continue
        if _normalise_role(candidate.role) not in _ALLOWED_ROLES:
            continue
        normalised.append(candidate)
    deduplicated: dict[tuple[str, str, str], ApplyCandidate] = {}
    for index, candidate in enumerate(normalised):
        key = (
            _apply_name_key(candidate.href),
            _apply_name_key(candidate.accessible_name),
            str(index) if not candidate.href else "",
        )
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = candidate
            continue
        if candidate.selector and not existing.selector:
            deduplicated[key] = candidate
    normalised = list(deduplicated.values())
    if not normalised:
        return None

    def score(candidate: ApplyCandidate) -> tuple[int, int, int, int]:
        return (
            1 if candidate.first_party else 0,
            1 if candidate.inside_job_root else 0,
            0 if candidate.third_party else 1,
            1 if _normalise_role(candidate.role) == "button" else 0,
        )

    ranked = sorted(
        normalised,
        key=lambda candidate: (*score(candidate), candidate.href, candidate.selector),
        reverse=True,
    )
    best_score = score(ranked[0])
    best = [candidate for candidate in ranked if score(candidate) == best_score]
    return best[0] if len(best) == 1 else None


class ApplyClickSafetyViolation(RuntimeError):
    """A click crossed the resolution-only boundary into confirmation/submission."""

    def __init__(
        self,
        message: str,
        *,
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.audit_evidence: dict[str, object] = (
            dict(evidence) if isinstance(evidence, Mapping) else {}
        )

    @classmethod
    def raise_if_landing_is_submission(
        cls,
        *,
        url: str,
        title: str = "",
        text: str = "",
        observation: Mapping[str, object] | None = None,
    ) -> None:
        if is_submission_or_confirmation_landing(
            url=url,
            title=title,
            text=text,
            observation=observation,
        ):
            raise cls(
                "Apply click landed on a confirmation/submitted page; "
                "the resolution-only run is halted"
            )


def is_submission_or_confirmation_landing(
    *,
    url: str = "",
    title: str = "",
    text: str = "",
    observation: Mapping[str, object] | None = None,
) -> bool:
    """Detect a final confirmation/submitted landing without clicking anything."""

    if isinstance(observation, Mapping) and any(
        bool(observation.get(key))
        for key in (
            "confirmation_visible",
            "submitted_visible",
            "submission_confirmed",
            "application_submitted",
        )
    ):
        return True
    material = " ".join((str(url or ""), str(title or ""), str(text or ""))).casefold()
    if re.search(r"/(?:confirmation|confirmed|submitted)(?:[/?#]|$)", material):
        return True
    return bool(
        re.search(
            r"\b(?:application\s+(?:has\s+been\s+)?submitted|submission\s+confirmed|"
            r"application\s+received|application\s+confirmation|thank\s+you\s+for\s+"
            r"(?:applying|your\s+application)|confirmation\s+number)\b",
            material,
        )
    )


class ApplyClickBudget:
    """One process-local, thread-safe click allowance for a resolution run."""

    def __init__(self, limit: int) -> None:
        limit = int(limit)
        if limit < 1:
            raise ValueError("Apply click budget limit must be positive")
        self.limit = limit
        self._consumed = 0
        self._lock = threading.Lock()

    def try_consume(self) -> bool:
        with self._lock:
            if self._consumed >= self.limit:
                return False
            self._consumed += 1
            return True

    @property
    def consumed(self) -> int:
        with self._lock:
            return self._consumed

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._consumed)


@dataclass(frozen=True, slots=True)
class ApplyAffordance:
    """Bounded metadata for one unique, identity-rooted Apply control."""

    selector: str
    accessible_name: str
    role: str
    root_selector: str = ""
    requisition: str = ""
    provider: str = ""
    tenant: str = ""
    root_employer: str = ""
    root_role_title: str = ""
    frame_url: str = ""
    frame_index: int = -1


@dataclass(frozen=True, slots=True)
class ApplyClickResult:
    """Non-sensitive result of the element-level click boundary."""

    clicked: bool
    outcome: str
    accessible_name: str
    role: str


def _normalise_name(value: object) -> str:
    return " ".join(str(value or "").casefold().split())[:200]


def _normalise_role(value: object) -> str:
    return str(value or "").casefold().strip()[:40]


def _resolved_control(locator: Any) -> tuple[str, str, str]:
    raw = locator.evaluate(
        r"""
        element => {
          const tag = String(element.tagName || '').toLowerCase();
          const explicitRole = String(element.getAttribute('role') || '')
            .trim().toLowerCase();
          const role = explicitRole ||
            (tag === 'button' ? 'button' : tag === 'a' ? 'link' : '');
          const labelledBy = String(element.getAttribute('aria-labelledby') || '')
            .split(/\s+/).filter(Boolean).map(id => document.getElementById(id))
            .filter(Boolean).map(node => node.innerText || node.textContent || '')
            .join(' ');
          const accessibleName = String(
            element.getAttribute('aria-label') || labelledBy ||
            element.getAttribute('title') || element.innerText ||
            element.textContent || element.getAttribute('value') || ''
          ).replace(/\s+/g, ' ').trim().slice(0, 200);
          return {
            accessible_name: accessibleName,
            role,
            type: String(element.getAttribute('type') || '').toLowerCase(),
          };
        }
        """
    )
    if not isinstance(raw, Mapping):
        return "", "", ""
    return (
        str(raw.get("accessible_name") or "").strip()[:200],
        _normalise_role(raw.get("role")),
        str(raw.get("type") or "").casefold().strip()[:40],
    )


def _click_scope(page: Any, affordance: ApplyAffordance) -> Any | None:
    """Return the exact inspected document, refusing an ambiguous frame."""

    if affordance.frame_index < 0:
        return page
    try:
        frames = list(getattr(page, "frames", ()) or ())
    except Exception:  # noqa: BLE001 - an opaque frame tree is unsafe
        return None
    matches = [
        frame for index, frame in enumerate(frames)
        if index == affordance.frame_index
    ]
    if len(matches) != 1:
        return None
    frame = matches[0]
    if affordance.frame_url:
        current_url = str(getattr(frame, "url", "") or "").strip()
        if current_url and current_url != affordance.frame_url:
            return None
    return frame


def guarded_apply_click(
    page: Any,
    affordance: ApplyAffordance,
    *,
    timeout_ms: int,
) -> ApplyClickResult:
    """Click one revalidated Apply control, or refuse before any action.

    The helper has no loop and calls ``click`` at most once.  It never types,
    uploads, submits a form explicitly, or resolves a second control.
    """

    scope = _click_scope(page, affordance)
    if scope is None:
        return ApplyClickResult(False, "apply_click_frame_ambiguous", "", "")
    try:
        locator = scope.locator(affordance.selector)
    except Exception:  # noqa: BLE001 - an opaque frame is unsafe
        return ApplyClickResult(False, "apply_click_frame_ambiguous", "", "")
    try:
        count = int(locator.count())
    except Exception:  # noqa: BLE001 - opaque control is refused
        count = 0
    if count != 1:
        return ApplyClickResult(False, "apply_click_ambiguous", "", "")

    try:
        current_name, current_role, current_type = _resolved_control(locator)
    except Exception:  # noqa: BLE001 - opaque control is refused
        return ApplyClickResult(False, "apply_click_inspection_failed", "", "")
    normalised_name = _normalise_name(current_name)
    expected_name = _normalise_name(affordance.accessible_name)
    expected_role = _normalise_role(affordance.role)

    if (
        current_type == "submit"
        or is_denied_action_name(normalised_name)
        or is_denied_action_name(current_role)
    ):
        return ApplyClickResult(
            False,
            "apply_click_submit_denylist_refused",
            current_name,
            current_role,
        )
    if (
        not normalised_name
        or not is_explicit_apply_name(normalised_name)
        or current_role not in _ALLOWED_ROLES
    ):
        return ApplyClickResult(
            False,
            "apply_click_not_apply_control",
            current_name,
            current_role,
        )
    if normalised_name != expected_name or current_role != expected_role:
        return ApplyClickResult(
            False,
            "apply_click_affordance_changed",
            current_name,
            current_role,
        )

    # A click-only fallback may not have a stored requisition.  It is still
    # required to remain inside the exact role/employer root selected during
    # discovery, and that identity must be re-proven immediately before the
    # sole possible click.
    if affordance.root_selector and (
        affordance.root_employer or affordance.root_role_title
    ):
        try:
            identity_binding = locator.evaluate(
                r"""
                (element, args) => {
                  const root = element.closest(args.rootSelector);
                  if (!root) return {bound: false, employer: false, role: false};
                  const normalise = value => String(value || '').normalize('NFKC')
                    .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
                  const text = normalise(
                    `${root.innerText || root.textContent || ''} ` +
                    `${root.getAttribute?.('data-employer') || ''} ` +
                    `${root.getAttribute?.('data-company') || ''} ` +
                    `${root.getAttribute?.('data-company-name') || ''} ` +
                    `${root.getAttribute?.('data-role') || ''} ` +
                    `${root.getAttribute?.('data-role-title') || ''} ` +
                    `${root.getAttribute?.('data-job-title') || ''}`
                  );
                  const contains = expected => {
                    const value = normalise(expected);
                    return Boolean(value) && ` ${text} `.includes(` ${value} `);
                  };
                  return {
                    bound: true,
                    employer: contains(args.employer),
                    role: contains(args.role),
                  };
                }
                """,
                {
                    "rootSelector": affordance.root_selector,
                    "employer": affordance.root_employer,
                    "role": affordance.root_role_title,
                },
            )
            identity_exact = (
                isinstance(identity_binding, Mapping)
                and bool(identity_binding.get("bound"))
                and bool(identity_binding.get("employer"))
                and bool(identity_binding.get("role"))
            )
        except Exception:  # noqa: BLE001 - changed/opaque root is refused
            identity_exact = False
        if not identity_exact:
            return ApplyClickResult(
                False,
                "apply_click_identity_root_changed",
                current_name,
                current_role,
            )

    # The discovery marker is not authority: a page can mutate between DOM
    # inspection and the click.  Re-resolve the control's exact marked root
    # and prove the stored requisition again immediately before acting.
    if affordance.requisition:
        if not (
            affordance.root_selector and affordance.provider and affordance.tenant
        ):
            return ApplyClickResult(
                False,
                "apply_click_requisition_changed",
                current_name,
                current_role,
            )
        try:
            binding = locator.evaluate(
                r"""
                (element, args) => {
                  const root = element.closest(args.rootSelector);
                  if (!root) return {bound: false, values: []};
                  const selector =
                    '[data-requisition], [data-job-id], [data-posting-id], ' +
                    '[data-requisition-id]';
                  const nodes = [root, ...Array.from(root.querySelectorAll(selector))];
                  const values = [];
                  for (const node of nodes) {
                    for (const name of [
                      'data-requisition', 'data-job-id', 'data-posting-id',
                      'data-requisition-id'
                    ]) {
                      const value = String(node.getAttribute?.(name) || '')
                        .normalize('NFKC').toLowerCase()
                        .replace(/\s+/g, ' ').trim();
                      if (value) values.push(value);
                    }
                  }
                  return {bound: true, values: Array.from(new Set(values))};
                }
                """,
                {"rootSelector": affordance.root_selector},
            )
            values = (
                list(binding.get("values") or ())
                if isinstance(binding, Mapping) and bool(binding.get("bound"))
                else []
            )
            expected = canonical_requisition(affordance.requisition)
            exact = bool(values) and len(values) == 1 and str(values[0]) == expected
            if not values and isinstance(binding, Mapping) and bool(binding.get("bound")):
                identity = RequisitionIdentity(
                    affordance.provider,
                    affordance.tenant,
                    affordance.requisition,
                )
                exact = identity_matches_url(identity, str(getattr(scope, "url", "") or ""))
        except Exception:  # noqa: BLE001 - a changed/opaque root is refused
            exact = False
        if not exact:
            return ApplyClickResult(
                False,
                "apply_click_requisition_changed",
                current_name,
                current_role,
            )

    try:
        locator.click(timeout=int(timeout_ms))
    except Exception as exc:  # noqa: BLE001 - timeout/failure remains fail-closed
        outcome = (
            "apply_click_timeout"
            if "timeout" in type(exc).__name__.casefold()
            else "apply_click_failed"
        )
        return ApplyClickResult(False, outcome, current_name, current_role)
    return ApplyClickResult(
        True,
        "apply_click_clicked",
        current_name,
        current_role,
    )
