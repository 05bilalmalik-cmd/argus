from __future__ import annotations

from app.automation.adapters.registry import AdapterRegistry
from app.automation.targets import TargetResolution
from app.domain.targets import TargetKind
from playwright.sync_api import sync_playwright


def _target() -> TargetResolution:
    return TargetResolution(source_url="https://jobs.smartrecruiters.com/acme/123-role", final_url="https://jobs.smartrecruiters.com/acme/123-role", kind=TargetKind.APPLICATION_ENTRY,
        provider="smartrecruiters", identity_verified=True, evidence={"structured_feed": "smartrecruiters:fixture", "employer": "ARGUS Test Capital", "role": "Summer Analyst", "requisition": "/lab/ats/smartrecruiters/application"})


def test_adapter_name_and_exact_matcher():
    from app.automation.adapters.smartrecruiters import SmartRecruitersAdapter
    assert SmartRecruitersAdapter.name == "smartrecruiters"
    assert SmartRecruitersAdapter.matches("https://jobs.smartrecruiters.com/acme/123-role")
    assert not SmartRecruitersAdapter.matches("https://careers.example.test/jobs/smartrecruiters", '<main data-ats="smartrecruiters">')


def test_registry_accepts_verified_provider_resolution():
    assert AdapterRegistry().detect(_target()).name == "smartrecruiters"


def test_registry_keeps_unverified_resolution_generic():
    result = TargetResolution(source_url="https://jobs.smartrecruiters.com/acme/123-role", final_url="https://jobs.smartrecruiters.com/acme/123-role", kind=TargetKind.APPLICATION_ENTRY,
        provider="smartrecruiters", identity_verified=False, evidence={"structured_feed": "smartrecruiters:fixture", "employer": "ARGUS Test Capital", "role": "Summer Analyst", "requisition": "/lab/ats/smartrecruiters/application"})
    assert AdapterRegistry().detect(result).name == "generic"


JOURNEY_HTML = """
<!doctype html><html><body>
<div id="distractions"><button id="newsletter" type="button" onclick="window.__newsletterClicks=(window.__newsletterClicks||0)+1">Subscribe to job alerts</button><a id="search" href="/search" onclick="window.__searchClicks=(window.__searchClicks||0)+1">Search jobs</a></div>
<main data-ats="smartrecruiters" data-employer="ARGUS Test Capital" data-role="Summer Analyst">
  <section id="entry"><button id="apply" type="button">Apply</button></section>
</main>
<script>
const state={step:0}; window.__newsletterClicks=0; window.__searchClicks=0;
const render = step => {
 state.step=step;
 if (step===0) {
  document.getElementById('entry').innerHTML = '<form id="grnhse_app" class="ats-form" data-ats-application data-step="contact" method="post" action="/lab/ats/smartrecruiters/application/submit"><label>First name<input id="first" name="first_name" required></label><label>Email<input id="email" name="email" type="email" required></label><button id="next" type="button">Next</button></form>';
  document.getElementById('first').addEventListener('input', () => {
   if (!document.getElementById('graduation')) document.getElementById('next').insertAdjacentHTML('beforebegin', '<label>Graduation year<input id="graduation" name="graduation_year" required></label><fieldset><legend>Will you require sponsorship?</legend><label><input id="sponsor-no" type="radio" name="sponsorship" value="No" required>No</label></fieldset>');
  });
  document.getElementById('next').onclick=()=>{ if(document.getElementById('grnhse_app').checkValidity()) render(1); };
 } else {
  document.getElementById('entry').innerHTML='<form id="grnhse_app" class="ats-form" data-ats-application data-step="review" method="post" action="/lab/ats/smartrecruiters/application/submit"><input name="first_name" value="Demo" readonly><input name="email" value="demo@example.test" readonly><input name="graduation_year" value="2029" readonly><input name="sponsorship" value="No" readonly><button id="final-submit" data-automation-id="submitButton" type="submit">Submit application</button></form>';
 }
};
document.getElementById('apply').onclick=()=>render(0);
</script></body></html>
"""


def test_complete_loopback_journey_is_bounded_and_receipted():
    from app.automation.adapters.smartrecruiters import SmartRecruitersAdapter

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        post_count = []
        page.route("**/*", lambda route: (post_count.append(route.request.url) if route.request.method == "POST" else None) or route.fulfill(status=200, content_type="text/html", body=(
            '<html><body><h1>Application received</h1><p>Reference: ARG-SMARTRECRUITERS-123</p></body></html>'
            if route.request.method == "POST" else JOURNEY_HTML
        )))
        page.goto("https://jobs.smartrecruiters.com/acme/123-role")
        adapter = SmartRecruitersAdapter(_target())

        page.locator("#apply").click()
        fields, evidence = adapter.inspect_with_evidence(page)
        assert evidence["root_found"] is True
        assert {field.question.name for field in fields} == {"first_name", "email"}
        adapter.fill(page, next(field for field in fields if field.question.name == "first_name"), "Demo")
        adapter.fill(page, next(field for field in fields if field.question.name == "email"), "demo@example.test")
        assert adapter.verify_step(page, fields, {"first_name": "Demo", "email": "demo@example.test"}) is True

        fields, _ = adapter.inspect_with_evidence(page)
        assert {field.question.name for field in fields} >= {"first_name", "graduation_year", "sponsorship"}
        adapter.fill(page, next(field for field in fields if field.question.name == "graduation_year"), "2029")
        adapter.fill(page, next(field for field in fields if field.question.name == "sponsorship"), "No")
        assert adapter.verify_step(page, fields, {"first_name": "Demo", "graduation_year": "2029", "sponsorship": "No"}) is True

        assert adapter.advance_one_step(page) is True
        fields, evidence = adapter.inspect_with_evidence(page)
        assert evidence["root_found"] is True
        assert evidence["submit_present"] is True
        target = adapter.submission_target(page, adapter._last_form_handle)
        assert target is not None and target.control_selector.endswith("#final-submit")
        assert page.locator("#final-submit").count() == 1

        distractions = page.evaluate("() => [window.__newsletterClicks, window.__searchClicks]")
        with page.expect_response(lambda response: response.request.method == "POST") as response_info:
            page.locator("#final-submit").click()
        response = response_info.value
        assert response.status == 200
        assert page.locator("body").inner_text().find("Reference: ARG-SMARTRECRUITERS-123") >= 0
        assert distractions == [0, 0]
        assert len(post_count) == 1
        browser.close()
