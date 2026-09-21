"""Realistic custom-portal fixtures for the structural field-context extractor.

Fixtures resemble non-ATS career portals (Rothschild / Point72 / Jane Street
style markup): deeply nested divs, nonstandard field names, section headings
for Education / Work Authorisation / Diversity, radio groups with a shared
question, and the three required markers.
"""

from app.automation.classifier import DeterministicClassifier
from app.automation.field_context import (
    FieldContext,
    extract_field_context,
    field_context_to_question,
)
from app.domain.questions import CanonicalKey


def test_label_via_for_id_with_nonstandard_name() -> None:
    doc = """
    <html><body>
      <form id="apply">
        <div class="form-row"><div class="fld">
          <label for="cand_q_7x2">Given name(s)</label>
          <input type="text" id="cand_q_7x2" name="q_7x2" placeholder="e.g. Ada" />
        </div></div>
      </form>
    </body></html>
    """
    ctx = extract_field_context('<input type="text" id="cand_q_7x2" name="q_7x2">', doc)

    assert ctx.label == "Given name(s)"
    assert ctx.name == "q_7x2"
    assert ctx.element_id == "cand_q_7x2"
    assert ctx.input_type == "text"
    assert "Given name(s)" in ctx.context_text


def test_wrapping_label() -> None:
    doc = """
    <html><body><form>
      <label class="check">I certify that the information provided is accurate
        <input type="checkbox" name="att_1" />
      </label>
    </form></body></html>
    """
    ctx = extract_field_context('<input type="checkbox" name="att_1">', doc)

    assert "certify" in ctx.label.casefold()
    assert "input" not in ctx.label.casefold() or "certify" in ctx.label.casefold()
    assert ctx.input_type == "checkbox"


def test_aria_label_and_labelledby() -> None:
    doc = """
    <html><body><form>
      <span id="lbl-mobile">Contact telephone</span>
      <input type="tel" name="f_221" aria-labelledby="lbl-mobile" />
      <input type="text" name="f_222" aria-label="LinkedIn profile URL" />
    </form></body></html>
    """
    by_ref = extract_field_context(
        '<input type="tel" name="f_221" aria-labelledby="lbl-mobile">', doc
    )
    direct = extract_field_context(
        '<input type="text" name="f_222" aria-label="LinkedIn profile URL">', doc
    )

    assert by_ref.label == "Contact telephone"
    assert direct.label == "LinkedIn profile URL"


def test_fieldset_legend_grouping() -> None:
    doc = """
    <html><body><form>
      <fieldset class="grp">
        <legend>Work Authorisation</legend>
        <div><label for="w1">Are you legally authorised to work in the UK?</label>
        <input type="text" id="w1" name="custom_88" /></div>
      </fieldset>
    </form></body></html>
    """
    ctx = extract_field_context('<input type="text" id="w1" name="custom_88">', doc)

    assert ctx.fieldset_legend == "Work Authorisation"
    assert "Work Authorisation" in ctx.context_text


def test_nearest_preceding_heading_sections() -> None:
    doc = """
    <html><body><form>
      <h2>Education</h2>
      <div class="row"><input type="text" name="edu_blah" title="University" /></div>
      <h2>Diversity</h2>
      <div class="row"><input type="text" name="div_blah" title="Ethnicity" /></div>
    </form></body></html>
    """
    edu = extract_field_context(
        '<input type="text" name="edu_blah" title="University">', doc,
        selector='input[name="edu_blah"]',
    )
    div = extract_field_context(
        '<input type="text" name="div_blah" title="Ethnicity">', doc,
        selector='input[name="div_blah"]',
    )

    assert edu.section_heading == "Education"
    assert div.section_heading == "Diversity"
    assert edu.title == "University"


def test_radio_group_shared_question_and_options() -> None:
    doc = """
    <html><body><form>
      <fieldset>
        <legend>Will you now or in the future require visa sponsorship?</legend>
        <div class="opt"><input type="radio" id="sp-y" name="sp_req" value="yes" />
          <label for="sp-y">Yes</label></div>
        <div class="opt"><input type="radio" id="sp-n" name="sp_req" value="no" />
          <label for="sp-n">No</label></div>
      </fieldset>
    </form></body></html>
    """
    ctx = extract_field_context(
        '<input type="radio" id="sp-y" name="sp_req" value="yes">', doc
    )

    assert ctx.input_type == "radio"
    assert "sponsorship" in ctx.group_label.casefold()
    assert set(ctx.group_options) == {"Yes", "No"}
    assert "Yes" in ctx.context_text and "No" in ctx.context_text


