"""A refused start must tell the candidate what stopped it and what to do.

Most refusal texts already name the specific cause -- which status excluded the
row, which target state it actually has -- and that specificity is the useful
part, so it is preserved and a next step is appended.  Only maintainer-only
wording is replaced outright.  The exact technical text always remains in
``message`` so a refusal stays diagnosable.
"""
from __future__ import annotations

import json

import pytest

from app.routers.api import (
    _PREFILL_WALL_NEXT_STEP,
    _PREFILL_WALL_REPLACE,
    _prefill_error,
)

APPLICATION_ID = "11111111-2222-3333-4444-555555555555"
ENVELOPE_TEXT = (
    "Serialized target-resolution envelope disagrees with authoritative "
    "target columns"
)


def _payload(code: str, wall: str) -> dict:
    return json.loads(_prefill_error(APPLICATION_ID, code, wall).body)


def test_maintainer_only_text_is_not_what_the_candidate_reads() -> None:
    body = _payload("target_evidence_invalid", ENVELOPE_TEXT)
    assert "envelope" not in body["wall"]
    assert "columns" not in body["wall"]
    assert body["message"] == ENVELOPE_TEXT  # still diagnosable


@pytest.mark.parametrize(
    ("code", "detail", "keyword"),
    [
        (
            "user_status_excluded",
            "User status NOT_INTERESTED excludes this opportunity",
            "NOT_INTERESTED",
        ),
        (
            "target_not_application_form",
            "the actual target is JOB_DETAIL.",
            "JOB_DETAIL",
        ),
    ],
)
def test_the_specific_cause_survives(code: str, detail: str, keyword: str) -> None:
    """The named status/target is the actionable part and must not be lost."""

    wall = _payload(code, detail)["wall"]
    assert keyword in wall
    assert detail.rstrip(".") in wall
    assert _PREFILL_WALL_NEXT_STEP[code] in wall


def test_a_next_step_is_added_without_doubling_punctuation() -> None:
    wall = _payload("prefill_session_active", "A live session already exists.")["wall"]
    assert wall == "A live session already exists. Use Continue rather than starting a second run."
    assert ".." not in wall


def test_refusal_never_claims_anything_was_sent() -> None:
    body = _payload("target_evidence_invalid", ENVELOPE_TEXT)
    assert body["submitted"] is False
    assert body["click_boundary_crossed"] is False


@pytest.mark.parametrize("code", sorted(_PREFILL_WALL_REPLACE))
def test_replacement_copy_reads_as_a_sentence(code: str) -> None:
    wall = _payload(code, ENVELOPE_TEXT)["wall"]
    assert wall == _PREFILL_WALL_REPLACE[code]
    assert wall[0].isupper() and wall.endswith(".")
    assert len(wall) < 400
    for banned in ("_", "None", "null", "Exception", "Traceback"):
        assert banned not in wall


def test_an_unmapped_code_still_surfaces_its_original_text() -> None:
    """An unknown refusal must not become blank or generic."""

    body = _payload("some_unmapped_future_code", "a specific new reason")
    assert body["wall"] == "a specific new reason"
    assert body["message"] == "a specific new reason"


def test_an_empty_detail_still_yields_a_next_step() -> None:
    wall = _payload("user_status_excluded", "")["wall"]
    assert wall == _PREFILL_WALL_NEXT_STEP["user_status_excluded"]


def test_status_and_code_are_preserved_for_the_caller() -> None:
    response = _prefill_error(APPLICATION_ID, "user_status_excluded", "detail")
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["code"] == "user_status_excluded"
    assert body["application_id"] == APPLICATION_ID
