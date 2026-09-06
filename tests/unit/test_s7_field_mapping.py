"""S7 Field Map: verify the 12 real failing labels resolve to correct canonical keys.

See brief_s7_fieldmap.md for the diagnosed root causes and policy decisions.
"""
from __future__ import annotations

from app.automation.classifier import DeterministicClassifier
from app.domain.questions import CanonicalKey, FormQuestion


class TestS7FieldMapping:
    """Regression: each of the 12 real failing labels maps to the right key."""

    classifier = DeterministicClassifier()

    def _classify(self, label: str, field_type: str = "text", *, required: bool = True) -> CanonicalKey:
        return self.classifier.classify(
            FormQuestion(label=label, field_type=field_type, required=required)
        ).canonical_key

    # --- Labels that should now resolve to a specific key (Root Causes A, B, C) ---

    def test_01_referral_source(self) -> None:
        """#1 — 'How did you hear about this internship?*' → SOURCE (Root Cause A)"""
        assert self._classify("How did you hear about this internship?*") is CanonicalKey.SOURCE

    def test_02_overall_grade(self) -> None:
        """#2 — 'Overall Grade*' → FINAL_GRADE (Root Cause B)"""
        assert self._classify("Overall Grade*") is CanonicalKey.FINAL_GRADE

    def test_03_privacy_attestation(self) -> None:
        """#3 — 'Privacy *' → LEGAL_ATTESTATION (Root Cause C)"""
        assert self._classify("Privacy *") is CanonicalKey.LEGAL_ATTESTATION

    def test_04_confirm_understand_internship(self) -> None:
        """#4 — 'Please confirm you understand this is a 12-month internship*' → LEGAL_ATTESTATION"""
        assert (
            self._classify("Please confirm you understand this is a 12-month internship*")
            is CanonicalKey.LEGAL_ATTESTATION
        )

    def test_05_military_service(self) -> None:
        """#5 — 'Have you served in the military?*' → DEMOGRAPHIC (Root Cause C)"""
        assert (
            self._classify("Have you served in the military?*")
            is CanonicalKey.DEMOGRAPHIC
        )

    # --- Labels that must remain UNKNOWN (deliberately left as human-gated) ---

    def test_06_english_fluency_remains_unknown(self) -> None:
        """#6 — 'Do you speak English at a Fluent or Native level?*' → UNKNOWN (human-only)"""
        assert (
            self._classify("Do you speak English at a Fluent or Native level?*")
            is CanonicalKey.UNKNOWN
        )

    def test_07_previously_applied_point72_remains_unknown(self) -> None:
        """#7 — 'Have you previously applied to work at Point72?*' → UNKNOWN (human-only)"""
        assert (
            self._classify("Have you previously applied to work at Point72?*")
            is CanonicalKey.UNKNOWN
        )

    def test_08_outstanding_offers_remains_unknown(self) -> None:
        """#8 — 'Do you have any outstanding offers or deadlines?*' → UNKNOWN (human-only)"""
        assert (
            self._classify("Do you have any outstanding offers or deadlines?*")
            is CanonicalKey.UNKNOWN
        )

    def test_09_office_days_remains_unknown(self) -> None:
        """#9 — 'Are you willing to work in the office 5-days a week? *' → UNKNOWN (human-only)"""
        assert (
            self._classify("Are you willing to work in the office 5-days a week? *")
            is CanonicalKey.UNKNOWN
        )

    def test_10_full_time_2028_remains_unknown(self) -> None:
        """#10 — 'Will you be ready for full-time employment in 2028?*' → UNKNOWN (human-only)"""
        assert (
            self._classify("Will you be ready for full-time employment in 2028?*")
            is CanonicalKey.UNKNOWN
        )

    # --- Label #11: optional free text, left omitted ---

    def test_11_note_to_hiring_manager(self) -> None:
        """#11 — 'Note to Hiring Manager' (optional, not required) → UNKNOWN"""
        key = self.classifier.classify(
            FormQuestion(label="Note to Hiring Manager", field_type="textarea", required=False)
        ).canonical_key
        assert key is CanonicalKey.UNKNOWN

    # --- Label #12: long instruction, not required ---

    def test_12_apply_one_internship_instruction(self) -> None:
        """#12 — long instruction text (not required) → UNKNOWN"""
        key = self.classifier.classify(
            FormQuestion(
                label=(
                    "In an effort to streamline the process, please only apply to one internship. "
                    "You will be evaluated for all internship opportunities during the screening process."
                ),
                field_type="text",
                required=False,
            )
        ).canonical_key
        assert key is CanonicalKey.UNKNOWN

    # --- Regression guard: the narrowed internship rule still catches genuine history ---

    def test_genuine_internship_history_remains_unknown(self) -> None:
        """A genuine internship-history question ('Have you previously completed an internship?') → UNKNOWN.

        This proves the line-162 narrowing preserved its original intent.
        """
        assert (
            self._classify("Have you previously completed an internship?")
            is CanonicalKey.UNKNOWN
        )