def test_required_via_attribute() -> None:
    ctx = extract_field_context(
        '<input type="email" name="em" required>',
        "<html><body><form><input type='email' name='em' required></form></body></html>",
    )

    assert ctx.required is True
    assert "required-attr" in ctx.required_evidence


def test_required_via_aria_required() -> None:
    ctx = extract_field_context(
        '<input type="text" name="ux" aria-required="true">',
        "<html><body><form><input type='text' name='ux' aria-required='true'></form></body></html>",
    )

    assert ctx.required is True
    assert "aria-required" in ctx.required_evidence


def test_required_via_asterisk_in_label() -> None:
    doc = """
    <html><body><form>
      <label for="fn">First name *</label>
      <input type="text" id="fn" name="field_01" />
    </form></body></html>
    """
    ctx = extract_field_context('<input type="text" id="fn" name="field_01">', doc)

    assert ctx.required is True
    assert "asterisk-in-label" in ctx.required_evidence


def test_help_text_via_describedby_and_hint_sibling() -> None:
    doc = """
    <html><body><form>
      <label for="cv">Upload CV</label>
      <input type="file" id="cv" name="doc_9" aria-describedby="cv-hint" />
      <small id="cv-hint" class="hint">PDF only, max 5MB</small>
    </form></body></html>
    """
    ctx = extract_field_context('<input type="file" id="cv" name="doc_9">', doc)

    assert "PDF only" in ctx.help_text
    assert ctx.input_type == "file"


def test_placeholder_autocomplete_and_select_options() -> None:
    doc = """
    <html><body><form>
      <h2>Education</h2>
      <label for="uni">Institution</label>
      <input type="text" id="uni" name="x1" placeholder="e.g. Imperial College"
             autocomplete="organization" />
      <label for="gy">Graduation</label>
      <select id="gy" name="x2">
        <option value="">Select…</option>
        <option value="2026">2026</option>
        <option value="2027">2027</option>
      </select>
    </form></body></html>
    """
    text_ctx = extract_field_context(
        '<input type="text" id="uni" name="x1">', doc
    )
    sel_ctx = extract_field_context('<select id="gy" name="x2"></select>', doc)

    assert text_ctx.placeholder == "e.g. Imperial College"
    assert text_ctx.autocomplete == "organization"
    assert sel_ctx.input_type == "select"
    assert "2026" in sel_ctx.select_options
    assert "2027" in sel_ctx.select_options
    assert "2026" in sel_ctx.option_values


def test_malformed_and_missing_label_returns_partial_without_raising() -> None:
    broken_doc = "<html><body><form><div><input type=text name=noquote><div>"
    ctx = extract_field_context("<input type=text name=noquote>", broken_doc)

    assert isinstance(ctx, FieldContext)
    assert ctx.name == "noquote"
    assert ctx.label == ""

    empty = extract_field_context("", "")
    assert isinstance(empty, FieldContext)
    assert empty.context_text == ""

    none_ctx = extract_field_context(None, None)
    assert isinstance(none_ctx, FieldContext)

    garbage = extract_field_context("<div><span>not a field", "<<<not html>>>")
    assert isinstance(garbage, FieldContext)


def test_context_text_priority_order() -> None:
    doc = """
    <html><body><form>
      <h2>Work Authorisation</h2>
      <fieldset><legend>Eligibility</legend>
        <label for="e1">Right to work question</label>
        <input type="text" id="e1" name="zz9" placeholder="Details" />
      </fieldset>
    </form></body></html>
    """
    ctx = extract_field_context('<input type="text" id="e1" name="zz9">', doc)

    label_pos = ctx.context_text.find("Right to work question")
    legend_pos = ctx.context_text.find("Eligibility")
    heading_pos = ctx.context_text.find("Work Authorisation")
    name_pos = ctx.context_text.find("zz9")
    assert -1 not in (label_pos, legend_pos, heading_pos, name_pos)
    assert label_pos < legend_pos < heading_pos < name_pos


def test_bridge_to_form_question_enriches_classifier_match() -> None:
    """A bare nonstandard name is UNKNOWN; enriched context matches."""
    doc = """
    <html><body><form>
      <fieldset>
        <legend>Will you now or in the future require visa sponsorship?</legend>
        <div><input type="radio" id="s1" name="q_spons" value="yes" />
        <label for="s1">Yes</label></div>
        <div><input type="radio" id="s2" name="q_spons" value="no" />
        <label for="s2">No</label></div>
      </fieldset>
    </form></body></html>
    """
    from app.domain.questions import FormQuestion

    bare = FormQuestion(label="", field_type="radio", name="q_spons")
    assert DeterministicClassifier().classify(bare).canonical_key == CanonicalKey.UNKNOWN

    ctx = extract_field_context(
        '<input type="radio" id="s1" name="q_spons" value="yes">', doc
    )
    enriched = ctx.to_form_question()
    assert isinstance(enriched, type(bare))
    mapping = DeterministicClassifier().classify(enriched)
    assert mapping.canonical_key == CanonicalKey.SPONSORSHIP

    # Functional alias agrees.
    assert field_context_to_question(ctx) == enriched


