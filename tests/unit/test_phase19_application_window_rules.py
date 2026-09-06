from __future__ import annotations

from datetime import date

import pytest

from app.scouting.application_window import (
    ApplicationWindowStatus,
    derive_application_window,
    tracker_owned_host,
)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"explicit_status": "open"}, ApplicationWindowStatus.OPEN),
        ({"explicit_status": "coming soon"}, ApplicationWindowStatus.NOT_YET_OPEN),
        ({"explicit_status": "closed"}, ApplicationWindowStatus.CLOSED),
        (
            {
                "explicit_status": "unrecognised vendor state",
                "application_url": "https://jobs.example.com/role",
            },
            ApplicationWindowStatus.UNKNOWN,
        ),
        (
            {
                "opening_date": date(2026, 8, 28),
                "application_url": "https://jobs.example.com/role",
                "today": date(2026, 8, 27),
            },
            ApplicationWindowStatus.NOT_YET_OPEN,
        ),
        (
            {
                "closing_date": date(2026, 8, 26),
                "application_url": "https://jobs.example.com/role",
                "today": date(2026, 8, 27),
            },
            ApplicationWindowStatus.CLOSED,
        ),
        (
            {
                "opening_date": date(2026, 8, 29),
                "closing_date": date(2026, 8, 28),
                "application_url": "https://jobs.example.com/role",
                "today": date(2026, 8, 27),
            },
            ApplicationWindowStatus.UNKNOWN,
        ),
        (
            {"application_url": "https://jobs.example.com/role"},
            ApplicationWindowStatus.OPEN,
        ),
        (
            {"application_url": None, "link_signal_known": True},
            ApplicationWindowStatus.NOT_YET_OPEN,
        ),
        ({}, ApplicationWindowStatus.UNKNOWN),
    ],
)
def test_application_window_evidence_priority(kwargs, expected) -> None:
    assert derive_application_window(**kwargs) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://app.the-trackr.com/uk-finance/spring-weeks",
        "https://api.the-trackr.com/programmes",
        "https://the-trackr.com/file.pdf",
        "https://academy.the-trackr.com/course",
    ],
)
def test_every_trackr_owned_host_is_recognised(url: str) -> None:
    assert tracker_owned_host(url) is True


def test_trackr_attribution_on_an_employer_host_is_not_a_trackr_owned_host() -> None:
    assert (
        tracker_owned_host(
            "https://job-boards.greenhouse.io/acme/jobs/123?gh_src=Trackr"
        )
        is False
    )
