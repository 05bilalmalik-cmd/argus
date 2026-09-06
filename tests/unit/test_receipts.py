import pytest

from app.automation.receipts import parse_receipt_text


def test_receipt_parser_extracts_reference_from_confirmation() -> None:
    receipt = parse_receipt_text(
        "Thank you for applying. Your application has been submitted. Reference: ARG-48291",
        "https://jobs.example.test/confirmation",
    )

    assert receipt is not None
    assert receipt.reference == "ARG-48291"
    assert "submitted" in receipt.confirmation_text.lower()


def test_receipt_parser_rejects_unrelated_page_text() -> None:
    assert parse_receipt_text("Apply for this role", "https://jobs.example.test/apply") is None


def test_receipt_parser_does_not_treat_confirmation_heading_as_reference() -> None:
    receipt = parse_receipt_text(
        "CONFIRMATION VERIFIED Thank you for applying. Your application has been submitted. Reference: ARG-AB12CD34",
        "https://jobs.example.test/confirmation",
    )

    assert receipt is not None
    assert receipt.reference == "ARG-AB12CD34"


def test_same_page_confirmation_without_reference_is_not_sufficient_evidence() -> None:
    from app.automation.receipts import receipt_has_submission_evidence

    receipt = parse_receipt_text(
        "Thank you for applying. Your application has been submitted.",
        "https://jobs.example.test/apply",
    )

    assert receipt is not None
    assert receipt_has_submission_evidence(receipt, "https://jobs.example.test/apply") is False


def test_reference_or_confirmation_navigation_is_sufficient_evidence() -> None:
    from app.automation.receipts import receipt_has_submission_evidence

    with_reference = parse_receipt_text(
        "Thank you for applying. Reference: ARG-7788",
        "https://jobs.example.test/apply",
    )
    navigated = parse_receipt_text(
        "Thank you for applying. Your application has been submitted.",
        "https://jobs.example.test/confirmation",
    )

    assert with_reference is not None
    assert navigated is not None
    assert receipt_has_submission_evidence(with_reference, "https://jobs.example.test/apply") is True
    assert receipt_has_submission_evidence(navigated, "https://jobs.example.test/apply") is True


def _evidence(**overrides):
    from app.automation.receipts import ReceiptEvidence

    values = {
        "url_before_click": "https://jobs.example.test/apply",
        "dom_had_reference": False,
        "dom_confirmation_text_present": False,
        "baseline_captured": True,
        "page_id": "page-1",
        "frame_url": "https://jobs.example.test/apply",
        "root_selector": "#application",
        "control_selector": "#submit",
        "control_fingerprint": "control-1",
        "form_action": "https://jobs.example.test/api/apply",
        "target_method": "POST",
        "bound_target_fingerprint": "target-1",
        "target_url": "https://jobs.example.test/confirmation",
        "destination": "https://jobs.example.test/confirmation",
        "provider": "example",
        "bound_intent_id": "intent-1",
        "bound_intent_nonce": "nonce-1",
        "request_url": "https://jobs.example.test/api/apply",
        "request_method": "POST",
        "response_url": "https://jobs.example.test/api/apply",
        "response_status": 201,
        "response_request_id": "req-1",
        "request_id": "req-1",
        "final_url": "https://jobs.example.test/confirmation",
        "navigation_url": "https://jobs.example.test/confirmation",
        "reference": "",
        "provider_success": True,
    }
    values.update(overrides)
    return ReceiptEvidence(**values)


def test_receipt_contract_rejects_preexisting_reference_and_confirmation_text() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(
        dom_had_reference=True,
        dom_confirmation_text_present=True,
        reference="ARG-1234",
        confirmation_text="Thank you for applying",
    )
    after = _evidence(
        dom_had_reference=True,
        dom_confirmation_text_present=True,
        reference="ARG-1234",
        confirmation_text="Thank you for applying",
        reference_seen_before_click=True,
    )
    assert receipt_is_correlated(before, after, bound_target="target-1", bound_intent={"id": "intent-1", "nonce": "nonce-1"}) is False


