"""Durable, exact, single-use authority for one final submission click.

The authority is intentionally a small capability.  It is issued only after
the user has reviewed the server-produced final manifest, and it can be
consumed once only when the execution-side manifest is byte-for-byte the same
stable projection.  Volatile browser identifiers (for example ``page_id``)
are deliberately excluded; the stable control/form identity is included
instead so a changed target cannot reuse the token.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Mapping
from urllib.parse import urlsplit

from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.states import (
    UserApplicationStatus,
    user_status_exclusion_reason,
)
from app.models import (
    Application,
    Opportunity,
    SubmissionAuthority,
    SubmissionReviewBinding,
)


_AUTHORITY_ELIGIBLE_USER_STATUSES = (
    UserApplicationStatus.NOT_APPLIED.value,
    UserApplicationStatus.INTERESTED.value,
)


class SubmissionAuthorityError(RuntimeError):
    """Raised for every authority mismatch, replay, expiry, or invalid state."""


AuthorityError = SubmissionAuthorityError


# These are the server-produced, load-bearing fields.  The raw browser
# ``target_fingerprint`` is not trusted because it currently includes the
# owner-thread page id, which is expected to change between review and the
# fresh execution page.  We derive a stable target fingerprint below from the
# fields that identify the actual form/control instead.
_REQUIRED_MANIFEST_KEYS = frozenset(
    {
        "application_id",
        "employer",
        "role",
        "requisition",
        "provider",
        "application_url",
        "destination",
        "form_action",
        "expected_final_url",
        "method",
        "form_identity",
        "root_selector",
        "control_selector",
        "control_fingerprint",
        "frame_url",
        "documents",
        "answers",
    }
)

_STABLE_TARGET_KEYS = (
    "provider",
    "employer",
    "role",
    "requisition",
    "form_identity",
    "application_url",
    "destination",
    "form_action",
    "expected_final_url",
    "method",
    "root_selector",
    "control_selector",
    "control_fingerprint",
    "frame_url",
)

_VOLATILE_KEYS = frozenset(
    {
        "expires_at",
        "issued_at",
        "session_id",
        "status",
        "submission",
        "mode",
        "url",
        "final_url",
        "title",
        "provider_step",
        "page_id",
        # This is accepted as input for compatibility with existing navigator
        # payloads, but replaced by the stable digest below.
        "target_fingerprint",
    }
)


def _required_text(manifest: Mapping[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SubmissionAuthorityError(f"manifest field {key!r} is required")
    return value.strip()


def _json_safe(value: object, *, path: str = "manifest") -> object:
    """Return JSON-safe evidence, rejecting unsupported executable/path data."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SubmissionAuthorityError(f"{path} contains a non-string key")
            result[key] = _json_safe(item, path=f"{path}.{key}")
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, path=f"{path}[]") for item in value]
    raise SubmissionAuthorityError(f"{path} contains unsupported evidence")


