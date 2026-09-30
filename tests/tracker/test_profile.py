from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tracker.profile import (
    DEFAULT_PROFILE,
    ProfileValidationError,
    load_profile,
    save_profile,
)


def test_default_finance_profile_is_non_identifying_and_has_fixed_years() -> None:
    profile = load_profile(Path("does-not-exist"))

    assert profile["graduation_years"] == {
        "summer": 2028,
        "year_in_industry": 2029,
        "spring_week": 2029,
    }
    assert profile["degree"] == "Finance"
    assert "UK" in profile["desired_locations"]
    assert set(profile) <= {
        "graduation_years",
        "degree",
        "desired_roles",
        "desired_locations",
    }
    assert profile == DEFAULT_PROFILE


def test_profile_round_trip_is_validated_and_does_not_add_identity_fields(tmp_path: Path) -> None:
    edited = {
        "graduation_years": {"summer": 2030, "year_in_industry": 2031, "spring_week": 2031},
        "degree": "Accounting and Finance",
        "desired_roles": ["summer", "spring_week"],
        "desired_locations": ["UK", "London"],
    }

    saved = save_profile(tmp_path, edited)
    loaded = load_profile(tmp_path)

    assert saved == edited
    assert loaded == edited
    on_disk = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert on_disk == edited
    assert "email" not in on_disk
    assert "name" not in on_disk


def test_profile_rejects_unknown_or_unsafe_values(tmp_path: Path) -> None:
    with pytest.raises(ProfileValidationError):
        save_profile(tmp_path, {**DEFAULT_PROFILE, "email": "person@example.test"})
    with pytest.raises(ProfileValidationError):
        save_profile(
            tmp_path,
            {
                **DEFAULT_PROFILE,
                "graduation_years": {
                    "summer": 1999,
                    "year_in_industry": 2029,
                    "spring_week": 2029,
                },
            },
        )
    with pytest.raises(ProfileValidationError):
        save_profile(tmp_path, {**DEFAULT_PROFILE, "degree": ""})


def test_malformed_profile_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "profile.json").write_text('{"degree": "Finance", "name": "secret"}', encoding="utf-8")

    with pytest.raises(ProfileValidationError):
        load_profile(tmp_path)