def test_receipt_contract_rejects_fake_dom_success_and_failed_response() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="", response_request_id="")
    fake_dom = _evidence(
        request_url="",
        request_method="",
        response_url="",
        response_status=None,
        response_request_id="",
        request_id="",
        provider_success=False,
    )
    failed = _evidence(response_status=500, provider_success=False)
    assert receipt_is_correlated(before, fake_dom, bound_target="target-1", bound_intent={"id": "intent-1", "nonce": "nonce-1"}) is False
    assert receipt_is_correlated(before, failed, bound_target="target-1", bound_intent={"id": "intent-1", "nonce": "nonce-1"}) is False


def test_receipt_contract_rejects_intermediate_navigation_and_unrelated_response() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence()
    intermediate = _evidence(
        final_url="https://jobs.example.test/step-2",
        navigation_url="https://jobs.example.test/step-2",
        navigation_kind="intermediate",
        reference="",
        dom_had_reference=False,
        provider_success=False,
    )
    unrelated = _evidence(
        response_url="https://jobs.example.test/api/other",
        request_url="https://jobs.example.test/api/other",
        provider_success=True,
    )
    assert receipt_is_correlated(before, intermediate, bound_target="target-1", bound_intent={"id": "intent-1", "nonce": "nonce-1"}) is False
    assert receipt_is_correlated(before, unrelated, bound_target="target-1", bound_intent={"id": "intent-1", "nonce": "nonce-1"}) is False


