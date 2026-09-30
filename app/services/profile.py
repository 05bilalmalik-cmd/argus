from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy.orm import Session

from app.domain.questions import CanonicalKey
from app.domain.eligibility import CandidateSnapshot
from app.models import CandidateProfile
from app.repositories import CandidateRepository
from app.scouting.programmes import ProgrammeFraming
from app.security.audit import AuditInput, append_audit
from app.security.crypto import CryptoBox


def _parse_admissible_years(json_text: str | None) -> tuple[int, ...]:
    """Parse admissible_graduation_years_json defensively.

    Requires a JSON list of integers in range 1950-2100.
    On any malformed input (invalid JSON, not a list, non-int items, out-of-range),
    returns empty tuple to fail closed - caller falls back to legacy single-year behavior.
    """
    if not json_text:
        return ()
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    years: list[int] = []
    for item in parsed:
        if not isinstance(item, int):
            return ()
        if not (1950 <= item <= 2100):
            return ()
        years.append(item)
    return tuple(years)


@dataclass(frozen=True, slots=True)
class ProfileUpdate:
    first_name: str | None = None
    last_name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    city: str | None = None
    postcode: str | None = None
    country: str | None = None
    linkedin_url: str | None = None
    university: str | None = None
    degree: str | None = None
    graduation_year: int | None = None
    current_study_year: str | None = None
    preferred_locations: tuple[str, ...] | None = None
    admissible_graduation_years: tuple[int, ...] | None = None
    work_authorisation: str | None = None
    requires_sponsorship: bool | None = None
    work_authorisation_approved: bool | None = None


class ProfileService:
    _PLAIN_FIELDS = {
        "first_name",
        "last_name",
        "preferred_name",
        "email",
        "phone",
        "address_line1",
        "city",
        "postcode",
        "country",
        "linkedin_url",
        "university",
        "degree",
        "graduation_year",
        "current_study_year",
    }

    def __init__(self, session: Session, crypto: CryptoBox):
        self.session = session
        self.crypto = crypto
        self.repository = CandidateRepository(session)

    def get_model(self) -> CandidateProfile:
        return self.repository.get_or_create()

    def update(self, update: ProfileUpdate, *, actor: str = "user") -> CandidateProfile:
        profile = self.get_model()
        changed: list[str] = []
        for field in fields(update):
            name = field.name
            value = getattr(update, name)
            if value is None:
                continue
            if name in self._PLAIN_FIELDS:
                setattr(profile, name, value)
                changed.append(name)
            elif name == "preferred_locations":
                profile.preferred_locations_json = json.dumps(list(value))
                changed.append(name)
            elif name == "admissible_graduation_years":
                profile.admissible_graduation_years_json = json.dumps(list(value))
                changed.append(name)
            elif name == "work_authorisation":
                profile.work_authorisation_ciphertext = self.crypto.encrypt(value)
                changed.append(name)
            elif name == "requires_sponsorship":
                profile.sponsorship_required_ciphertext = self.crypto.encrypt(
                    "true" if value else "false"
                )
                changed.append(name)
            elif name == "work_authorisation_approved":
                profile.work_authorisation_approved = value
                changed.append(name)
        self.session.flush()
        append_audit(
            self.session,
            AuditInput(
                actor,
                "profile.updated",
                "profile",
                str(profile.id),
                {"changed_fields": sorted(changed)},
            ),
        )
        return profile

    def _requires_sponsorship(self, profile: CandidateProfile) -> bool | None:
        if not profile.sponsorship_required_ciphertext:
            return None
        return self.crypto.decrypt(profile.sponsorship_required_ciphertext) == "true"

    def get_snapshot(self) -> CandidateSnapshot:
        profile = self.get_model()
        preferred = tuple(json.loads(profile.preferred_locations_json or "[]"))
        sponsorship = (
            self._requires_sponsorship(profile)
            if profile.work_authorisation_approved
            else None
        )
        return CandidateSnapshot(
            expected_graduation_year=profile.graduation_year,
            requires_sponsorship=sponsorship,
            work_authorisation_approved=profile.work_authorisation_approved,
            preferred_locations=preferred,
        )

    def get_automation_data(
        self, framing: ProgrammeFraming | None = None
    ) -> dict[str, Any]:
        profile = self.get_model()
        values: dict[str, Any] = {
            "identity.first_name": profile.first_name,
            "identity.last_name": profile.last_name,
            "identity.full_name": " ".join(
                part for part in (profile.first_name, profile.last_name) if part
            ),
            "contact.email": profile.email,
            "contact.phone": profile.phone,
            "contact.address_line_1": profile.address_line1,
            "contact.city": profile.city,
            "contact.postcode": profile.postcode,
            "contact.country": profile.country,
            "contact.linkedin": profile.linkedin_url,
            "education.university": profile.university,
            "education.degree": profile.degree,
            "education.current_study_year": profile.current_study_year,
        }
        admissible_years = _parse_admissible_years(profile.admissible_graduation_years_json)
        if framing is None:
            values[CanonicalKey.GRADUATION_YEAR.value] = profile.graduation_year
            values[CanonicalKey.EDUCATION_END_YEAR.value] = profile.graduation_year
        elif admissible_years:
            if framing.graduation_year in admissible_years:
                values[CanonicalKey.GRADUATION_YEAR.value] = framing.graduation_year
                values[CanonicalKey.EDUCATION_END_YEAR.value] = framing.graduation_year
            else:
                values[CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value] = True
                values["guard.programme_graduation_stored"] = profile.graduation_year
                values["guard.programme_graduation_tier"] = framing.graduation_year
                values["guard.programme_admissible_years"] = admissible_years
        elif profile.graduation_year is not None:
            if profile.graduation_year == framing.graduation_year:
                values[CanonicalKey.GRADUATION_YEAR.value] = profile.graduation_year
                values[CanonicalKey.EDUCATION_END_YEAR.value] = profile.graduation_year
            else:
                values[CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value] = True
                values["guard.programme_graduation_stored"] = profile.graduation_year
                values["guard.programme_graduation_tier"] = framing.graduation_year
        cleaned = {key: value for key, value in values.items() if value not in (None, "")}
        if profile.work_authorisation_approved:
            if profile.work_authorisation_ciphertext:
                cleaned["legal.work_authorisation"] = self.crypto.decrypt(
                    profile.work_authorisation_ciphertext
                )
            sponsorship = self._requires_sponsorship(profile)
            if sponsorship is not None:
                cleaned["legal.sponsorship"] = sponsorship
        return cleaned

    def public_dict(self) -> dict[str, Any]:
        profile = self.get_model()
        data = {
            "id": profile.id,
            "first_name": profile.first_name,
            "last_name": profile.last_name,
            "preferred_name": profile.preferred_name,
            "email": profile.email,
            "phone": profile.phone,
            "address_line1": profile.address_line1,
            "city": profile.city,
            "postcode": profile.postcode,
            "country": profile.country,
            "linkedin_url": profile.linkedin_url,
            "university": profile.university,
            "degree": profile.degree,
            "graduation_year": profile.graduation_year,
            "current_study_year": profile.current_study_year,
            "preferred_locations": json.loads(profile.preferred_locations_json or "[]"),
            "admissible_graduation_years": list(_parse_admissible_years(profile.admissible_graduation_years_json)),
            "work_authorisation_configured": bool(profile.work_authorisation_ciphertext),
            "requires_sponsorship": (
                self._requires_sponsorship(profile)
                if profile.work_authorisation_approved
                else None
            ),
            "work_authorisation_approved": profile.work_authorisation_approved,
        }
        return data
