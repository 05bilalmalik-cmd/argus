"""Risk-0 answer-bank expansion: plan + seed answers for high-frequency
unmapped form labels so applications stop pausing in NEEDS_USER.

SERVER AWARENESS: this script WRITES to the live SQLite database through the
application's own service layer (``AnswerService.upsert`` - never raw SQL).
Stop the ARGUS server (or run this via the API) before using ``--apply``
to avoid SQLite writer contention with the running autopilot/scheduler.

Usage (from the ARGUS repo root):
    ./.venv/Scripts/python.exe scripts/seed_answers.py            # dry run (default)
    ./.venv/Scripts/python.exe scripts/seed_answers.py --apply    # real writes

Safety rules enforced here:
  * Idempotent: canonical keys that already exist in the answer bank are
    never touched (pass ``--refresh`` to update values instead of skipping).
  * Sensitivity respected: nothing demographic/protected/legal is ever
    planned or written. Gender, ethnicity, disability, date of birth,
    nationality, free-school-meals style questions and work-authorisation /
    sponsorship wording stay untouched (they remain human handoffs).
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Never auto-answer these canonical-key prefixes / exact keys.
_SENSITIVE_PREFIXES = ("sensitive.",)
_SENSITIVE_KEYS = frozenset(
    {
        "legal.work_authorisation",
        "legal.sponsorship",
        "legal.attestation",
        "handoff.captcha",
        "handoff.assessment",
    }
)


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    """The subset of the candidate profile the planner is allowed to see."""

    university: str = ""
    degree: str = ""
    graduation_year: int | None = None
    linkedin_url: str = ""
    preferred_locations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnswerSpec:
    """A planned ``AnswerEntry`` row (value stored encrypted at write time)."""

    canonical_key: str
    prompt: str
    answer: str
    evidence: str = ""
    category: str = "risk0_seed"
    approved: bool = True
    sensitive: bool = False


def primary_discipline(degree: str) -> str:
    """Derive the undergrad discipline from a degree string such as
    ``"Bachelor of Science, Finance"`` -> ``"Finance"``."""
    parts = [part.strip() for part in (degree or "").split(",") if part.strip()]
    return parts[-1] if parts else ""


def plan_answers(
    existing_keys: set[str] | frozenset[str], profile: ProfileFacts
) -> list[AnswerSpec]:
    """Pure, testable seed planner.

    Returns the ``AnswerSpec`` rows that SHOULD be inserted given the
    canonical keys already present in the answer bank and the candidate
    profile facts. Deterministic, no I/O; skipping existing keys makes the
    resulting write step idempotent.
    """

    specs: list[AnswerSpec] = []

    def add(spec: AnswerSpec, *, allow_empty: bool = False) -> None:
        if spec.canonical_key.startswith(_SENSITIVE_PREFIXES):
            raise ValueError(f"refusing sensitive key: {spec.canonical_key}")
        if spec.canonical_key in _SENSITIVE_KEYS:
            raise ValueError(f"refusing protected key: {spec.canonical_key}")
        if spec.canonical_key in existing_keys:
            return
        if not allow_empty and not spec.answer.strip():
            raise ValueError(f"empty answer for {spec.canonical_key}")
        specs.append(spec)

    # LinkedIn: only when the profile actually carries a URL.
    linkedin = (profile.linkedin_url or "").strip()
    if linkedin:
        add(
            AnswerSpec(
                canonical_key="contact.linkedin",
                prompt="LinkedIn Profile",
                answer=linkedin,
                evidence="candidate_profile.linkedin_url",
            )
        )

    # School/university name (forms asking bare "School*" for the institution).
    university = (profile.university or "").strip()
    if university:
        add(
            AnswerSpec(
                canonical_key="education.university",
                prompt="University / School attended",
                answer=university,
                evidence="candidate_profile.university",
            )
        )

    # Undergrad discipline / degree subject checkboxes, derived from the degree.
    discipline = primary_discipline(profile.degree)
    if discipline:
        add(
            AnswerSpec(
                canonical_key="answer.discipline",
                prompt="Undergrad Discipline(s)",
                answer=discipline,
                evidence=f"derived from degree '{profile.degree}'",
            )
        )
        add(
            AnswerSpec(
                canonical_key="answer.subjects",
                prompt="Subjects(s) aligned with education background",
                answer=discipline,
                evidence=f"derived from degree '{profile.degree}'",
            )
        )

    # Preferred work location / desired office -> most specific preferred
    # location (a city such as "London" beats a country like "United Kingdom").
    countries = {"uk", "united kingdom", "england", "great britain"}
    location = next(
        (
            loc
            for loc in profile.preferred_locations
            if loc.strip().casefold() not in countries
        ),
        next(iter(profile.preferred_locations), ""),
    )
    if location:
        add(
            AnswerSpec(
                canonical_key="answer.work_location",
                prompt="What is your preferred work location?",
                answer=location,
                evidence="candidate_profile.preferred_locations",
            )
        )
        add(
            AnswerSpec(
                canonical_key="answer.desired_office",
                prompt="What is your desired office?",
                answer=location,
                evidence="candidate_profile.preferred_locations",
            )
        )

    # Referral-source marketing question: safe generic answer.
    add(
        AnswerSpec(
            canonical_key="answer.source",
            prompt="How did you hear about us?",
            answer="Company careers website",
            evidence="generic marketing response",
        )
    )

    # Education end dates derived from the graduation year.
    if profile.graduation_year:
        add(
            AnswerSpec(
                canonical_key="answer.end_month",
                prompt="End date month",
                answer="September",
                evidence="UK academic cycle; graduation year "
                f"{profile.graduation_year}",
            )
        )
        add(
            AnswerSpec(
                canonical_key="answer.end_year",
                prompt="End date year",
                answer=str(profile.graduation_year),
                evidence="candidate_profile.graduation_year",
            )
        )

    # Workday/ATS account-creation password. NOT auto-generated here: the
    # seed only registers the key with a placeholder that stays unapproved.
    # The user sets the real value via the API/UI, then approves it.
    add(
        AnswerSpec(
            canonical_key="account.password",
            prompt="ATS account password (Workday 'Create Account' step)",
            answer="",
            evidence="user-provided; never generated",
            sensitive=True,
            approved=False,
        ),
        allow_empty=True,
    )

    return specs


# Labels that look factual/protected -> never auto-answer, always human review.
_HUMAN_REVIEW_PATTERN = (
    r"pronouns|nationality|ethnic|gender|cultural background|age group|"
    r"age range|visa|eligibility to work|employment eligibility|type of "
    r"school|school meals|military|family member|security licens|finra"
)

# Label heuristics shared with the seed script: normalised label -> key.
_LABEL_PROPOSALS: tuple[tuple[str, str], ...] = (
    (r"mathematics competitions", "(human review: factual claim)"),
    (_HUMAN_REVIEW_PATTERN, "(human review: protected/factual)"),
    (r"linkedin", "contact.linkedin"),
    (r"hear about|referral source|how did you find", "answer.source"),
    (r"undergrad|discipline|field of study|degree subject", "answer.discipline"),
    (r"subjects?\s*\(s\)|fields? of study", "answer.subjects"),
    (r"preferred work location", "answer.work_location"),
    (
        r"desired office|which office|office applying|^\s*office\s*$",
        "answer.desired_office",
    ),
    (r"end date.*\byear\b|completion year", "answer.end_year"),
    (r"end date.*month", "answer.end_month"),
    (r"^\s*school\s*$|school name|university|college attended|institution",
     "education.university"),
)

_UI_FIELD_TYPES = frozenset({"search", "password", "file"})


def propose_key(label: str, field_type: str) -> str:
    """Heuristic audit proposal for an unmapped label."""
    if field_type.casefold() in _UI_FIELD_TYPES:
        return "(site UI, not a question)"
    folded = re.sub(r"[()*]", " ", label).casefold().strip()
    for pattern, key in _LABEL_PROPOSALS:
        if re.search(pattern, folded):
            return key
    return "(no deterministic proposal)"


def load_profile_facts(session) -> ProfileFacts:  # pragma: no cover - thin adapter
    """Read the candidate profile row via the model layer."""
    import json

    from sqlalchemy import select

    from app.models import CandidateProfile

    profile = session.scalars(select(CandidateProfile)).first()
    if profile is None:
        return ProfileFacts()
    try:
        locations = tuple(json.loads(profile.preferred_locations_json or "[]"))
    except json.JSONDecodeError:
        locations = ()
    return ProfileFacts(
        university=profile.university or "",
        degree=profile.degree or "",
        graduation_year=profile.graduation_year,
        linkedin_url=profile.linkedin_url or "",
        preferred_locations=tuple(str(item) for item in locations),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write (default: dry run). Stop the ARGUS server first.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="update existing keys instead of skipping them",
    )
    args = parser.parse_args(argv)

    from sqlalchemy import select

    from app.cli import _runtime
    from app.models import AnswerEntry
    from app.services.answers import AnswerService

    settings, database, crypto = _runtime()

    with database.session_scope() as session:
        existing = set(
            session.scalars(select(AnswerEntry.canonical_key)).all()
        )
        profile = load_profile_facts(session)
        keys_for_plan = set() if args.refresh else existing
        plan = plan_answers(keys_for_plan, profile)

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"[{mode}] profile: uni={profile.university!r} "
              f"degree={profile.degree!r} grad={profile.graduation_year} "
              f"locations={profile.preferred_locations}")
        print(f"[{mode}] answer bank holds {len(existing)} keys; "
              f"{len(plan)} new answer(s) planned:\n")
        for spec in plan:
            print(f"  {spec.canonical_key:<24} approved={spec.approved} "
                  f"sensitive={spec.sensitive} value={spec.answer!r}")
            print(f"{'':<26}prompt: {spec.prompt}")

        if args.apply and plan:
            service = AnswerService(session, crypto)
            for spec in plan:
                service.upsert(
                    canonical_key=spec.canonical_key,
                    prompt=spec.prompt,
                    answer=spec.answer,
                    approved=spec.approved,
                    sensitive=spec.sensitive,
                    category=spec.category,
                    evidence=spec.evidence,
                    actor="risk0_seed_script",
                )
            session.commit()
            print(f"\n[APPLY] wrote {len(plan)} answer entries.")
        elif args.apply:
            print("\n[APPLY] nothing to do - all planned keys already exist.")
        else:
            print("\n[dry run] re-run with --apply (server stopped) to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
