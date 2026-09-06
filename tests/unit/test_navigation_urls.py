from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://jobs.example.test/role?utm_source=trackr&gh_src=abc&q=quant",
            "https://jobs.example.test/role?gh_src=abc&q=quant",
        ),
        (
            "https://jobs.lever.co/acme/123?trid=99&lever-source=trackr&iis=feed",
            "https://jobs.lever.co/acme/123?lever-source=trackr&iis=feed",
        ),
        (
            "https://acme.wd3.myworkdayjobs.com/job/London/Intern_R1?dcr_ci=old&jobId=R1",
            "https://acme.wd3.myworkdayjobs.com/job/London/Intern_R1?jobId=R1",
        ),
        (
            "https://jobs.example.test/role?utm_campaign=x&gh_src=a#apply",
            "https://jobs.example.test/role?gh_src=a#apply",
        ),
    ],
)
def test_trackr_cleanup_removes_only_known_tracking_pairs(raw: str, expected: str) -> None:
    """Deleting the first tracking pair must not delete the query delimiter."""

    from app.scouting.trackr_live import _clean_url

    assert _clean_url(raw) == expected


def test_navigation_validation_preserves_functional_query_and_fragment() -> None:
    """Navigation validation must never substitute a lossy deduplication key."""

    from app.domain.targets import validate_navigation_url

    raw = (
        "HTTPS://Jobs.Example.Test/apply?gh_src=GH&lever-source=LV&q=credit"
        "&iis=trackr&jobId=ABC#questions"
    )
    assert validate_navigation_url(raw) == (
        "https://jobs.example.test/apply?gh_src=GH&lever-source=LV&q=credit"
        "&iis=trackr&jobId=ABC#questions"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "file:///C:/candidate.txt",
        "https://user:secret@example.test/apply",
        "https://example.test/apply\nhttps://attacker.test/",
        "trackr-pending://Firm/Role",
    ],
)
def test_navigation_validation_rejects_non_http_or_ambiguous_urls(raw: str) -> None:
    from app.domain.targets import validate_navigation_url

    with pytest.raises(ValueError, match="navigation URL"):
        validate_navigation_url(raw)


def test_canonical_key_is_not_used_as_navigation_url() -> None:
    from app.domain.targets import canonical_url_key, validate_navigation_url

    raw = "HTTPS://Jobs.Example.Test/apply/?utm_source=x&jobId=ABC#questions"
    assert canonical_url_key(raw) == "https://jobs.example.test/apply?jobId=ABC"
    assert validate_navigation_url(raw) == (
        "https://jobs.example.test/apply/?utm_source=x&jobId=ABC#questions"
    )


def test_source_fingerprint_distinguishes_location_and_division() -> None:
    from app.domain.targets import source_fingerprint

    shared = {
        "employer": "Acme Capital",
        "role_title": "Summer Analyst",
        "cycle": "2027",
        "source_url": "https://careers.example.test/students",
    }
    london_credit = source_fingerprint(
        **shared, location="London", division="Private Credit"
    )
    paris_credit = source_fingerprint(
        **shared, location="Paris", division="Private Credit"
    )
    london_equity = source_fingerprint(
        **shared, location="London", division="Private Equity"
    )

    assert len({london_credit, paris_credit, london_equity}) == 3
