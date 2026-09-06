from __future__ import annotations

import importlib
import json
import socket
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from app.automation.targets import TargetResolution
from app.config import Settings
from app.db import Database
from app.domain.targets import TargetKind
from app.models import AuditEvent, Opportunity
from app.services.navigator import _SourceResolutionExecutor


EVERCORE_ROLE = "2027 Private Funds Group - Industrial Placement"
EVERCORE_TITLE = "Private Funds Group Industrial Placement (2027) | Evercore"


def _role_identity_module():
    return importlib.import_module("app.services.role_identity")


def _candidate(*, employer_text: str, title: str, source: str = "h1"):
    module = _role_identity_module()
    return module.RoleTitleCandidate(
        root_marker="root-1",
        root_text=employer_text,
        source=source,
        text=title,
    )


def _match(*, role: str, employer: str, employer_text: str, title: str):
    module = _role_identity_module()
    return module.match_role_identity_v2(
        expected_role=role,
        expected_employer=employer,
        candidates=(
            _candidate(employer_text=employer_text, title=title),
        ),
    )


@pytest.fixture(scope="module")
def browser_page() -> Page:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def test_role_match_v2_defaults_off(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})

    assert settings.role_match_v2_enabled is False


def test_role_match_v2_requires_explicit_opt_in(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_ROLE_MATCH_V2": "true",
        }
    )

    assert settings.role_match_v2_enabled is True


def test_exact_page_title_matches_without_regression() -> None:
    result = _match(
        role="Placement, Client Service, 2027",
        employer="AlphaSights",
        employer_text="AlphaSights Placement, Client Service, 2027 Apply now",
        title="Placement, Client Service, 2027",
    )

    assert result.matched is True
    assert result.decision == "match"
    assert result.method == "role_title_set_v2"
    assert result.coverage == 1.0
    assert result.jaccard == 1.0


def test_observed_evercore_reordered_title_matches() -> None:
    result = _match(
        role=EVERCORE_ROLE,
        employer="Evercore",
        employer_text=f"{EVERCORE_TITLE} Apply",
        title=EVERCORE_TITLE,
    )

    assert result.matched is True
    assert result.decision == "match"
    assert result.reason == "unique_high_confidence_title"
    assert result.overlap_count == 6
    assert result.expected_token_count == 6
    assert result.page_token_count == 6
    assert result.coverage == 1.0
    assert result.jaccard == 1.0


@pytest.mark.parametrize(
    ("role", "wrong_page_title"),
    [
        (
            "2027 Finance Industrial Placement",
            "2027 Risk Finance Industrial Placement",
        ),
        (
            "2027 Global Markets University Industrial Placement",
            "2027 London Global Markets University Industrial Placement",
        ),
        (
            "Finance Industrial Placement Programme 2027",
            "Finance Industrial Placement Programme 2027 2028",
        ),
        (
            "Private Markets Investment Specialist Industrial Placement",
            "Tax Private Markets Investment Specialist Industrial Placement",
        ),
    ],
)
def test_same_employer_near_neighbor_supersets_fail_closed(
    role: str,
    wrong_page_title: str,
) -> None:
    result = _match(
        role=role,
        employer="Evercore",
        employer_text=f"{wrong_page_title} | Evercore",
        title=wrong_page_title,
    )

    assert result.matched is False
    assert result.decision in {"mismatch", "abstain"}


def test_employer_match_requires_token_boundaries() -> None:
    result = _match(
        role="Placement Year 2027",
        employer="KIA",
        employer_text="Nokia Placement Year 2027",
        title="Placement Year 2027",
    )

    assert result.matched is False
    assert result.reason == "employer_identity_missing"


def test_employer_suffix_does_not_hide_same_word_as_role_qualifier() -> None:
    result = _match(
        role=(
            "2027 Global Markets University Industrial Placement Programme London"
        ),
        employer="Risk Capital",
        employer_text=(
            "2027 Risk Global Markets University Industrial Placement Programme "
            "London | Risk Capital"
        ),
        title=(
            "2027 Risk Global Markets University Industrial Placement Programme "
            "London | Risk Capital"
        ),
    )

    assert result.matched is False
    assert result.decision in {"mismatch", "abstain"}


