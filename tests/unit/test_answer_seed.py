"""Unit tests for the Risk-0 answer-bank seed planner.

``plan_answers`` is pure: given the canonical keys already in the answer
bank and the candidate profile facts, it returns the AnswerSpec rows that
should be inserted. These tests pin down coverage of the high-frequency
unmapped labels, idempotency, and the sensitivity guardrails.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.answers import _STOPWORDS  # noqa: E402
from scripts.seed_answers import AnswerSpec, ProfileFacts, plan_answers  # noqa: E402

PROFILE = ProfileFacts(
    university="Example University",
    degree="Bachelor of Science, Finance",
    graduation_year=2028,
    linkedin_url="https://www.linkedin.com/in/demo-candidate",
    preferred_locations=("London",),
)


def _tokens(text: str) -> set[str]:
    """Mirror of AnswerService._fuzzy_resolve tokenisation."""
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9']+", text.casefold())
        if token not in _STOPWORDS and len(token) > 2
    }


def _fuzzy_overlap(label: str, prompt: str) -> float:
    wanted = _tokens(label)
    if len(wanted) < 2:
        return 0.0  # fuzzy resolver refuses single-token labels outright
    stored = _tokens(prompt)
    return len(wanted & stored) / len(wanted)


def test_full_plan_covers_high_frequency_labels() -> None:
    plan = plan_answers(set(), PROFILE)
    keys = {spec.canonical_key for spec in plan}
    assert {
        "contact.linkedin",
        "education.university",
        "answer.discipline",
        "answer.subjects",
        "answer.work_location",
        "answer.desired_office",
        "answer.source",
        "answer.end_month",
        "answer.end_year",
    } <= keys


def test_plan_values_derive_from_profile() -> None:
    plan = {spec.canonical_key: spec for spec in plan_answers(set(), PROFILE)}
    assert plan["contact.linkedin"].answer == (
        "https://www.linkedin.com/in/demo-candidate"
    )
    assert plan["answer.discipline"].answer == "Finance"
    assert plan["answer.work_location"].answer == "London"
    assert plan["answer.desired_office"].answer == "London"
    assert plan["answer.end_year"].answer == "2028"
    assert plan["answer.end_month"].answer == "September"
    assert plan["answer.source"].answer == "Company careers website"


def test_city_level_location_preferred_over_country() -> None:
    profile = dataclasses.replace(
        PROFILE, preferred_locations=("United Kingdom", "London")
    )
    plan = {s.canonical_key: s for s in plan_answers(set(), profile)}
    assert plan["answer.work_location"].answer == "London"

    country_only = dataclasses.replace(PROFILE, preferred_locations=("Wales",))
    plan = {s.canonical_key: s for s in plan_answers(set(), country_only)}
    assert plan["answer.work_location"].answer == "Wales"


def test_every_spec_is_approved_and_non_sensitive() -> None:
    for spec in plan_answers(set(), PROFILE):
        assert isinstance(spec, AnswerSpec)
        if spec.canonical_key == "account.password":
            # deliberate exception: user-supplied secret, unapproved placeholder
            assert spec.approved is False and spec.sensitive is True
            continue
        assert spec.approved is True
        assert spec.sensitive is False


def test_sensitive_categories_never_planned() -> None:
    forbidden_prefixes = ("sensitive.",)
    forbidden_keys = {
        "legal.work_authorisation",
        "legal.sponsorship",
        "legal.attestation",
        "handoff.captcha",
        "handoff.assessment",
    }
    for spec in plan_answers(set(), PROFILE):
        assert not spec.canonical_key.startswith(forbidden_prefixes)
        assert spec.canonical_key not in forbidden_keys


def test_demographic_style_profile_fields_are_ignored() -> None:
    # Even a profile carrying demographic-looking data must not leak into
    # the plan: the planner only reads whitelisted ProfileFacts fields.
    assert ProfileFacts.__dataclass_fields__.keys() == {
        "university",
        "degree",
        "graduation_year",
        "linkedin_url",
        "preferred_locations",
    }
    assert all(not spec.sensitive or spec.canonical_key == "account.password" for spec in plan_answers(set(), PROFILE))
    # the password placeholder must stay unapproved until the user fills it
    pw = next(spec for spec in plan_answers(set(), PROFILE) if spec.canonical_key == "account.password")
    assert pw.approved is False and pw.answer == ""


def test_idempotency_existing_keys_skipped() -> None:
    full_plan = plan_answers(set(), PROFILE)
    existing = {spec.canonical_key for spec in full_plan}
    assert plan_answers(existing, PROFILE) == []
    partial = {"contact.linkedin"}
    remaining = plan_answers(partial, PROFILE)
    assert "contact.linkedin" not in {s.canonical_key for s in remaining}
    assert len(remaining) == len(full_plan) - 1


def test_empty_profile_yields_minimal_safe_plan() -> None:
    plan = plan_answers(set(), ProfileFacts())
    # Generic referral-source answer + the unapproved password placeholder
    # are safe without profile data.
    keys = [spec.canonical_key for spec in plan]
    assert "answer.source" in keys
    assert "account.password" in keys
    pw = next(spec for spec in plan if spec.canonical_key == "account.password")
    assert pw.approved is False  # never auto-approved


def test_prompts_fuzzy_match_real_form_labels() -> None:
    """Stored prompts must resolve against the live form labels via the
    AnswerService fuzzy matcher (>= 0.5 token overlap)."""
    targets = {
        "Undergrad Discipline(s) *": "Undergrad Discipline(s)",
        "What is your preferred work location? *": (
            "What is your preferred work location?"
        ),
        "What is your desired office? *": "What is your desired office?",
        "How did you hear about CTC? *": "How did you hear about us?",
        "How did you hear about this job? *": "How did you hear about us?",
        "End date year*": "End date year",
        "End date month*": "End date month",
        "LinkedIn Profile": "LinkedIn Profile",
    }
    plan = {spec.canonical_key: spec for spec in plan_answers(set(), PROFILE)}
    key_for_prompt = {spec.prompt: spec.canonical_key for spec in plan.values()}
    for label, prompt in targets.items():
        spec = plan[key_for_prompt[prompt]]
        assert _fuzzy_overlap(label, spec.prompt) >= 0.5, (
            f"{spec.canonical_key} prompt {spec.prompt!r} does not match "
            f"form label {label!r}"
        )


def test_discipline_derivation() -> None:
    from scripts.seed_answers import primary_discipline

    assert primary_discipline("Bachelor of Science, Finance") == "Finance"
    assert primary_discipline("BSc Economics") == "BSc Economics"
    assert primary_discipline("") == ""
