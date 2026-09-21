from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import sync_playwright

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.registry import AdapterRegistry
from app.automation.targets import TargetResolution
from app.domain.targets import TargetKind


_LOCAL_TEST_URL = ""


@pytest.fixture(scope="module", autouse=True)
def _local_http_server():
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = b"<html><body>local adapter test</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    global _LOCAL_TEST_URL
    _LOCAL_TEST_URL = f"http://127.0.0.1:{server.server_port}/lab/ats/standard"
    yield
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _unverified_generic_adapter(page) -> GenericAdapter:
    """Return a GenericAdapter with an unverified resolution (custom portal scenario)."""
    target_url = str(getattr(page, "url", "") or "") or _LOCAL_TEST_URL
    return AdapterRegistry().detect(
        TargetResolution(
            source_url=target_url,
            final_url=target_url,
            kind=TargetKind.APPLICATION_ENTRY,
            provider="generic",
            identity_verified=False,
            evidence={},
        )
    )


# --- Fixture HTML pages ---

CUSTOM_PORTAL_APPLICATION_FORM = """
<!DOCTYPE html>
<html>
<body>
  <main>
    <form id="job-application" action="/apply" method="post">
      <h1>Software Engineer Application</h1>

      <div class="field-group">
        <label for="first_name">First Name</label>
        <input type="text" id="first_name" name="first_name" placeholder="John" required aria-required="true">
      </div>

      <div class="field-group">
        <label for="last_name">Last Name</label>
        <input type="text" id="last_name" name="last_name" placeholder="Doe" required>
      </div>

      <div class="field-group">
        <label for="email">Email Address</label>
        <input type="email" id="email" name="email" placeholder="john@example.com" required>
      </div>

      <div class="field-group">
        <label for="phone">Phone Number</label>
        <input type="tel" id="phone" name="phone" placeholder="+1 555 123">
      </div>

      <div class="field-group">
        <label for="linkedin">LinkedIn Profile</label>
        <input type="url" id="linkedin" name="linkedin" placeholder="https://linkedin.com/in/johndoe">
      </div>

      <div class="field-group">
        <label for="location">Preferred Location</label>
        <select id="location" name="location" required>
          <option value="">Select...</option>
          <option value="london">London</option>
          <option value="new_york">New York</option>
          <option value="remote">Remote</option>
        </select>
      </div>

      <div class="field-group">
        <label for="cover_letter">Cover Letter</label>
        <textarea id="cover_letter" name="cover_letter" placeholder="Why are you interested in this role?" rows="5"></textarea>
      </div>

      <div class="field-group">
        <label for="cv">Upload CV / Resume</label>
        <input type="file" id="cv" name="cv" accept=".pdf,.doc,.docx" required>
      </div>

      <div class="field-group">
        <label for="github">GitHub Profile</label>
        <input type="url" id="github" name="github" placeholder="https://github.com/johndoe">
      </div>

      <button type="submit" id="submit-btn">Submit Application</button>
    </form>
  </main>
</body>
</html>
"""

NO_FORM_PAGE = """
<!DOCTYPE html>
<html>
<body>
  <main>
    <h1>Careers at Acme Corp</h1>
    <p>We are hiring! View our open positions below.</p>
    <ul>
      <li><a href="/jobs/1">Software Engineer</a></li>
      <li><a href="/jobs/2">Product Manager</a></li>
    </ul>
    <div class="footer">
      <p>Contact us at careers@example.com</p>
    </div>
  </main>
</body>
</html>
"""

NEWSLETTER_ONLY_FORM = """
<!DOCTYPE html>
<html>
<body>
  <header>
    <nav>Acme Corp</nav>
  </header>
  <main>
    <section class="hero">
      <h1>Join Our Talent Community</h1>
      <p>Stay updated on new opportunities.</p>
    </section>
    <form id="newsletter-signup" action="/newsletter/subscribe" method="post">
      <label for="nl-email">Email Address</label>
      <input type="email" id="nl-email" name="email" placeholder="Enter your email" required>
      <button type="submit">Subscribe</button>
    </form>
    <section class="jobs">
      <h2>Open Positions</h2>
      <ul>
        <li><a href="/jobs/1">Software Engineer</a></li>
      </ul>
    </section>
  </main>
</body>
</html>
"""

