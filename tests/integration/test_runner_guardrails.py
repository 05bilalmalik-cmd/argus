import socket
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from app.automation.adapters.generic import GenericAdapter
from app.automation.runner import (
    SubmissionBlocked,
    assert_field_fill_allowed,
    assert_navigation_allowed,
    assert_submission_allowed,
)
from app.config import Settings


def settings(tmp_path: Path, **values) -> Settings:
    env = {"ARGUS_DATA_DIR": str(tmp_path)}
    env.update(values)
    return Settings.load(env)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )


def test_submission_guard_allows_local_mock_only_at_risk_zero(tmp_path: Path) -> None:
    local = settings(tmp_path)

    assert_submission_allowed(local, "http://127.0.0.1:8787/lab/ats/standard", 0)
    with pytest.raises(SubmissionBlocked, match="built-in ATS laboratory"):
        assert_submission_allowed(local, "http://127.0.0.1:9999/lab/ats/standard", 0)
    with pytest.raises(SubmissionBlocked, match="built-in ATS laboratory"):
        assert_submission_allowed(local, "http://127.0.0.1:8787/api/profile", 0)
    with pytest.raises(SubmissionBlocked, match="risk level 0"):
        assert_submission_allowed(local, "http://127.0.0.1:8787/lab/ats/standard", 1)
    with pytest.raises(SubmissionBlocked, match="Live submission is disabled"):
        assert_submission_allowed(local, "https://jobs.example.com/apply", 0)



def test_field_fill_guard_requires_explicitly_trusted_destination(tmp_path: Path) -> None:
    local = settings(tmp_path)
    assert_field_fill_allowed(local, "http://127.0.0.1:8787/lab/ats/standard")
    with pytest.raises(SubmissionBlocked, match="trusted for field filling"):
        assert_field_fill_allowed(local, "http://127.0.0.1:9999/lab/ats/standard")
    with pytest.raises(SubmissionBlocked, match="trusted for field filling"):
        assert_field_fill_allowed(local, "https://jobs.example.com/apply")

    trusted = settings(
        tmp_path,
        ARGUS_LIVE_DOMAIN_ALLOWLIST="jobs.example.com",
    )
    assert_field_fill_allowed(trusted, "https://jobs.example.com/apply")


def test_review_navigation_allows_public_page_before_field_trust_is_known(tmp_path: Path) -> None:
    local = settings(tmp_path)

    assert_navigation_allowed(local, "https://jobs.example.com/apply")
    with pytest.raises(SubmissionBlocked, match="safe public"):
        assert_navigation_allowed(local, "http://192.168.1.10/apply")


def test_submission_guard_requires_explicit_allowlisted_domain(tmp_path: Path) -> None:
    configured = settings(
        tmp_path,
        ARGUS_ENABLE_LIVE_SUBMIT="true",
        ARGUS_LIVE_DOMAIN_ALLOWLIST="jobs.example.com",
    )

    assert_submission_allowed(configured, "https://jobs.example.com/apply", 0)
    with pytest.raises(SubmissionBlocked, match="not allowlisted"):
        assert_submission_allowed(configured, "https://evil.example/apply", 0)


def test_generic_adapter_inspects_semantic_labels_radio_options_and_files() -> None:
    html = """
    <form>
      <label for="first">First name</label><input id="first" name="first_name" required>
      <fieldset><legend>Will you require sponsorship?</legend>
        <label><input type="radio" name="sponsor" value="yes">Yes</label>
        <label><input type="radio" name="sponsor" value="no">No</label>
      </fieldset>
      <label for="cv">Upload CV</label><input id="cv" name="cv" type="file" required>
      <button type="submit">Submit application</button>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        fields = GenericAdapter().inspect(page)

        browser.close()

    assert [field.question.label for field in fields] == [
        "First name",
        "Will you require sponsorship?",
        "Upload CV",
    ]
    assert fields[1].question.options == ("Yes", "No")
    assert fields[2].question.field_type == "file"


def test_generic_adapter_surfaces_assessment_and_captcha_handoffs() -> None:
    html = """
    <form>
      <button type="button">Begin online assessment</button>
      <div class="g-recaptcha" data-sitekey="test"></div>
      <button type="submit">Submit application</button>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        fields = GenericAdapter().inspect(page)

        browser.close()

    assert [(field.question.label, field.question.field_type) for field in fields] == [
        ("Begin online assessment", "button"),
        ("Verify you are human", "captcha"),
    ]