def test_context_bleed_bug_reproduction() -> None:
    """Reproduce the context bleed bug from the issue.

    Two-field document where field 1's help text leaks into field 2's context.
    """
    doc = """
    <html><body><form>
      <h2>Personal Details</h2>
      <fieldset><legend>Contact Information</legend>
        <label>Given name *</label>
        <input type="text" name="q_7x2" />
        <small class="hint">Enter your legal given name</small>
      </fieldset>
      <h2>Education</h2>
      <label>Which university did you attend?</label>
      <input type="text" name="edu_1" placeholder="e.g. Imperial College" />
    </form></body></html>
    """

    # Extract context for the university field (field 2)
    uni_ctx = extract_field_context(
        '<input type="text" name="edu_1" placeholder="e.g. Imperial College">',
        doc,
        selector='input[name="edu_1"]'
    )

    # The bug: field 1's help text "Enter your legal given name" leaks into field 2
    print(f"context_text: {uni_ctx.context_text}")
    print(f"placeholder: {uni_ctx.placeholder}")
    print(f"help_text: {uni_ctx.help_text}")
    print(f"label: {uni_ctx.label}")
    print(f"section_heading: {uni_ctx.section_heading}")
    print(f"fieldset_legend: {uni_ctx.fieldset_legend}")

    # These assertions should pass after the fix
    assert "given name" not in uni_ctx.context_text.casefold(), f"Context bleed: 'given name' found in context_text: {uni_ctx.context_text}"
    assert "legal given name" not in uni_ctx.context_text.casefold(), f"Context bleed: 'legal given name' found in context_text: {uni_ctx.context_text}"
    assert uni_ctx.placeholder == "e.g. Imperial College", f"Placeholder contaminated: {uni_ctx.placeholder}"
    assert uni_ctx.help_text == "", f"Help text should be empty for university field, got: {uni_ctx.help_text}"
    assert uni_ctx.label == "Which university did you attend?", f"Wrong label: {uni_ctx.label}"
    assert uni_ctx.section_heading == "Education", f"Wrong section heading: {uni_ctx.section_heading}"
    assert uni_ctx.fieldset_legend == "", f"Fieldset legend should not leak from previous section: {uni_ctx.fieldset_legend}"