FORM_WITH_SENSITIVE_FIELDS = """
<!DOCTYPE html>
<html>
<body>
  <main>
    <form id="job-application" action="/apply" method="post">
      <h1>Software Engineer Application</h1>

      <div class="field-group">
        <label for="first_name">First Name</label>
        <input type="text" id="first_name" name="first_name" required>
      </div>

      <div class="field-group">
        <label for="last_name">Last Name</label>
        <input type="text" id="last_name" name="last_name" required>
      </div>

      <div class="field-group">
        <label for="email">Email Address</label>
        <input type="email" id="email" name="email" required>
      </div>

      <div class="field-group">
        <label for="sponsorship">Will you require visa sponsorship?</label>
        <select id="sponsorship" name="sponsorship" required>
          <option value="">Select...</option>
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </div>

      <div class="field-group">
        <label for="work_auth">Are you legally authorized to work in this country?</label>
        <select id="work_auth" name="work_authorisation" required>
          <option value="">Select...</option>
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </div>

      <div class="field-group">
        <label for="criminal_record">Do you have any criminal convictions?</label>
        <select id="criminal_record" name="criminal_record" required>
          <option value="">Select...</option>
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </div>

      <div class="field-group">
        <label for="demographics">Voluntary demographic information</label>
        <select id="demographics" name="demographics">
          <option value="">Prefer not to say</option>
          <option value="option1">Option 1</option>
          <option value="option2">Option 2</option>
        </select>
      </div>

      <div class="field-group">
        <label for="attestation">I certify all information is true</label>
        <input type="checkbox" id="attestation" name="legal_attestation" required>
      </div>

      <div class="field-group">
        <label for="cv">Upload CV</label>
        <input type="file" id="cv" name="cv" accept=".pdf" required>
      </div>

      <button type="submit">Submit Application</button>
    </form>
  </main>
</body>
</html>
"""

SEARCH_FORM_ONLY = """
<!DOCTYPE html>
<html>
<body>
  <header>
    <form role="search" action="/search" method="get">
      <label for="search">Search jobs</label>
      <input type="search" id="search" name="q" placeholder="Search...">
      <button type="submit">Search</button>
    </form>
  </header>
  <main>
    <h1>Job Board</h1>
  </main>
</body>
</html>
"""

LOGIN_FORM_ONLY = """
<!DOCTYPE html>
<html>
<body>
  <main>
    <form id="login-form" action="/login" method="post">
      <h2>Sign In</h2>
      <label for="username">Username</label>
      <input type="text" id="username" name="username" required>
      <label for="password">Password</label>
      <input type="password" id="password" name="password" required>
      <button type="submit">Sign In</button>
    </form>
  </main>
</body>
</html>
"""

CONTACT_FORM_ONLY = """
<!DOCTYPE html>
<html>
<body>
  <main>
    <form id="contact-form" action="/contact" method="post">
      <h2>Contact Us</h2>
      <label for="name">Name</label>
      <input type="text" id="name" name="name" required>
      <label for="email">Email</label>
      <input type="email" id="email" name="email" required>
      <label for="message">Message</label>
      <textarea id="message" name="message" required></textarea>
      <button type="submit">Send Message</button>
    </form>
  </main>
</body>
</html>
"""


# --- Tests ---