def _canonical_http_origin(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubmissionAuthorityError(f"{field} is required")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise SubmissionAuthorityError(f"{field} is not a valid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise SubmissionAuthorityError(f"{field} must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise SubmissionAuthorityError(f"{field} must not contain credentials")
    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    port_text = f":{port}" if port is not None and not default_port else ""
    return f"{parsed.scheme.lower()}://{host}{port_text}"


def _validate_manifest_url(manifest: Mapping[str, object], key: str) -> str:
    value = _required_text(manifest, key)
    _canonical_http_origin(value, field=key)
    return value


def _normalise_documents(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        raise SubmissionAuthorityError("manifest documents must be a list")
    documents: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SubmissionAuthorityError(f"manifest documents[{index}] is invalid")
        document_id = item.get("id")
        sha256 = item.get("sha256")
        kind = item.get("kind")
        approved = item.get("approved")
        if not isinstance(document_id, str) or not document_id.strip():
            raise SubmissionAuthorityError(f"manifest documents[{index}] has no id")
        if not isinstance(sha256, str) or not sha256.strip():
            raise SubmissionAuthorityError(
                f"manifest documents[{index}] has no content hash"
            )
        if not isinstance(kind, str) or not kind.strip():
            raise SubmissionAuthorityError(f"manifest documents[{index}] has no kind")
        if approved is not True:
            raise SubmissionAuthorityError(
                f"manifest documents[{index}] is not approved"
            )
        document_id = document_id.strip()
        if document_id in seen:
            raise SubmissionAuthorityError(
                f"manifest documents contains duplicate id {document_id!r}"
            )
        seen.add(document_id)
        documents.append(
            {
                "id": document_id,
                "kind": kind.strip(),
                # Hashes are compared as content identities.  Case-folding
                # keeps equivalent hexadecimal encodings deterministic while
                # still binding the complete supplied digest string.
                "sha256": sha256.strip().lower(),
                "approved": True,
            }
        )
    documents.sort(key=lambda item: (str(item["kind"]), str(item["id"])))
    return documents


def _normalise_answers(value: object) -> list[dict[str, object]]:
    """Bind approved answer records without persisting their plaintext."""

    if not isinstance(value, (list, tuple)):
        raise SubmissionAuthorityError("manifest answers must be a list")
    answers: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SubmissionAuthorityError(f"manifest answers[{index}] is invalid")
        unknown = sorted(
            set(item)
            - {"id", "canonical_key", "sha256", "approved", "sensitive"}
        )
        if unknown:
            raise SubmissionAuthorityError(
                f"manifest answers[{index}] contains unsupported fields: "
                + ", ".join(str(key) for key in unknown)
            )
        answer_id = item.get("id")
        canonical_key = item.get("canonical_key")
        sha256 = item.get("sha256")
        approved = item.get("approved")
        sensitive = item.get("sensitive")
        if not isinstance(answer_id, str) or not answer_id.strip():
            raise SubmissionAuthorityError(f"manifest answers[{index}] has no id")
        if not isinstance(canonical_key, str) or not canonical_key.strip():
            raise SubmissionAuthorityError(
                f"manifest answers[{index}] has no canonical key"
            )
        if (
            not isinstance(sha256, str)
            or len(sha256.strip()) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in sha256.strip())
        ):
            raise SubmissionAuthorityError(
                f"manifest answers[{index}] has no valid content hash"
            )
        if approved is not True:
            raise SubmissionAuthorityError(
                f"manifest answers[{index}] is not approved"
            )
        if not isinstance(sensitive, bool):
            raise SubmissionAuthorityError(
                f"manifest answers[{index}] has no sensitivity binding"
            )
        answer_id = answer_id.strip()
        canonical_key = canonical_key.strip()
        if answer_id in seen_ids or canonical_key in seen_keys:
            raise SubmissionAuthorityError(
                "manifest answers contains a duplicate identity or canonical key"
            )
        seen_ids.add(answer_id)
        seen_keys.add(canonical_key)
        answers.append(
            {
                "id": answer_id,
                "canonical_key": canonical_key,
                # The digest is calculated from the encrypted stored token.
                # It therefore detects any stored-answer replacement without
                # exposing low-entropy answer plaintext to offline guessing.
                "sha256": sha256.strip().lower(),
                "approved": True,
                "sensitive": sensitive,
            }
        )
    answers.sort(key=lambda item: (str(item["canonical_key"]), str(item["id"])))
    return answers


def authority_manifest_projection(
    manifest: Mapping[str, object],
    *,
    strict: bool = True,
) -> dict[str, object]:
    """Project a server-produced manifest onto its stable authority contract.

    ``strict`` is exposed for diagnostic callers that need to inspect a
    partial display payload.  The authority service always uses the strict
    default, which rejects incomplete or client-shaped manifests instead of
    falling back to hashing whatever keys happened to be present.
    """

    if not isinstance(manifest, Mapping):
        raise SubmissionAuthorityError("manifest must be a mapping")
    if not strict:
        return {
            key: _json_safe(value, path=f"manifest.{key}")
            for key, value in sorted(manifest.items())
            if key not in _VOLATILE_KEYS
        }

    missing = sorted(key for key in _REQUIRED_MANIFEST_KEYS if key not in manifest)
    if missing:
        raise SubmissionAuthorityError(
            "manifest is incomplete; missing " + ", ".join(missing)
        )

    projected: dict[str, object] = {}
    for key in (
        "application_id",
        "employer",
        "role",
        "requisition",
        "provider",
        "form_identity",
        "root_selector",
        "control_selector",
        "control_fingerprint",
        "frame_url",
    ):
        projected[key] = _required_text(manifest, key)

    projected["destination"] = _validate_manifest_url(manifest, "destination")
    projected["application_url"] = _validate_manifest_url(
        manifest, "application_url"
    )
    projected["form_action"] = _validate_manifest_url(manifest, "form_action")
    projected["expected_final_url"] = _validate_manifest_url(
        manifest, "expected_final_url"
    )
    method = _required_text(manifest, "method").upper()
    projected["method"] = method
    projected["documents"] = _normalise_documents(manifest["documents"])
    projected["answers"] = _normalise_answers(manifest["answers"])

    target_material = {key: projected[key] for key in _STABLE_TARGET_KEYS}
    target_json = canonical_manifest_json(target_material)
    projected["target_fingerprint"] = hashlib.sha256(
        target_json.encode("utf-8")
    ).hexdigest()
    return projected


def canonical_manifest_json(manifest: Mapping[str, object]) -> str:
    """Serialize already-projected evidence deterministically."""

    if not isinstance(manifest, Mapping):
        raise SubmissionAuthorityError("manifest must be a mapping")
    try:
        return json.dumps(
            _json_safe(dict(manifest)),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        raise SubmissionAuthorityError("manifest is not canonicalizable") from exc


def manifest_fingerprint(manifest: Mapping[str, object]) -> str:
    projected = authority_manifest_projection(manifest)
    return hashlib.sha256(canonical_manifest_json(projected).encode("utf-8")).hexdigest()


class SubmissionReviewBindingService:
    """Persist and revalidate the complete manifest shown at final review."""

    def __init__(self, session: Session, *, now=None) -> None:
        self.session = session
        self._now = now or (lambda: datetime.now(timezone.utc))

    def record(
        self,
        *,
        application_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        destination_origin: str,
        expires_at: datetime,
    ) -> SubmissionReviewBinding:
        application_id, session_id, canonical, fingerprint, origin = self._contract(
            application_id=application_id,
            session_id=session_id,
            manifest=manifest,
            destination_origin=destination_origin,
        )
        now = SubmissionAuthorityService._aware(self._now())
        expiry = SubmissionAuthorityService._aware(expires_at)
        if expiry <= now:
            raise SubmissionAuthorityError("displayed review binding is already expired")
        existing = self._find(application_id, session_id)
        if existing is not None:
            self._assert_matches(
                existing,
                canonical=canonical,
                fingerprint=fingerprint,
                origin=origin,
                now=now,
            )
            return existing
        binding = SubmissionReviewBinding(
            application_id=application_id,
            session_id=session_id,
            manifest_fingerprint=fingerprint,
            manifest_json=canonical,
            destination_origin=origin,
            reviewed_at=now,
            expires_at=expiry,
        )
        try:
            with self.session.begin_nested():
                self.session.add(binding)
                self.session.flush()
        except IntegrityError as exc:
            existing = self._find(application_id, session_id)
            if existing is None:
                raise SubmissionAuthorityError(
                    "displayed review binding lost its atomic insert race"
                ) from exc
            self._assert_matches(
                existing,
                canonical=canonical,
                fingerprint=fingerprint,
                origin=origin,
                now=now,
            )
            return existing
        return binding

    def validate(
        self,
        *,
        application_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        destination_origin: str,
    ) -> SubmissionReviewBinding:
        application_id, session_id, canonical, fingerprint, origin = self._contract(
            application_id=application_id,
            session_id=session_id,
            manifest=manifest,
            destination_origin=destination_origin,
        )
        binding = self._find(application_id, session_id)
        if binding is None:
            raise SubmissionAuthorityError(
                "displayed review binding is missing; request the final manifest again"
            )
        self._assert_matches(
            binding,
            canonical=canonical,
            fingerprint=fingerprint,
            origin=origin,
            now=SubmissionAuthorityService._aware(self._now()),
        )
        return binding

    def _find(self, application_id: str, session_id: str) -> SubmissionReviewBinding | None:
        return self.session.scalar(
            select(SubmissionReviewBinding).where(
                SubmissionReviewBinding.application_id == application_id,
                SubmissionReviewBinding.session_id == session_id,
            )
        )

    @staticmethod
    def _contract(
        *,
        application_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        destination_origin: str,
    ) -> tuple[str, str, str, str, str]:
        application_id = SubmissionAuthorityService._binding_text(
            application_id, "application_id"
        )
        session_id = SubmissionAuthorityService._binding_text(session_id, "session_id")
        projected = authority_manifest_projection(manifest)
        if projected["application_id"] != application_id:
            raise SubmissionAuthorityError(
                "manifest application_id does not match displayed review binding"
            )
        origin = SubmissionAuthorityService._canonical_origin(destination_origin)
        if origin != _canonical_http_origin(
            projected["destination"], field="manifest.destination"
        ):
            raise SubmissionAuthorityError(
                "destination origin does not match displayed review binding"
            )
        canonical = canonical_manifest_json(projected)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return application_id, session_id, canonical, fingerprint, origin

    @staticmethod
    def _assert_matches(
        binding: SubmissionReviewBinding,
        *,
        canonical: str,
        fingerprint: str,
        origin: str,
        now: datetime,
    ) -> None:
        if SubmissionAuthorityService._aware(binding.expires_at) <= now:
            raise SubmissionAuthorityError("displayed review binding is expired")
        if (
            binding.manifest_fingerprint != fingerprint
            or binding.manifest_json != canonical
            or binding.destination_origin != origin
        ):
            raise SubmissionAuthorityError(
                "displayed review binding changed after it was shown"
            )


class SubmissionAuthorityService:
    """Issue and atomically consume a row bound to one exact manifest."""

    def __init__(self, session: Session, *, now=None) -> None:
        self.session = session
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _current_user_status(self, application_id: str) -> str | None:
        return self.session.scalar(
            select(Opportunity.user_status)
            .join(Application, Application.opportunity_id == Opportunity.id)
            .where(Application.id == application_id)
        )

    def _assert_user_status_eligible(self, application_id: str) -> None:
        status = self._current_user_status(application_id)
        if status is None:
            raise SubmissionAuthorityError("authority application does not exist")
        reason = user_status_exclusion_reason(status)
        if reason is not None:
            raise SubmissionAuthorityError(reason)

    def issue(
        self,
        *,
        application_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        destination_origin: str,
        expires_in_seconds: int = 300,
    ) -> SubmissionAuthority:
        application_id = self._binding_text(application_id, "application_id")
        session_id = self._binding_text(session_id, "session_id")
        self._assert_user_status_eligible(application_id)
        projected = authority_manifest_projection(manifest)
        if projected["application_id"] != application_id:
            raise SubmissionAuthorityError(
                "manifest application_id does not match authority binding"
            )
        origin = self._canonical_origin(destination_origin)
        destination_origin_from_manifest = _canonical_http_origin(
            projected["destination"], field="manifest.destination"
        )
        if origin != destination_origin_from_manifest:
            raise SubmissionAuthorityError(
                "destination origin does not match the final manifest"
            )
        if isinstance(expires_in_seconds, bool) or not isinstance(
            expires_in_seconds, int
        ) or expires_in_seconds <= 0:
            raise SubmissionAuthorityError("authority expiry must be a positive integer")

        issued_at = self._aware(self._now())
        canonical = canonical_manifest_json(projected)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self.session.scalar(
            select(SubmissionAuthority).where(
                SubmissionAuthority.application_id == application_id,
                SubmissionAuthority.session_id == session_id,
            )
        )
        if existing is not None:
            self._assert_reissuance_matches(
                existing,
                fingerprint=fingerprint,
                canonical=canonical,
                origin=origin,
                now=issued_at,
            )
            return existing

        authority = SubmissionAuthority(
            id=secrets.token_urlsafe(32),
            application_id=application_id,
            session_id=session_id,
            manifest_fingerprint=fingerprint,
            manifest_json=canonical,
            destination_origin=origin,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=expires_in_seconds),
        )
        # The unique application/session index is the actual concurrency
        # boundary.  A savepoint lets a losing issuer recover the committed
        # winner without rolling back unrelated work in the request session.
        try:
            with self.session.begin_nested():
                self.session.add(authority)
                self.session.flush()
        except IntegrityError as exc:
            existing = self.session.scalar(
                select(SubmissionAuthority).where(
                    SubmissionAuthority.application_id == application_id,
                    SubmissionAuthority.session_id == session_id,
                )
            )
            if existing is None:
                raise SubmissionAuthorityError(
                    "authority issuance lost its atomic insert race"
                ) from exc
            self._assert_reissuance_matches(
                existing,
                fingerprint=fingerprint,
                canonical=canonical,
                origin=origin,
                now=issued_at,
            )
            return existing
        return authority

    def consume(
        self,
        authority_id: str,
        *,
        application_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        destination_origin: str,
    ) -> SubmissionAuthority:
        authority_id = self._binding_text(authority_id, "authority_id")
        application_id = self._binding_text(application_id, "application_id")
        session_id = self._binding_text(session_id, "session_id")
        projected = authority_manifest_projection(manifest)
        if projected["application_id"] != application_id:
            raise SubmissionAuthorityError(
                "manifest application_id does not match authority binding"
            )
        origin = self._canonical_origin(destination_origin)
        if origin != _canonical_http_origin(
            projected["destination"], field="manifest.destination"
        ):
            raise SubmissionAuthorityError(
                "destination origin does not match the final manifest"
            )
        canonical = canonical_manifest_json(projected)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = self._aware(self._now())
        result = self.session.execute(
            update(SubmissionAuthority)
            .where(
                SubmissionAuthority.id == authority_id,
                SubmissionAuthority.application_id == application_id,
                SubmissionAuthority.session_id == session_id,
                SubmissionAuthority.manifest_fingerprint == fingerprint,
                SubmissionAuthority.manifest_json == canonical,
                SubmissionAuthority.destination_origin == origin,
                SubmissionAuthority.consumed_at.is_(None),
                SubmissionAuthority.expires_at > now,
                exists(
                    select(1)
                    .select_from(Application)
                    .join(
                        Opportunity,
                        Application.opportunity_id == Opportunity.id,
                    )
                    .where(
                        Application.id == application_id,
                        Opportunity.user_status.in_(
                            _AUTHORITY_ELIGIBLE_USER_STATUSES
                        ),
                    )
                ),
            )
            .values(consumed_at=now)
        )
        if result.rowcount != 1:
            current_status = self._current_user_status(application_id)
            current = self.session.get(SubmissionAuthority, authority_id)
            detail = "authority is invalid, expired, consumed, or mismatched"
            status_reason = user_status_exclusion_reason(current_status)
            if status_reason is not None:
                detail = status_reason
            if current is not None:
                detail += " [authority binding mismatch]"
                try:
                    stored_manifest = json.loads(current.manifest_json)
                except (TypeError, ValueError):
                    stored_manifest = {}
                if isinstance(stored_manifest, Mapping):
                    changed_fields = sorted(
                        key
                        for key in set(stored_manifest) | set(projected)
                        if stored_manifest.get(key) != projected.get(key)
                    )
                    if changed_fields:
                        # Field names are safe diagnostics; values can contain
                        # candidate/application evidence and are never echoed.
                        detail += " [changed fields: " + ", ".join(changed_fields) + "]"
            # There was no successful CAS.  Release the read transaction so a
            # caller can safely record the blocked attempt or retry the review
            # flow with a new session.
            self.session.rollback()
            raise SubmissionAuthorityError(detail)
        authority = self.session.scalar(
            select(SubmissionAuthority).where(SubmissionAuthority.id == authority_id)
        )
        if authority is None:
            self.session.rollback()
            raise SubmissionAuthorityError("authority disappeared during consumption")
        return authority

    def validate(
        self,
        authority_id: str,
        *,
        application_id: str,
        session_id: str,
        manifest: Mapping[str, object],
        destination_origin: str,
    ) -> SubmissionAuthority:
        """Non-consuming exact check used before PREPARED intent creation."""

        authority_id = self._binding_text(authority_id, "authority_id")
        application_id = self._binding_text(application_id, "application_id")
        session_id = self._binding_text(session_id, "session_id")
        self._assert_user_status_eligible(application_id)
        projected = authority_manifest_projection(manifest)
        if projected["application_id"] != application_id:
            raise SubmissionAuthorityError(
                "manifest application_id does not match authority binding"
            )
        origin = self._canonical_origin(destination_origin)
        if origin != _canonical_http_origin(
            projected["destination"], field="manifest.destination"
        ):
            raise SubmissionAuthorityError(
                "destination origin does not match the final manifest"
            )
        canonical = canonical_manifest_json(projected)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        authority = self.session.get(SubmissionAuthority, authority_id)
        now = self._aware(self._now())
        if (
            authority is None
            or authority.application_id != application_id
            or authority.session_id != session_id
            or authority.manifest_fingerprint != fingerprint
            or authority.manifest_json != canonical
            or authority.destination_origin != origin
            or authority.consumed_at is not None
            or self._aware(authority.expires_at) <= now
        ):
            raise SubmissionAuthorityError(
                "authority is invalid, expired, consumed, or mismatched"
            )
        return authority

    @staticmethod
    def _binding_text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SubmissionAuthorityError(f"{field} binding is required")
        return value.strip()

    @staticmethod
    def _canonical_origin(value: object) -> str:
        return _canonical_http_origin(value, field="destination_origin")

    @staticmethod
    def _assert_reissuance_matches(
        authority: SubmissionAuthority,
        *,
        fingerprint: str,
        canonical: str,
        origin: str,
        now: datetime,
    ) -> None:
        if (
            authority.manifest_fingerprint != fingerprint
            or authority.manifest_json != canonical
            or authority.destination_origin != origin
        ):
            raise SubmissionAuthorityError(
                "authority already exists for this application/session with different evidence"
            )
        if authority.consumed_at is not None:
            raise SubmissionAuthorityError("authority for this session was already consumed")
        if SubmissionAuthorityService._aware(authority.expires_at) <= now:
            raise SubmissionAuthorityError("authority for this session has expired")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