def test_multi_field_form_no_context_bleed() -> None:
    """Test a realistic 6-field form across 2 sections with no context bleed.

    Each field's context must contain ONLY its own label/help/placeholder,
    plus shared section heading/fieldset legend. No other field's text.
    """
    doc = """
    <html><body><form>
      <h2>Personal Details</h2>
      <fieldset><legend>Contact Information</legend>
        <div class="field-row">
          <label>Given name *</label>
          <input type="text" name="given_name" placeholder="e.g. Ada" />
          <small class="hint">Enter your legal given name</small>
        </div>
        <div class="field-row">
          <label>Family name *</label>
          <input type="text" name="family_name" placeholder="e.g. Lovelace" />
          <small class="hint">Enter your legal family name</small>
        </div>
        <div class="field-row">
          <label>Email *</label>
          <input type="email" name="email" placeholder="e.g. ada@example.com" />
        </div>
        <div class="field-row">
          <label>Phone</label>
          <input type="tel" name="phone" placeholder="+44 20 7946" />
          <small class="hint">UK format preferred</small>
        </div>
      </fieldset>
      <h2>Education</h2>
      <fieldset><legend>Education Details</legend>
        <div class="field-row">
          <label>Which university did you attend?</label>
          <input type="text" name="university" placeholder="e.g. Imperial College" />
        </div>
        <div class="field-row">
          <label>Expected graduation date</label>
          <input type="date" name="grad_date" placeholder="YYYY-MM-DD" />
          <small class="hint">Expected month and year of graduation</small>
        </div>
      </fieldset>
    </form></body></html>
    """

    # Expected canonical keys for each field
    expected_keys = {
        "given_name": CanonicalKey.FIRST_NAME,
        "family_name": CanonicalKey.LAST_NAME,
        "email": CanonicalKey.EMAIL,
        "phone": CanonicalKey.PHONE,
        "university": CanonicalKey.UNIVERSITY,
        "grad_date": CanonicalKey.GRADUATION_YEAR,
    }

    # Forbidden text that must NOT appear in other fields' contexts
    # (Shared section headings and fieldset legends are ALLOWED)
    forbidden_text = {
        "given_name": ["family name", "legal family name", "ada@example.com", "+44 20 7946", "imperial college", "uk format preferred", "expected month"],
        "family_name": ["given name", "legal given name", "ada@example.com", "+44 20 7946", "imperial college", "uk format preferred", "expected month"],
        "email": ["given name", "family name", "legal given name", "legal family name", "+44 20 7946", "imperial college", "uk format preferred", "expected month"],
        "phone": ["given name", "family name", "legal given name", "legal family name", "ada@example.com", "imperial college", "expected month"],
        "university": ["given name", "family name", "legal given name", "legal family name", "ada@example.com", "+44 20 7946", "uk format preferred", "ada", "lovelace"],
        "grad_date": ["given name", "family name", "legal given name", "legal family name", "ada@example.com", "+44 20 7946", "imperial college", "uk format preferred", "ada", "lovelace"],
    }

    classifier = DeterministicClassifier()

    for field_name, expected_key in expected_keys.items():
        ctx = extract_field_context(
            f'<input type="text" name="{field_name}">',
            doc,
            selector=f'input[name="{field_name}"]'
        )

        # Verify no forbidden text from other fields
        context_lower = ctx.context_text.casefold()
        for forbidden in forbidden_text[field_name]:
            assert forbidden not in context_lower, (
                f"Context bleed in {field_name}: forbidden text '{forbidden}' found in context: {ctx.context_text}"
            )

        # Verify own label is present
        assert ctx.label != "", f"Label missing for {field_name}"

        # Verify placeholder is correct (not contaminated)
        if field_name == "given_name":
            assert ctx.placeholder == "e.g. Ada"
        elif field_name == "family_name":
            assert ctx.placeholder == "e.g. Lovelace"
        elif field_name == "email":
            assert ctx.placeholder == "e.g. ada@example.com"
        elif field_name == "phone":
            assert ctx.placeholder == "+44 20 7946"
        elif field_name == "university":
            assert ctx.placeholder == "e.g. Imperial College"
        elif field_name == "grad_date":
            assert ctx.placeholder == "YYYY-MM-DD"

        # Verify help text is only from own field
        if field_name == "given_name":
            assert "legal given name" in ctx.help_text.casefold()
            assert "legal family name" not in ctx.help_text.casefold()
        elif field_name == "family_name":
            assert "legal family name" in ctx.help_text.casefold()
            assert "legal given name" not in ctx.help_text.casefold()
        elif field_name == "phone":
            assert "uk format" in ctx.help_text.casefold()
        elif field_name == "grad_date":
            assert "graduation" in ctx.help_text.casefold()
        elif field_name in ("email", "university"):
            assert ctx.help_text == "", f"Unexpected help text for {field_name}: {ctx.help_text}"

        # Verify shared context IS present
        assert ctx.section_heading in ("Personal Details", "Education"), f"Wrong section heading for {field_name}: {ctx.section_heading}"
        assert ctx.fieldset_legend in ("Contact Information", "Education Details"), f"Wrong fieldset legend for {field_name}: {ctx.fieldset_legend}"

        # Verify classification
        enriched = ctx.to_form_question()
        mapping = classifier.classify(enriched)
        assert mapping.canonical_key == expected_key, (
            f"Classification failed for {field_name}: expected {expected_key}, got {mapping.canonical_key}. "
            f"Context: {ctx.context_text}"
        )


def test_single_field_document_regression_guard() -> None:
    """Regression guard: single-field document behaves exactly as before."""
    doc = """
    <html><body><form>
      <h2>Education</h2>
      <label for="uni">Institution</label>
      <input type="text" id="uni" name="x1" placeholder="e.g. Imperial College"
             autocomplete="organization" />
    </form></body></html>
    """
    ctx = extract_field_context(
        '<input type="text" id="uni" name="x1" placeholder="e.g. Imperial College" autocomplete="organization">',
        doc
    )

    assert ctx.label == "Institution"
    assert ctx.placeholder == "e.g. Imperial College"
    assert ctx.autocomplete == "organization"
    assert ctx.section_heading == "Education"
    assert ctx.help_text == ""
    assert "Institution" in ctx.context_text
    assert "Education" in ctx.context_text
    assert "Imperial College" in ctx.context_text
    assert "organization" in ctx.context_text