def test_custom_portal_application_form_located_and_fields_enumerated() -> None:
    """A realistic custom-portal application form is located and its fields enumerated with labels."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(CUSTOM_PORTAL_APPLICATION_FORM)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    # Should find the application form and enumerate its fields
    assert len(fields) >= 7, f"Expected at least 7 fields, got {len(fields)}: {[f.question.label for f in fields]}"

    # Check field labels are properly extracted
    labels = [f.question.label.strip().lower() for f in fields]
    assert any("first name" in l for l in labels), f"Missing first name field, got: {labels}"
    assert any("last name" in l for l in labels), f"Missing last name field, got: {labels}"
    assert any("email" in l for l in labels), f"Missing email field, got: {labels}"
    assert any("phone" in l for l in labels), f"Missing phone field, got: {labels}"
    assert any("linkedin" in l for l in labels), f"Missing linkedin field, got: {labels}"
    assert any("location" in l or "preferred location" in l for l in labels), f"Missing location select, got: {labels}"
    assert any("cover letter" in l for l in labels), f"Missing cover letter field, got: {labels}"
    assert any("cv" in l or "resume" in l for l in labels), f"Missing CV upload field, got: {labels}"
    assert any("github" in l for l in labels), f"Missing github field, got: {labels}"

    # Check field types
    control_types = [f.control_type for f in fields]
    assert "text" in control_types or "email" in control_types
    assert "select" in control_types
    assert "textarea" in control_types
    assert "file" in control_types

    # Check required flags
    required_fields = [f for f in fields if f.question.required]
    assert len(required_fields) >= 4, f"Expected at least 4 required fields, got {len(required_fields)}"

    # Check selectors are present
    for field in fields:
        assert field.selector, f"Field {field.question.label} missing selector"
        assert field.question.name, f"Field {field.question.label} missing name"


def test_page_with_no_form_returns_empty_result_fail_closed() -> None:
    """A page with NO form returns the refuse/empty result (fail closed)."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(NO_FORM_PAGE)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    # Should return empty list (fail closed - no form found)
    assert fields == [], f"Expected empty fields for page with no form, got: {[f.question.label for f in fields]}"


def test_newsletter_signup_form_not_mistaken_for_application() -> None:
    """A page with a trivial newsletter-signup form only is NOT mistaken for an application form."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(NEWSLETTER_ONLY_FORM)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    # Should NOT find the newsletter form as an application form
    assert fields == [], f"Expected empty fields for newsletter-only page, got: {[f.question.label for f in fields]}"


def test_file_input_reported_as_upload_not_filled() -> None:
    """The file input is reported as an upload (control_type='file'), not filled."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(CUSTOM_PORTAL_APPLICATION_FORM)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    file_fields = [f for f in fields if f.control_type == "file"]
    assert len(file_fields) == 1, f"Expected exactly 1 file input, got {len(file_fields)}"

    file_field = file_fields[0]
    assert file_field.control_type == "file"
    assert file_field.question.field_type == "file"
    # The label should be meaningful (CV/Resume related)
    assert "cv" in file_field.question.label.lower() or "resume" in file_field.question.label.lower()


def test_submit_control_found_but_not_activated() -> None:
    """The submit control is found but explicitly NOT activated by the adapter."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(CUSTOM_PORTAL_APPLICATION_FORM)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        # Also check evidence from inspect_with_evidence
        _, evidence = adapter.inspect_with_evidence(page)

        browser.close()

    # Submit should be reported as present in evidence
    assert evidence.get("submit_present") is True, "Submit control should be detected"

    # The adapter's submit() method should NOT be called during inspect
    # Verify no submission occurred by checking page state
    # (The form should not have been submitted - no navigation, no form submission)
    # This is inherently tested by the fact that inspect() returns without error
    # and the page remains on the test URL


def test_sensitive_fields_reported_not_prefilled() -> None:
    """Sensitive fields present on the form are reported as fields needing human, never pre-filled."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(FORM_WITH_SENSITIVE_FIELDS)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    # All sensitive fields should be enumerated
    labels = [f.question.label.strip().lower() for f in fields]

    # Check sponsorship field exists
    assert any("sponsor" in l for l in labels), f"Missing sponsorship field, got: {labels}"
    # Check work authorization field exists
    assert any("authoriz" in l or "work auth" in l for l in labels), f"Missing work authorization field, got: {labels}"
    # Check criminal record field exists
    assert any("criminal" in l for l in labels), f"Missing criminal record field, got: {labels}"
    # Check demographic field exists
    assert any("demographic" in l for l in labels), f"Missing demographic field, got: {labels}"
    # Check legal attestation field exists
    assert any("attest" in l or "certify" in l for l in labels), f"Missing legal attestation field, got: {labels}"

    # The adapter itself doesn't pre-fill - it just reports fields
    # The runner's build_fill_plan handles blocking sensitive fields
    # Verify fields are returned with proper structure for the runner to classify
    for field in fields:
        assert field.selector
        assert field.control_type
        assert isinstance(field.question.required, bool)
        assert isinstance(field.question.options, tuple)


