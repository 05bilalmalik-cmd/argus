"""Tests for FIELD CONTEXT wiring in the live fill path.

These tests verify that the JS inspection script's collected context (options,
option_label) is carried through to the classifier via enriched FormQuestion.label,
and that sensitive keys remain refused regardless of context richness.
"""

import os
from app.automation.adapters.generic import _enrich_label_for_classifier
from app.automation.classifier import DeterministicClassifier
from app.automation.runner import build_fill_plan
from app.automation.types import InspectedField
from app.domain.questions import CanonicalKey, FormQuestion, Sensitivity


def enriched_field(
    label: str,
    field_type: str = "text",
    *,
    required: bool = True,
    options=(),
    option_label: str = "",
) -> InspectedField:
    """Create an InspectedField with label enriched like the live adapter does."""
    enriched_label = _enrich_label_for_classifier(label, field_type, tuple(options), option_label)
    return InspectedField(
        selector="#field",
        control_type=field_type,
        question=FormQuestion(
            label=enriched_label,
            field_type=field_type,
            required=required,
            options=tuple(options),
            option_label=option_label,
        ),
    )


def raw_field(
    label: str,
    field_type: str = "text",
    *,
    required: bool = True,
    options=(),
    option_label: str = "",
) -> InspectedField:
    """Create an InspectedField WITHOUT enrichment (for testing regression)."""
    return InspectedField(
        selector="#field",
        control_type=field_type,
        question=FormQuestion(
            label=label,
            field_type=field_type,
            required=required,
            options=tuple(options),
            option_label=option_label,
        ),
    )


class TestLabelEnrichment:
    """Test the label enrichment helper directly."""

    def test_select_enriches_with_options(self) -> None:
        enriched = _enrich_label_for_classifier(
            "Graduation year", "select", ("2026", "2027", "2028"), ""
        )
        assert "Graduation year" in enriched
        assert "2026" in enriched
        assert "2027" in enriched
        assert "2028" in enriched
        assert enriched.count(" — ") == 3  # label + 3 options

    def test_radio_enriches_with_options(self) -> None:
        enriched = _enrich_label_for_classifier(
            "Will you require sponsorship?", "radio", ("Yes", "No"), ""
        )
        assert "Will you require sponsorship?" in enriched
        assert "Yes" in enriched
        assert "No" in enriched

    def test_checkbox_enriches_with_option_label(self) -> None:
        enriched = _enrich_label_for_classifier(
            "I certify", "checkbox", (), "I certify the information is accurate"
        )
        assert "I certify" in enriched
        assert "information is accurate" in enriched

    def test_text_field_no_enrichment(self) -> None:
        enriched = _enrich_label_for_classifier("First name", "text", (), "")
        assert enriched == "First name"

    def test_deduplication_prevents_repeats(self) -> None:
        enriched = _enrich_label_for_classifier(
            "Yes", "radio", ("Yes", "No"), ""
        )
        # "Yes" appears as both label and option - should only appear once
        assert enriched == "Yes — No"

    def test_empty_label_uses_options(self) -> None:
        enriched = _enrich_label_for_classifier("", "select", ("2026", "2027"), "")
        assert enriched == "2026 — 2027"