def test_receipt_contract_accepts_delayed_exact_correlated_provider_success() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="", response_request_id="")
    after = _evidence(
        dom_had_reference=True,
        dom_confirmation_text_present=True,
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="req-new",
        response_request_id="req-new",
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target=_strict_target(),
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is True


def test_receipt_contract_rejects_same_provider_unrelated_response_even_with_matching_ids() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence()
    after = _evidence(
        response_url="https://jobs.example.test/confirmation",
        request_id="request-redirect",
        response_request_id="request-redirect",
        dom_confirmation_text_present=True,
    )
    assert receipt_is_correlated(before, after, bound_target="target-1", bound_intent={"id": "intent-1", "nonce": "nonce-1"}) is False


def test_page_crash_or_timeout_after_click_is_not_a_safe_failure() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence()
    for uncertain in ("page_crashed", "timed_out"):
        after = _evidence(**{uncertain: True})
        assert receipt_is_correlated(before, after, bound_target="target-1", bound_intent={"id": "intent-1", "nonce": "nonce-1"}) is False


def test_dom_boolean_without_fresh_normalized_text_is_not_success() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(reference="", confirmation_text="")
    after = _evidence(
        reference="",
        confirmation_text="",
        dom_confirmation_text_present=True,
        dom_had_reference=True,
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target="target-1",
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def test_unknown_or_preexisting_baseline_is_rejected() -> None:
    from app.automation.receipts import receipt_is_correlated

    after = _evidence(dom_confirmation_text_present=True, confirmation_text="Application submitted")
    assert receipt_is_correlated(
        _evidence(baseline_captured=False),
        after,
        bound_target="target-1",
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
    assert receipt_is_correlated(
        _evidence(reference_seen_before_click=True),
        after,
        bound_target="target-1",
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def test_missing_or_conflicting_page_frame_root_control_binding_is_rejected() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence()
    target = {
        "target_fingerprint": "target-1",
        "control_fingerprint": "control-1",
        "page_id": "page-1",
        "frame_url": "https://jobs.example.test/apply",
        "root_selector": "#application",
        "control_selector": "#submit",
        "form_action": "https://jobs.example.test/api/apply",
        "method": "POST",
        "destination": "https://jobs.example.test/confirmation",
        "provider": "example",
    }
    after = _evidence()
    assert receipt_is_correlated(
        before,
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
    assert receipt_is_correlated(
        before,
        _evidence(root_selector="#other"),
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def test_structured_target_requires_exact_fingerprint_and_rejects_baseline_replacement() -> None:
    from app.automation.receipts import receipt_is_correlated

    target = {
        "target_fingerprint": "target-1",
        "control_fingerprint": "control-1",
        "page_id": "page-1",
        "frame_url": "https://jobs.example.test/apply",
        "root_selector": "#application",
        "control_selector": "#submit",
        "form_action": "https://jobs.example.test/api/apply",
        "method": "POST",
        "destination": "https://jobs.example.test/confirmation",
        "provider": "example",
    }
    before = _evidence(
        bound_target_fingerprint="target-1",
        request_id="",
        response_request_id="",
    )
    after = _evidence(
        bound_target_fingerprint="target-1",
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="fresh-request",
        response_request_id="fresh-request",
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is True
    assert receipt_is_correlated(
        before,
        _evidence(
            bound_target_fingerprint="other-control",
            reference="ARG-5678",
            confirmation_text="Application submitted",
            request_id="fresh-request",
            response_request_id="fresh-request",
        ),
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
    assert receipt_is_correlated(
        _evidence(
            bound_target_fingerprint="control-1",
            reference="ARG-OLD",
            request_id="",
            response_request_id="",
        ),
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def test_reused_request_id_and_unrelated_same_origin_response_are_rejected() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="old-request", response_request_id="old-request")
    assert receipt_is_correlated(
        before,
        _evidence(
            dom_confirmation_text_present=True,
            confirmation_text="Application submitted",
            request_id="old-request",
            response_request_id="old-request",
        ),
        bound_target="target-1",
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
    assert receipt_is_correlated(
        _evidence(),
        _evidence(
            dom_confirmation_text_present=True,
            confirmation_text="Application submitted",
            response_url="https://jobs.example.test/api/other",
        ),
        bound_target="target-1",
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def _strict_target(**overrides):
    target = {
        "target_fingerprint": "target-1",
        "control_fingerprint": "control-1",
        "page_id": "page-1",
        "frame_url": "https://jobs.example.test/apply",
        "root_selector": "#application",
        "control_selector": "#submit",
        "form_action": "https://jobs.example.test/api/apply",
        "method": "POST",
        "destination": "https://jobs.example.test/confirmation",
        "provider": "example",
    }
    target.update(overrides)
    return target


def test_receipt_rejects_scalar_or_incomplete_target_binding() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="", response_request_id="")
    after = _evidence(
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="fresh",
        response_request_id="fresh",
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target="target-1",
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
    incomplete = _strict_target(provider="")
    assert receipt_is_correlated(
        before,
        after,
        bound_target=incomplete,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def test_receipt_rejects_conflicting_target_and_intent_aliases() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(
        request_id="",
        response_request_id="",
        target_fingerprint="target-1",
        intent_id="intent-1",
    )
    after = _evidence(
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="fresh",
        response_request_id="fresh",
        target_fingerprint="other-target",
        intent_id="other-intent",
    )
    target = _strict_target()
    target["bound_target_fingerprint"] = "other-target"
    assert receipt_is_correlated(
        before,
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
    assert receipt_is_correlated(
        before,
        after,
        bound_target=_strict_target(),
        bound_intent={"id": "intent-1", "bound_intent_id": "other-intent", "nonce": "nonce-1"},
    ) is False


def test_receipt_requires_exact_2xx_and_explicit_final_navigation_target() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(
        target_url="https://jobs.example.test/apply",
        destination="https://jobs.example.test/apply",
        request_id="",
        response_request_id="",
    )
    redirected = _evidence(
        response_status=302,
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="redirect",
        response_request_id="redirect",
    )
    assert receipt_is_correlated(
        before,
        redirected,
        bound_target=_strict_target(),
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
        expected_final_url="https://jobs.example.test/confirmation",
    ) is False
    successful = _evidence(
        target_url="https://jobs.example.test/apply",
        destination="https://jobs.example.test/apply",
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="fresh",
        response_request_id="fresh",
    )
    app_page_target = _strict_target(destination="https://jobs.example.test/apply")
    assert receipt_is_correlated(
        before,
        successful,
        bound_target=app_page_target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
    assert receipt_is_correlated(
        before,
        successful,
        bound_target=app_page_target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
        expected_final_url="https://jobs.example.test/confirmation",
    ) is True


def test_explicit_final_url_allows_cross_origin_receipt_navigation_only_when_exact():
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(
        target_url="https://receipt.example.test/confirmation",
        destination="https://receipt.example.test/confirmation",
        request_id="",
        response_request_id="",
    )
    after = _evidence(
        final_url="https://receipt.example.test/confirmation",
        navigation_url="https://receipt.example.test/confirmation",
        target_url="https://receipt.example.test/confirmation",
        destination="https://receipt.example.test/confirmation",
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="fresh",
        response_request_id="fresh",
    )
    target = _strict_target(destination="https://receipt.example.test/confirmation")
    assert receipt_is_correlated(
        before,
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
        expected_final_url="https://receipt.example.test:443/confirmation",
    ) is True
    assert receipt_is_correlated(
        before,
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
        expected_final_url="https://other.example.test/confirmation",
    ) is False


def test_receipt_rejects_conflicting_fresh_reference_aliases() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="", response_request_id="")
    after = _evidence(
        reference="ARG-5678",
        provider_reference="ARG-9999",
        confirmation_text="Application submitted",
        request_id="fresh-reference",
        response_request_id="fresh-reference",
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target=_strict_target(),
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def test_receipt_rejects_conflicting_fresh_confirmation_aliases() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="", response_request_id="")
    after = _evidence(
        confirmation_text="Application submitted",
        dom_confirmation_text="Thank you for applying",
        request_id="fresh-confirmation",
        response_request_id="fresh-confirmation",
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target=_strict_target(),
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False


def test_receipt_accepts_normalized_equal_reference_and_confirmation_aliases() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="", response_request_id="")
    after = _evidence(
        reference=" ARG-5678 ",
        provider_reference="arg-5678",
        confirmation_text=" Application submitted ",
        dom_confirmation_text="application   submitted",
        request_id="fresh-normalized",
        response_request_id="fresh-normalized",
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target=_strict_target(),
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is True


def test_explicit_final_url_requires_fresh_navigation_evidence() -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(
        target_url="https://jobs.example.test/apply",
        destination="https://jobs.example.test/apply",
        request_id="",
        response_request_id="",
        final_url="",
        navigation_url="",
    )
    after = _evidence(
        target_url="https://jobs.example.test/apply",
        destination="https://jobs.example.test/apply",
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id="fresh-no-navigation",
        response_request_id="fresh-no-navigation",
        final_url="",
        navigation_url="",
    )
    target = _strict_target(destination="https://jobs.example.test/apply")
    assert receipt_is_correlated(
        before,
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
        expected_final_url="https://jobs.example.test/confirmation",
    ) is False
    assert receipt_is_correlated(
        before,
        after,
        bound_target=target,
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is True


@pytest.mark.parametrize(
    "invalid_id",
    [True, 1, b"request-id", None, " ", "\t", "x" * 257, "request id"],
)
def test_receipt_request_ids_require_bounded_nonempty_strings(invalid_id) -> None:
    from app.automation.receipts import receipt_is_correlated

    before = _evidence(request_id="", response_request_id="")
    after = _evidence(
        reference="ARG-5678",
        confirmation_text="Application submitted",
        request_id=invalid_id,
        response_request_id=invalid_id,
    )
    assert receipt_is_correlated(
        before,
        after,
        bound_target=_strict_target(),
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
    ) is False