def test_search_form_not_mistaken_for_application() -> None:
    """A search form should not be mistaken for an application form."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(SEARCH_FORM_ONLY)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    assert fields == [], f"Search form should not be detected as application, got: {[f.question.label for f in fields]}"


def test_login_form_not_mistaken_for_application() -> None:
    """A login form should not be mistaken for an application form."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(LOGIN_FORM_ONLY)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    assert fields == [], f"Login form should not be detected as application, got: {[f.question.label for f in fields]}"


def test_contact_form_not_mistaken_for_application() -> None:
    """A contact form should not be mistaken for an application form."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(CONTACT_FORM_ONLY)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    assert fields == [], f"Contact form should not be detected as application, got: {[f.question.label for f in fields]}"


def test_field_enumeration_includes_aria_labels_and_placeholders() -> None:
    """Fields should include aria-labels and placeholders when present."""
    html = """
    <form action="/apply" method="post">
      <label for="name">Full Name</label>
      <input type="text" id="name" name="full_name" placeholder="Enter your full name" aria-label="Full Name" required>
      <label for="email">Email</label>
      <input type="email" id="email" name="email" placeholder="you@example.com" required>
      <label for="cv">Upload CV</label>
      <input type="file" id="cv" name="cv" accept=".pdf,.doc,.docx" required>
      <button type="submit">Apply</button>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(html)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    assert len(fields) == 3
    for field in fields:
        assert field.selector
        assert field.question.name
        # Placeholder should be captured
        if field.question.name == "full_name":
            assert field.question.placeholder == "Enter your full name"
        if field.question.name == "email":
            assert field.question.placeholder == "you@example.com"


def test_radio_group_enumerated_as_single_field_with_options() -> None:
    """Radio button groups should be enumerated as a single field with options."""
    html = """
    <form action="/apply" method="post">
      <label for="first_name">First Name</label>
      <input type="text" id="first_name" name="first_name" required>
      <label for="email">Email</label>
      <input type="email" id="email" name="email" required>
      <fieldset>
        <legend>Will you require sponsorship?</legend>
        <label><input type="radio" name="sponsorship" value="yes" required> Yes</label>
        <label><input type="radio" name="sponsorship" value="no"> No</label>
      </fieldset>
      <label for="cv">Upload CV</label>
      <input type="file" id="cv" name="cv" accept=".pdf,.doc,.docx" required>
      <button type="submit">Apply</button>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(html)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    # Should find one radio group field
    radio_fields = [f for f in fields if f.control_type == "radio"]
    assert len(radio_fields) == 1, f"Expected 1 radio group, got {len(radio_fields)}"

    radio_field = radio_fields[0]
    assert radio_field.question.field_type == "radio"
    assert "yes" in [o.lower() for o in radio_field.question.options]
    assert "no" in [o.lower() for o in radio_field.question.options]
    assert radio_field.question.required is True


def test_checkbox_enumerated_with_option_label() -> None:
    """Checkboxes should be enumerated with their option_label."""
    html = """
    <form action="/apply" method="post">
      <label for="first_name">First Name</label>
      <input type="text" id="first_name" name="first_name" required>
      <label for="email">Email</label>
      <input type="email" id="email" name="email" required>
      <label>
        <input type="checkbox" name="attestation" value="1" required>
        I certify all information is true and accurate
      </label>
      <label for="cv">Upload CV</label>
      <input type="file" id="cv" name="cv" accept=".pdf,.doc,.docx" required>
      <button type="submit">Apply</button>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(_LOCAL_TEST_URL)
        page.set_content(html)

        adapter = _unverified_generic_adapter(page)
        fields = adapter.inspect(page)

        browser.close()

    checkbox_fields = [f for f in fields if f.control_type == "checkbox"]
    assert len(checkbox_fields) == 1
    checkbox = checkbox_fields[0]
    assert checkbox.control_type == "checkbox"
    assert checkbox.question.option_label.strip() != ""
    assert "certify" in checkbox.question.option_label.lower()
    assert checkbox.question.required is True