@pytest.mark.parametrize(
    ("employer", "wrong_role", "page_title"),
    [
        ("Evercore", "Summer Internship (2027)", EVERCORE_TITLE),
        ("Evercore", "Advisory Spring Week (2027)", EVERCORE_TITLE),
        (
            "Evercore",
            "2026, Financial Sponsors Group, Off-Cycle Intern (6 months)",
            EVERCORE_TITLE,
        ),
        (
            "AlphaSights",
            "Insight Days, Client Service",
            "Placement, Client Service, 2027",
        ),
    ],
)
def test_real_same_employer_sibling_roles_do_not_match(
    employer: str,
    wrong_role: str,
    page_title: str,
) -> None:
    result = _match(
        role=wrong_role,
        employer=employer,
        employer_text=f"{employer} {page_title}",
        title=page_title,
    )

    assert result.matched is False
    assert result.decision == "mismatch"


def test_different_employer_does_not_match() -> None:
    result = _match(
        role=EVERCORE_ROLE,
        employer="Evercore",
        employer_text="Wiser Private Funds Group Industrial Placement (2027)",
        title="Private Funds Group Industrial Placement (2027) | Wiser",
    )

    assert result.matched is False
    assert result.decision == "abstain"
    assert result.reason == "employer_identity_missing"


def test_score_in_abstain_band_fails_closed() -> None:
    result = _match(
        role="Risk Industrial Placement 2027",
        employer="Nomura",
        employer_text="Nomura Industrial Placement Programme Risk 2026",
        title="Industrial Placement Programme Risk 2026",
    )

    assert result.matched is False
    assert result.decision == "abstain"
    assert result.reason == "score_in_abstain_band"
    assert result.coverage == 0.75
    assert result.jaccard == 0.5


def test_matcher_is_deterministic_and_makes_no_network_call(monkeypatch) -> None:
    def reject_network(*_args, **_kwargs):
        pytest.fail("the deterministic matcher attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    kwargs = {
        "role": EVERCORE_ROLE,
        "employer": "Evercore",
        "employer_text": f"{EVERCORE_TITLE} Apply",
        "title": EVERCORE_TITLE,
    }

    first = _match(**kwargs)
    second = _match(**kwargs)

    assert first == second


def _active_role_match(record: dict[str, str]) -> bool:
    module = _role_identity_module()
    role_identity = module._identity(record["expected_role"])
    page_identity = module._identity(record["candidate_title"])
    employer_identity = module._identity(record["employer"])
    padded_page = f" {page_identity} "
    exact_legacy = (
        f" {role_identity} " in padded_page
        and f" {employer_identity} " in padded_page
    )
    if exact_legacy:
        return True
    return _match(
        role=record["expected_role"],
        employer=record["employer"],
        employer_text=record["candidate_title"],
        title=record["candidate_title"],
    ).matched


def test_checked_in_labelled_set_has_exact_zero_false_positive_matrix() -> None:
    fixture_path = (
        Path(__file__).parents[1] / "fixtures" / "phase16_role_identity_labels.json"
    )
    labelled = json.loads(fixture_path.read_text(encoding="utf-8"))
    positives = labelled["positives"]
    negatives = labelled["negatives"]

    positive_decisions = [_active_role_match(item) for item in positives]
    negative_decisions = [_active_role_match(item) for item in negatives]

    assert len(positives) == 30
    assert len(negatives) >= 20
    assert all(
        _role_identity_module()._identity(item["employer"])
        in _role_identity_module()._identity(item["candidate_title"])
        for item in negatives
    )
    assert {
        "true_positive": sum(positive_decisions),
        "false_negative": len(positives) - sum(positive_decisions),
        "false_positive": sum(negative_decisions),
        "true_negative": len(negatives) - sum(negative_decisions),
    } == {
        "true_positive": 30,
        "false_negative": 0,
        "false_positive": 0,
        "true_negative": 28,
    }