class TestLivePathClassification:
    """Test classification through the live fill path (runner -> classifier)."""

    def test_ambiguous_label_disambiguated_by_options(self) -> None:
        """A thin label 'Graduation year' with select options maps to GRADUATION_YEAR.

        The label already contains 'Graduation year' which matches the pattern.
        The options provide additional confirmation.
        """
        plan = build_fill_plan(
            [enriched_field("Graduation year", "select", options=("2026", "2027", "2028"))],
            DeterministicClassifier(),
            {"education.graduation_year": "2026"},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.GRADUATION_YEAR
        assert plan.actions[0].status == "resolved"
        assert plan.actions[0].value == "2026"

    def test_ambiguous_label_disambiguated_by_radio_options(self) -> None:
        """A label with 'sponsor' keyword and radio options maps to SPONSORSHIP."""
        plan = build_fill_plan(
            [enriched_field("Sponsorship required?", "radio", options=("Yes", "No"))],
            DeterministicClassifier(),
            {"legal.sponsorship": False},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        # Sponsorship is LEGAL gated - should be blocked with missing answer
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.SPONSORSHIP
        assert plan.actions[0].status == "blocked"
        assert "approved_legal_answer_missing" in plan.risk.blocking_codes

    def test_checkbox_option_label_used_for_classification(self) -> None:
        """Checkbox with thin label but descriptive option_label maps to LEGAL_ATTESTATION."""
        plan = build_fill_plan(
            [enriched_field("Confirm", "checkbox", option_label="I certify the information is accurate")],
            DeterministicClassifier(),
            {},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.LEGAL_ATTESTATION
        assert plan.actions[0].status == "blocked"

    def test_confident_mapping_unchanged_by_added_context(self) -> None:
        """A confident deterministic mapping is NOT changed by added context (classifier level)."""
        classifier = DeterministicClassifier()

        # Raw label
        raw_q = FormQuestion(label="Country", field_type="select", options=("UK", "US", "France"))
        raw_mapping = classifier.classify(raw_q)

        # Enriched label (as adapter would produce)
        enriched_label = _enrich_label_for_classifier("Country", "select", ("UK", "US", "France"), "")
        enriched_q = FormQuestion(label=enriched_label, field_type="select", options=("UK", "US", "France"))
        enriched_mapping = classifier.classify(enriched_q)

        # Both should map to COUNTRY with high confidence
        assert raw_mapping.canonical_key == CanonicalKey.COUNTRY
        assert enriched_mapping.canonical_key == CanonicalKey.COUNTRY
        assert enriched_mapping.confidence >= 0.9

    def test_confident_first_name_unchanged(self) -> None:
        """First name mapping stays authoritative even with extra context (classifier level)."""
        classifier = DeterministicClassifier()

        raw_q = FormQuestion(label="First name", field_type="text")
        raw_mapping = classifier.classify(raw_q)

        # Text fields don't get enriched, but verify classifier stability
        enriched_q = FormQuestion(label="First name", field_type="text")
        enriched_mapping = classifier.classify(enriched_q)

        assert raw_mapping.canonical_key == CanonicalKey.FIRST_NAME
        assert enriched_mapping.canonical_key == CanonicalKey.FIRST_NAME
        assert enriched_mapping.confidence >= 0.9

    def test_semantic_classifier_flag_off_unchanged_behavior(self) -> None:
        """With ARGUS_SEMANTIC_CLASSIFIER_ENABLED off, behavior is identical."""
        os.environ.pop("ARGUS_SEMANTIC_CLASSIFIER_ENABLED", None)

        plan = build_fill_plan(
            [enriched_field("Graduation year", "select", options=("2026", "2027"))],
            DeterministicClassifier(),
            {"education.graduation_year": "2026"},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        # Should still map via deterministic (enriched label)
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.GRADUATION_YEAR

    def test_sensitive_sponsorship_refused_despite_rich_context(self) -> None:
        """SPONSORSHIP with obvious context is STILL refused and escalated."""
        plan = build_fill_plan(
            [enriched_field(
                "Will you now or in the future require visa sponsorship?",
                "radio",
                options=("Yes", "No")
            )],
            DeterministicClassifier(),
            {"legal.sponsorship": True},  # Even with approved value!
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        # Must be blocked - legal declarations never auto-filled
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.SPONSORSHIP
        assert plan.actions[0].status == "blocked"
        assert plan.actions[0].value is None
        assert "approved_legal_answer_missing" in plan.risk.blocking_codes

    def test_sensitive_work_authorisation_refused_despite_rich_context(self) -> None:
        """WORK_AUTHORISATION with obvious context is STILL refused."""
        plan = build_fill_plan(
            [enriched_field(
                "Are you legally authorised to work in the UK?",
                "radio",
                options=("Yes", "No")
            )],
            DeterministicClassifier(),
            {"legal.work_authorisation": True},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.WORK_AUTHORISATION
        assert plan.actions[0].status == "blocked"
        assert plan.actions[0].value is None

    def test_sensitive_criminal_record_refused_despite_rich_context(self) -> None:
        """CRIMINAL_RECORD with obvious context is STILL refused."""
        plan = build_fill_plan(
            [enriched_field(
                "Do you have a criminal record?",
                "radio",
                options=("Yes", "No")
            )],
            DeterministicClassifier(),
            {"legal.criminal_record": False},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.CRIMINAL_RECORD
        assert plan.actions[0].status == "blocked"
        assert plan.actions[0].value is None

    def test_sensitive_legal_attestation_refused_despite_rich_context(self) -> None:
        """LEGAL_ATTESTATION with obvious context is STILL refused."""
        plan = build_fill_plan(
            [enriched_field(
                "I certify that the information provided is accurate",
                "checkbox",
                option_label="I certify that the information provided is accurate"
            )],
            DeterministicClassifier(),
            {},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.LEGAL_ATTESTATION
        assert plan.actions[0].status == "blocked"
        assert plan.actions[0].value is None

    def test_sensitive_demographic_refused_despite_rich_context(self) -> None:
        """DEMOGRAPHIC with obvious context is STILL refused."""
        plan = build_fill_plan(
            [enriched_field(
                "What is your ethnic background?",
                "select",
                options=("White", "Black", "Asian", "Other")
            )],
            DeterministicClassifier(),
            {},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.DEMOGRAPHIC
        assert plan.actions[0].status == "blocked"
        assert plan.actions[0].value is None

    def test_sensitive_assessment_refused_despite_rich_context(self) -> None:
        """ASSESSMENT with obvious context is STILL refused."""
        plan = build_fill_plan(
            [enriched_field(
                "Begin online assessment",
                "button",
            )],
            DeterministicClassifier(),
            {},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.ASSESSMENT
        assert plan.actions[0].status == "blocked"

    def test_sensitive_captcha_refused_despite_rich_context(self) -> None:
        """CAPTCHA with obvious context is STILL refused."""
        plan = build_fill_plan(
            [enriched_field(
                "Verify you are human",
                "captcha",
            )],
            DeterministicClassifier(),
            {},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.CAPTCHA
        assert plan.actions[0].status == "blocked"


class TestContextIsolation:
    """Verify context from one field does not leak into another (no context bleed)."""

    def test_select_options_not_leaked_to_next_field(self) -> None:
        """Options from a select field don't contaminate the next field's classification."""
        plan = build_fill_plan(
            [
                enriched_field("Graduation year", "select", options=("2026", "2027")),
                enriched_field("University", "text"),
            ],
            DeterministicClassifier(),
            {"education.university": "Imperial"},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.GRADUATION_YEAR
        assert plan.actions[1].mapping.canonical_key == CanonicalKey.UNIVERSITY
        assert plan.actions[1].value == "Imperial"

    def test_radio_options_not_leaked_to_next_field(self) -> None:
        """Radio options from one field don't contaminate the next field."""
        plan = build_fill_plan(
            [
                enriched_field("Sponsorship required?", "radio", options=("Yes", "No")),
                enriched_field("First name", "text"),
            ],
            DeterministicClassifier(),
            {"identity.first_name": "Ada"},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.SPONSORSHIP
        assert plan.actions[1].mapping.canonical_key == CanonicalKey.FIRST_NAME
        assert plan.actions[1].value == "Ada"

    def test_checkbox_option_label_not_leaked(self) -> None:
        """Checkbox option_label doesn't leak to subsequent fields."""
        plan = build_fill_plan(
            [
                enriched_field("Confirm", "checkbox", option_label="I certify the information is accurate"),
                enriched_field("Last name", "text"),
            ],
            DeterministicClassifier(),
            {"identity.last_name": "Lovelace"},
            answer_lookup=lambda k, l: None,
            document_lookup=lambda k: None,
            adapter_name="generic",
        )
        assert plan.actions[0].mapping.canonical_key == CanonicalKey.LEGAL_ATTESTATION
        assert plan.actions[1].mapping.canonical_key == CanonicalKey.LAST_NAME
        assert plan.actions[1].value == "Lovelace"


class TestSemanticClassifierIntegration:
    """Test semantic classifier integration with enriched context."""

    def test_semantic_classifier_refuses_sensitive_keys(self) -> None:
        """Semantic classifier refuses sensitive keys even with rich context.

        Uses a field that deterministic classifier returns UNKNOWN for,
        so semantic layer runs and must refuse the sensitive mapping.
        The final result must be UNKNOWN + blocked (escalated to human).
        """
        os.environ["ARGUS_SEMANTIC_CLASSIFIER_ENABLED"] = "1"
        os.environ["ARGUS_SEMANTIC_CLASSIFIER_THRESHOLD"] = "0.2"
        try:
            from app.automation.classifier import CompositeClassifier
            from app.automation.semantic_classifier import SemanticClassifier

            classifier = CompositeClassifier(
                deterministic=DeterministicClassifier(),
                fallback=SemanticClassifier(),
            )

            # "Have you been convicted" - deterministic returns UNKNOWN
            # Semantic matches CRIMINAL_RECORD phrase but must refuse
            plan = build_fill_plan(
                [enriched_field("Have you been convicted", "radio", options=("Yes", "No"))],
                classifier,
                {},
                answer_lookup=lambda k, l: None,
                document_lookup=lambda k: None,
                adapter_name="generic",
            )
            # Must be UNKNOWN (refused) and blocked (escalated to human)
            assert plan.actions[0].mapping.canonical_key == CanonicalKey.UNKNOWN
            assert plan.actions[0].status == "blocked"
            assert plan.actions[0].value is None
        finally:
            os.environ.pop("ARGUS_SEMANTIC_CLASSIFIER_ENABLED", None)
            os.environ.pop("ARGUS_SEMANTIC_CLASSIFIER_THRESHOLD", None)

    def test_semantic_classifier_uses_enriched_context_for_unknown(self) -> None:
        """Semantic classifier benefits from enriched context on UNKNOWN fields."""
        os.environ["ARGUS_SEMANTIC_CLASSIFIER_ENABLED"] = "1"
        os.environ["ARGUS_SEMANTIC_CLASSIFIER_THRESHOLD"] = "0.3"
        try:
            from app.automation.classifier import CompositeClassifier
            from app.automation.semantic_classifier import SemanticClassifier

            classifier = CompositeClassifier(
                deterministic=DeterministicClassifier(),
                fallback=SemanticClassifier(),
            )

            # Thin label "Institution" - deterministic returns UNKNOWN
            # But enriched with select options "Harvard", "MIT" -> semantic matches UNIVERSITY
            plan = build_fill_plan(
                [enriched_field("Institution", "select", options=("Harvard", "MIT", "Stanford"))],
                classifier,
                {"education.university": "MIT"},
                answer_lookup=lambda k, l: None,
                document_lookup=lambda k: None,
                adapter_name="generic",
            )
            # Should map via semantic layer
            assert plan.actions[0].mapping.canonical_key == CanonicalKey.UNIVERSITY
            assert plan.actions[0].value == "MIT"
        finally:
            os.environ.pop("ARGUS_SEMANTIC_CLASSIFIER_ENABLED", None)
            os.environ.pop("ARGUS_SEMANTIC_CLASSIFIER_THRESHOLD", None)