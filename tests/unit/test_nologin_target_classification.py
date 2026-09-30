from __future__ import annotations

import pytest

from app.domain.targets import TargetKind


def test_password_input_markup_is_auth_wall() -> None:
    from app.automation.targets import classify_target

    result = classify_target(
        "https://careers.example.test/role",
        "https://careers.example.test/apply",
        html='<form id="login"><input type="password" name="pw"><button>Sign in</button></form>',
    )

    assert result.kind is TargetKind.AUTH_WALL
    assert "authentication_wall" in result.reason_codes


@pytest.mark.parametrize(
    "path",
    ["/login", "/sign-in", "/signin", "/auth"],
)
def test_auth_wall_url_paths(path: str) -> None:
    from app.automation.targets import classify_target

    final_url = f"https://careers.example.test{path}"
    result = classify_target(
        "https://careers.example.test/role",
        final_url,
    )

    assert result.kind is TargetKind.AUTH_WALL
    assert "authentication_wall" in result.reason_codes


@pytest.mark.parametrize(
    "html",
    [
        '<form><div class="g-recaptcha"></div></form>',
        '<form><div data-sitekey="test-site-key"></div></form>',
    ],
)
def test_captcha_markup_is_human_challenge(html: str) -> None:
    from app.automation.targets import classify_target

    result = classify_target(
        "https://careers.example.test/role",
        "https://careers.example.test/apply",
        html=html,
    )

    assert result.kind is TargetKind.HUMAN_CHALLENGE
    assert "human_challenge" in result.reason_codes


def test_clean_anonymous_application_form_is_neither_wall_nor_challenge() -> None:
    from app.automation.targets import classify_target

    url = "https://boards.greenhouse.io/acme/jobs/1234"
    html = (
        "<main><h1>Summer Analyst</h1>"
        '<form id="application"><input name="name"><input name="email">'
        "<button>Submit</button></form></main>"
    )
    result = classify_target(url, url, html=html)

    assert result.kind is not TargetKind.AUTH_WALL
    assert result.kind is not TargetKind.HUMAN_CHALLENGE


def test_automation_eligible_only_for_application_kinds() -> None:
    assert TargetKind.AUTH_WALL.automation_eligible is False
    assert TargetKind.HUMAN_CHALLENGE.automation_eligible is False
    assert TargetKind.APPLICATION_FORM.automation_eligible is True
    assert TargetKind.APPLICATION_ENTRY.automation_eligible is True


def test_header_nav_login_words_at_normal_job_url_are_not_auth_wall() -> None:
    from app.automation.targets import classify_target

    url = "https://boards.greenhouse.io/acme/jobs/1234"
    html = (
        '<header><nav><a href="/login">Sign in</a>'
        '<a href="/help">login help</a></nav></header>'
        "<main><h1>Summer Analyst</h1>"
        '<form id="application"><input name="name"><input name="email">'
        "<button>Apply</button></form></main>"
    )
    result = classify_target(url, url, html=html)

    assert result.kind is not TargetKind.AUTH_WALL
