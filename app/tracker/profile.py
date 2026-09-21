"""Validated, non-identifying profile storage for the tracker.

This profile is deliberately separate from ARGUS's candidate/application profile.  It
contains only matching preferences needed by the public internship tracker.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


class ProfileValidationError(ValueError):
    """A tracker profile is malformed or contains an unsupported field."""


DEFAULT_PROFILE: dict[str, object] = {
    "graduation_years": {
        "summer": 2028,
        "year_in_industry": 2029,
        "spring_week": 2029,
    },
    "degree": "Finance",
    "desired_roles": ["summer", "year_in_industry", "spring_week"],
    "desired_locations": ["UK"],
}

_ALLOWED_FIELDS = frozenset(DEFAULT_PROFILE)
_REQUIRED_PROGRAMMES = frozenset(DEFAULT_PROFILE["graduation_years"])
_PROFILE_FILENAME = "profile.json"


def _profile_path(data_dir: str | Path) -> Path:
    root = Path(data_dir)
    if root.name == _PROFILE_FILENAME:
        raise ProfileValidationError("data_dir must be a directory, not profile.json")
    return root / _PROFILE_FILENAME


def _text_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ProfileValidationError(f"{field} must be a non-empty list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ProfileValidationError(f"{field} must contain non-empty text")
        if len(item) > 120:
            raise ProfileValidationError(f"{field} values are too long")
        result.append(item.strip())
    return result


def validate_profile(profile: Mapping[str, object]) -> dict[str, object]:
    """Return a defensive, JSON-safe profile or raise before it is persisted."""

    if not isinstance(profile, Mapping):
        raise ProfileValidationError("profile must be an object")
    keys = set(profile)
    unknown = keys - _ALLOWED_FIELDS
    if unknown:
        raise ProfileValidationError(f"unsupported profile field: {sorted(unknown)[0]}")
    missing = _ALLOWED_FIELDS - keys
    if missing:
        raise ProfileValidationError(f"missing profile field: {sorted(missing)[0]}")

    years_raw = profile["graduation_years"]
    if not isinstance(years_raw, Mapping) or set(years_raw) != _REQUIRED_PROGRAMMES:
        raise ProfileValidationError(
            "graduation_years must contain summer, year_in_industry and spring_week"
        )
    years: dict[str, int] = {}
    for programme in ("summer", "year_in_industry", "spring_week"):
        year = years_raw[programme]
        if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
            raise ProfileValidationError(f"graduation_years.{programme} must be between 2000 and 2100")
        years[programme] = year

    degree = profile["degree"]
    if not isinstance(degree, str) or not degree.strip() or "\x00" in degree:
        raise ProfileValidationError("degree must be non-empty text")
    if len(degree.strip()) > 160:
        raise ProfileValidationError("degree is too long")

    return {
        "graduation_years": years,
        "degree": degree.strip(),
        "desired_roles": _text_list(profile["desired_roles"], "desired_roles"),
        "desired_locations": _text_list(profile["desired_locations"], "desired_locations"),
    }


def load_profile(data_dir: str | Path) -> dict[str, object]:
    """Load ``data_dir/profile.json`` or return the safe finance defaults."""

    path = _profile_path(data_dir)
    if not path.exists():
        return copy.deepcopy(DEFAULT_PROFILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileValidationError(f"could not read profile.json: {type(exc).__name__}") from exc
    return validate_profile(raw)


def save_profile(data_dir: str | Path, profile: Mapping[str, object]) -> dict[str, object]:
    """Validate and atomically save the tracker-only profile."""

    validated = validate_profile(profile)
    path = _profile_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return copy.deepcopy(validated)


__all__ = [
    "DEFAULT_PROFILE",
    "ProfileValidationError",
    "load_profile",
    "save_profile",
    "validate_profile",
]
