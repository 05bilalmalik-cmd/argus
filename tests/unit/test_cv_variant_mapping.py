"""Unit tests for the coupled programme-framing mapping.

Verifies the mapping introduced with the 2026-08-23 corpus restructure:
- year_in_industry / spring_week -> yii-cv (YINI degree, grad 2029)
- summer -> summer-cv (plain Finance, grad 2028)
- unknown groups -> no framing and therefore no CV selection
- blanket-tagged generic copies only win when no firm-specific doc matches
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from app.scouting import programmes
from app.services.applications import cv_variant_tag
from app.services.documents import DocumentService


class TestCvVariantTag:
    @pytest.mark.parametrize(
        ("group", "expected"),
        [
            ("summer", "summer-cv"),
            ("", None),
            ("other", None),
            (None, None),
            ("year_in_industry", "yii-cv"),
            ("spring_week", "yii-cv"),
            ("SUMMER", "summer-cv"),  # casefold applied
        ],
    )
    def test_group_to_variant(self, group, expected: str | None) -> None:
        assert cv_variant_tag(group) == expected


@pytest.mark.parametrize(
    ("group", "cv_variant_tag", "graduation_year", "degree_length_years"),
    [
        ("year_in_industry", "yii-cv", 2029, 4),
        ("spring_week", "yii-cv", 2029, 4),
        ("summer", "summer-cv", 2028, 3),
    ],
)
def test_programme_framing_keeps_cv_and_graduation_pair_coupled(
    group: str,
    cv_variant_tag: str,
    graduation_year: int,
    degree_length_years: int,
) -> None:
    resolver = getattr(programmes, "resolve_programme_framing", None)
    assert resolver is not None, "programme framing resolver is missing"
    framing = resolver(group)

    assert framing is not None
    assert framing.cv_variant_tag == cv_variant_tag
    assert framing.graduation_year == graduation_year
    assert framing.degree_length_years == degree_length_years


@pytest.mark.parametrize("group", [None, "", "OTHER", "invalid"])
def test_unknown_programme_has_no_framing(group: str | None) -> None:
    resolver = getattr(programmes, "resolve_programme_framing", None)
    assert resolver is not None, "programme framing resolver is missing"
    assert resolver(group) is None


def test_programme_framing_is_immutable() -> None:
    framing = programmes.resolve_programme_framing("summer")
    assert framing is not None

    with pytest.raises(FrozenInstanceError):
        framing.graduation_year = 2029


class StubDoc:
    def __init__(self, tags: list[str]) -> None:
        self.tags_json = json.dumps(tags)


class StubRepo:
    def __init__(self, docs: list[StubDoc]) -> None:
        self.docs = docs

    def approved_by_kind(self, kind: str) -> list[StubDoc]:
        return self.docs


def make_service(docs: list[StubDoc]) -> DocumentService:
    service = DocumentService.__new__(DocumentService)  # bypass __init__
    service.repository = StubRepo(docs)
    return service


class TestBlanketFallback:
    """Blanket copies are seeded WITHOUT firm/division tags and 'blanket' is
    never placed in desired_tags, so a specific document always outscores a
    blanket whenever a specific match exists; blanket wins only as fallback."""

    def test_specific_wins_when_present(self) -> None:
        svc = make_service(
            [
                StubDoc([DocumentService.BLANKET_TAG, "investment-banking", "yii-cv"]),
                StubDoc(["jp-morgan", "investment-banking", "yii-cv"]),
            ]
        )
        picked = svc.select_approved("cv", ("jp-morgan", "yii-cv", "investment-banking"))
        assert "jp-morgan" in picked.tags_json

    def test_blanket_used_as_fallback(self) -> None:
        svc = make_service([StubDoc([DocumentService.BLANKET_TAG, "investment-banking", "yii-cv"])])
        # The blanket shares the variant tag with desired_tags: that overlap
        # IS positive evidence, so it is a legitimate fallback.
        picked = svc.select_approved("cv", ("unknown-firm", "yii-cv"))
        assert DocumentService.BLANKET_TAG in picked.tags_json

    def test_zero_overlap_document_is_refused(self) -> None:
        svc = make_service(
            [StubDoc([DocumentService.BLANKET_TAG, "investment-banking", "summer-cv"])]
        )
        # No document carries any of the desired tags -> refuse rather than
        # upload an unrelated variant.
        assert svc.select_approved("cv", ("spring-week", "yii-cv")) is None