class _FlagOffPage:
    url = "https://jobs.example.test/role"

    def __init__(self) -> None:
        self.evaluate_calls = 0

    def evaluate(self, script, _args=None):
        self.evaluate_calls += 1
        assert "argus-role-match-v2-probe" not in script
        return {
            "href": "",
            "count": 0,
            "control_count": 0,
            "visible_root_count": 2,
            "bound_job_root_found": False,
            "page_apply_affordance_count": 1,
            "bound_apply_affordance_count": 0,
            "eligible_destination_count": 0,
            "ambiguity_guard_triggered": False,
            "candidate_urls": [],
        }


class _EvercoreMismatchPage:
    url = "https://jobs.smartrecruiters.com/Wiser/744000143765020"

    def __init__(self) -> None:
        self.evaluate_calls = 0

    def evaluate(self, script, args=None):
        self.evaluate_calls += 1
        if "argus-role-match-v2-probe" in script:
            return {
                "candidates": [
                    {
                        "root_marker": "evercore-root",
                        "root_text": f"{EVERCORE_TITLE} Apply",
                        "source": "h1",
                        "text": EVERCORE_TITLE,
                    }
                ]
            }
        if "__argusRoleMatchV2Observer" in script:
            return {"bound": True, "marker": str((args or {}).get("marker") or "")}
        marker = str((args or {}).get("role_match_v2_marker") or "")
        if marker:
            return {
                "href": "",
                "count": 0,
                "control_count": 0,
                "visible_root_count": 8,
                "bound_job_root_found": True,
                "page_apply_affordance_count": 0,
                "bound_apply_affordance_count": 0,
                "eligible_destination_count": 0,
                "ambiguity_guard_triggered": False,
                "candidate_urls": [],
            }
        return {
            "href": "",
            "count": 0,
            "control_count": 0,
            "visible_root_count": 8,
            "bound_job_root_found": False,
            "page_apply_affordance_count": 0,
            "bound_apply_affordance_count": 0,
            "eligible_destination_count": 0,
            "ambiguity_guard_triggered": False,
            "candidate_urls": [],
        }


def _executor(*, enabled: bool) -> _SourceResolutionExecutor:
    return _SourceResolutionExecutor(
        source_url="https://jobs.smartrecruiters.com/Wiser/744000143765020",
        provider_hint="smartrecruiters",
        employer="Evercore",
        role_title=EVERCORE_ROLE,
        role_match_v2_enabled=enabled,
    )


def test_split_cards_cannot_combine_employer_title_and_apply(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main>
          <section><h2>Evercore careers</h2></section>
          <section><h1>Private Funds Group Industrial Placement (2027)</h1></section>
          <section><a href="https://jobs.smartrecruiters.com/Wiser/apply">Apply</a></section>
        </main>
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["outcome"] == "apply_bound_job_root_not_found"


def test_flag_on_revalidates_legacy_employer_token_boundaries(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article>
          <h1>Placement Year 2027</h1><p>Nokia careers</p>
          <a href="https://jobs.smartrecruiters.com/Nokia/apply">Apply</a>
        </article></main>
        """
    )
    kwargs = {
        "source_url": "https://jobs.smartrecruiters.com/Nokia/placement",
        "provider_hint": "smartrecruiters",
        "employer": "KIA",
        "role_title": "Placement Year 2027",
    }
    legacy = _SourceResolutionExecutor(**kwargs, role_match_v2_enabled=False)
    enabled = _SourceResolutionExecutor(**kwargs, role_match_v2_enabled=True)

    assert legacy._discover_application_link(browser_page) == (
        "https://jobs.smartrecruiters.com/Nokia/apply"
    )
    assert enabled._discover_application_link(browser_page) == ""
    assert enabled._apply_hop_evidence["bound_job_root_found"] is False


def test_flag_off_does_not_execute_v2_heading_traversal(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article>
          <h1>2027 Private Funds Group - Industrial Placement | Evercore</h1>
          <a href="https://jobs.smartrecruiters.com/Wiser/apply">Apply</a>
        </article></main>
        """
    )
    browser_page.locator("h1").evaluate(
        """
        node => {
          window.v2ClosestCalls = 0;
          const original = node.closest.bind(node);
          node.closest = (...args) => {
            window.v2ClosestCalls += 1;
            return original(...args);
          };
        }
        """
    )
    executor = _executor(enabled=False)

    assert executor._discover_application_link(browser_page) == (
        "https://jobs.smartrecruiters.com/Wiser/apply"
    )
    assert executor._apply_hop_evidence["outcome"] == "apply_candidate_selected"
    assert browser_page.evaluate("window.v2ClosestCalls") == 0


def test_flag_off_no_get_probe_does_not_execute_v2_heading_traversal(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article>
          <h1>2027 Private Funds Group - Industrial Placement | Evercore</h1>
          <button type="button">Apply</button>
        </article></main>
        """
    )
    browser_page.locator("h1").evaluate(
        """
        node => {
          window.v2ClosestCalls = 0;
          const original = node.closest.bind(node);
          node.closest = (...args) => {
            window.v2ClosestCalls += 1;
            return original(...args);
          };
        }
        """
    )
    executor = _executor(enabled=False)

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["outcome"] == (
        "apply_affordance_has_no_get_destination"
    )
    assert executor._discover_apply_affordance(browser_page) is not None
    assert browser_page.evaluate("window.v2ClosestCalls") == 0


def test_flag_on_exact_route_rejects_wrong_role_with_extra_cycle(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article>
          <h1>Finance Industrial Placement Programme 2027 2028 | Evercore</h1>
          <a href="https://jobs.smartrecruiters.com/Wiser/wrong/apply">Apply</a>
        </article></main>
        """
    )
    executor = _SourceResolutionExecutor(
        source_url="https://jobs.smartrecruiters.com/Wiser/placement",
        provider_hint="smartrecruiters",
        employer="Evercore",
        role_title="Finance Industrial Placement Programme 2027",
        role_match_v2_enabled=True,
    )

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["bound_job_root_found"] is False
    assert browser_page.locator("[data-argus-role-match-v2-root]").count() == 0


def test_document_title_cannot_bind_an_unrelated_single_article(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <title>Private Funds Group Industrial Placement (2027) | Evercore</title>
        <main><article>
          <h1>Summer Internship (2027) | Evercore</h1>
          <a href="https://jobs.smartrecruiters.com/Wiser/summer/apply">Apply</a>
        </article></main>
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["bound_job_root_found"] is False


def test_broad_root_cannot_hide_provider_apply_path_behind_view_label(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main>
          <h1>Private Funds Group Industrial Placement (2027) | Evercore</h1>
          <a href="https://jobs.smartrecruiters.com/Wiser/sibling/apply">View role</a>
        </main>
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["bound_job_root_found"] is False


def test_multi_job_single_article_cannot_bind_sibling_apply(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article>
          <h1>Private Funds Group Industrial Placement (2027) | Evercore</h1>
          <h2>Summer Internship (2027) | Evercore</h2>
          <a href="https://jobs.smartrecruiters.com/Wiser/summer/apply">Apply</a>
        </article></main>
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["bound_job_root_found"] is False


def test_one_narrow_job_card_can_bind_title_employer_and_apply(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main>
          <article>
            <h1>Private Funds Group Industrial Placement (2027) | Evercore</h1>
            <a href="https://jobs.smartrecruiters.com/Wiser/apply">Apply</a>
          </article>
          <article>
            <h1>Summer Internship (2027) | Evercore</h1>
            <a href="https://jobs.smartrecruiters.com/Wiser/summer/apply">Apply</a>
          </article>
        </main>
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == (
        "https://jobs.smartrecruiters.com/Wiser/apply"
    )


def test_marker_revalidation_rejects_dom_mutation_before_get_discovery(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article id="job">
          <h1>Private Funds Group Industrial Placement (2027) | Evercore</h1>
          <a href="https://jobs.smartrecruiters.com/Wiser/apply">Apply</a>
        </article></main>
        """
    )
    browser_page.evaluate(
        """
        new MutationObserver(records => {
          if (records.some(record => record.attributeName ===
              'data-argus-role-match-v2-root')) {
            document.querySelector('#job h1').textContent =
              'Summer Internship (2027) | Evercore';
          }
        }).observe(document.querySelector('#job'), {attributes: true});
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["bound_job_root_found"] is False


def test_marker_revalidation_rejects_mutation_before_no_get_click_probe(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article id="job">
          <h1>Private Funds Group Industrial Placement (2027) | Evercore</h1>
          <button type="button">Apply</button>
        </article></main>
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == ""
    assert executor._apply_hop_evidence["outcome"] == (
        "apply_affordance_has_no_get_destination"
    )
    assert browser_page.locator("[data-argus-role-match-v2-root]").count() == 1
    browser_page.locator("#job h1").evaluate(
        "node => node.textContent = 'Summer Internship (2027) | Evercore'"
    )

    assert executor._discover_apply_affordance(browser_page) is None
    assert browser_page.locator("[data-argus-role-match-v2-root]").count() == 0


def test_visibility_attribute_mutation_invalidates_root_before_click(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article id="job">
          <h1 id="selected">Private Funds Group Industrial Placement (2027) | Evercore</h1>
          <h1 id="sibling" hidden>Summer Internship (2027) | Evercore</h1>
          <button id="apply" type="button">Apply</button>
        </article></main>
        <script>
          window.clickCount = 0;
          document.querySelector('#apply').addEventListener('click', () => {
            window.clickCount += 1;
          });
        </script>
        """
    )
    executor = _executor(enabled=True)
    assert executor._discover_application_link(browser_page) == ""
    affordance = executor._discover_apply_affordance(browser_page)
    assert affordance is not None
    browser_page.locator("#selected").evaluate("node => node.hidden = true")
    browser_page.locator("#sibling").evaluate("node => node.hidden = false")

    from app.services.resolution_apply_click import guarded_apply_click

    result = guarded_apply_click(browser_page, affordance, timeout_ms=1_000)
    assert result.clicked is False
    assert browser_page.evaluate("window.clickCount") == 0


def test_exact_title_visibility_mutation_is_root_scoped_before_click(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <main><article id="job">
          <h1 id="selected">Placement, Client Service, 2027</h1>
          <p>AlphaSights</p>
          <h1 id="sibling" hidden>Insight Days, Client Service</h1>
          <button id="apply" type="button">Apply</button>
        </article></main>
        <script>
          window.clickCount = 0;
          document.querySelector('#apply').addEventListener('click', () => {
            window.clickCount += 1;
          });
        </script>
        """
    )
    executor = _SourceResolutionExecutor(
        source_url="https://www.alphasights.com/job/placement-client-service-2027/",
        provider_hint="unknown",
        employer="AlphaSights",
        role_title="Placement, Client Service, 2027",
        role_match_v2_enabled=True,
    )
    assert executor._discover_application_link(browser_page) == ""
    affordance = executor._discover_apply_affordance(browser_page)
    assert affordance is not None
    assert browser_page.locator("[data-argus-role-match-v2-root]").count() == 1
    browser_page.locator("#selected").evaluate("node => node.hidden = true")
    browser_page.locator("#sibling").evaluate("node => node.hidden = false")

    from app.services.resolution_apply_click import guarded_apply_click

    result = guarded_apply_click(browser_page, affordance, timeout_ms=1_000)
    assert result.clicked is False
    assert browser_page.evaluate("window.clickCount") == 0


def test_submit_control_stays_denied_and_never_dispatches_submit(
    browser_page: Page,
) -> None:
    browser_page.set_content(
        """
        <form id="application"><article>
          <h1>Private Funds Group Industrial Placement (2027) | Evercore</h1>
          <button type="submit">Apply</button>
        </article></form>
        <script>
          window.submitCount = 0;
          document.querySelector('#application').addEventListener('submit', event => {
            event.preventDefault(); window.submitCount += 1;
          });
        </script>
        """
    )
    executor = _executor(enabled=True)

    assert executor._discover_application_link(browser_page) == ""
    affordance = executor._discover_apply_affordance(browser_page)
    assert affordance is not None
    from app.services.resolution_apply_click import guarded_apply_click

    result = guarded_apply_click(browser_page, affordance, timeout_ms=1_000)
    assert result.clicked is False
    assert result.outcome == "apply_click_submit_denylist_refused"
    assert browser_page.evaluate("window.submitCount") == 0


def test_flag_off_preserves_exact_existing_discovery_evidence() -> None:
    page = _FlagOffPage()
    executor = _executor(enabled=False)

    assert executor._discover_application_link(page) == ""

    assert page.evaluate_calls == 1
    assert executor._apply_hop_evidence == {
        "attempted": True,
        "source_url": page.url,
        "hop_chain": [page.url],
        "candidate_count": 0,
        "control_count": 0,
        "candidate_urls": [],
        "visible_root_count": 2,
        "bound_job_root_found": False,
        "page_apply_affordance_count": 1,
        "bound_apply_affordance_count": 0,
        "eligible_destination_count": 0,
        "ambiguity_guard_triggered": False,
        "navigation_started": False,
        "origin_binding": "not_reached",
        "egress_guard": "not_reached",
        "destination_verification": "not_reached",
        "outcome": "apply_bound_job_root_not_found",
    }


def test_enabled_match_audits_decision_score_and_compared_strings() -> None:
    page = _EvercoreMismatchPage()
    executor = _executor(enabled=True)

    assert executor._discover_application_link(page) == ""

    hop = executor._apply_hop_evidence
    assert hop["bound_job_root_found"] is True
    assert hop["outcome"] == "apply_control_not_found"
    assert hop["role_identity"] == {
        "decision": "match",
        "method": "role_title_set_v2",
        "reason": "unique_high_confidence_title",
        "expected_employer": "Evercore",
        "expected_role_title": EVERCORE_ROLE,
        "compared_page_title": EVERCORE_TITLE,
        "title_source": "h1",
        "employer_matched": True,
        "overlap_count": 6,
        "expected_word_count": 6,
        "page_word_count": 6,
        "coverage": 1.0,
        "jaccard": 1.0,
        "extra_page_word_count": 0,
        "match_max_extra_page_words": 0,
        "match_coverage_threshold": 1.0,
        "match_jaccard_threshold": 0.8,
        "mismatch_coverage_threshold": 0.5,
        "mismatch_jaccard_threshold": 0.35,
        "qualifying_title_count": 1,
    }


def test_role_match_v2_never_confers_submission_authority() -> None:
    executor = _executor(enabled=True)

    assert not hasattr(executor, "before_click")
    assert not hasattr(executor, "submission_binding")


def test_role_identity_evidence_is_written_to_the_hash_chained_audit(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import TargetResolutionService

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Evercore",
            role_title=EVERCORE_ROLE,
            programme_group="year_in_industry",
            cycle="2026-27",
            url="https://jobs.smartrecruiters.com/Wiser/744000143765020",
            source="trackr_live",
        )
        session.add(opportunity)
        session.flush()
        role_identity = _match(
            role=EVERCORE_ROLE,
            employer="Evercore",
            employer_text=EVERCORE_TITLE,
            title=EVERCORE_TITLE,
        ).audit_evidence()
        result = TargetResolution(
            source_url=opportunity.url,
            final_url=opportunity.url,
            kind=TargetKind.JOB_DETAIL,
            provider="smartrecruiters",
            identity_verified=False,
            reason_codes=("unverified_job_detail", "apply_control_not_found"),
            evidence={"apply_hop": {"role_identity": role_identity}},
        )

        TargetResolutionService(session).record(opportunity.id, result)

    with database.session_scope() as session:
        event = (
            session.query(AuditEvent)
            .filter(AuditEvent.event_type == "opportunity.target_resolved")
            .one()
        )
        details = json.loads(event.details_json)
        assert details["role_identity"] == role_identity
        assert details["role_identity"]["extra_page_word_count"] == 0
