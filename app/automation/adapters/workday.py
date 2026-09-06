from __future__ import annotations

import logging
import re
from typing import Any, Mapping
from playwright.sync_api import Page

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.verification import semantic_value_matches
from app.automation.targets import FormHandle, SubmissionTarget, trusted_provider_for_url
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion

logger = logging.getLogger(__name__)

# Selectors that mean "advance one step" — never final submission.
_STEP_ADVANCE_IDS = (
    "submitNextButton",
    "nextButton",
    "saveAndContinue",
    "MDxQ4",  # legacy Next id on some tenants
)

# The only data-automation-id that constitutes final submission.
_FINAL_SUBMIT_ID = "submitButton"


class WorkdayAdapter(GenericAdapter):
    """Workday hosted-careers adapter with an explicit per-step machine.

    Flow (forensic root cause 3): landing -> (cookie banner) -> Apply ->
    Apply Manually -> N form steps -> final review/submit.  Each step is
    inspected, filled and validated BEFORE advancing exactly once; the final
    submission control is identified strictly by
    ``data-automation-id="submitButton"`` so Next/Save/search/login controls
    can never be mistaken for it.
    """

    name = "workday"

    _APPLY_SELECTORS = (
        '[data-automation-id="adventureButton"]',
        'a[data-automation-id="applyButton"]',
    )
    _APPLY_MANUALLY_SELECTORS = (
        '[data-automation-id="applyManually"]',
        '[data-automation-id="applyManuallyButton"]',
    )

    @classmethod
    def matches(cls, url: str, html: str = "") -> bool:
        del html  # DOM labels are mutable and cannot establish provider trust.
        return trusted_provider_for_url(url) == "workday"

    # ------------------------------------------------------------- flow helpers

    @staticmethod
    def _first_visible(page: Page, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(locator.count()):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:  # noqa: BLE001 - detached/malformed selector
                continue
        return None

    def _dismiss_cookie_banner(self, page: Page) -> None:
        banner = self._first_visible(
            page,
            (
                'button:has-text("Accept Cookies")',
                'button:has-text("Accept all cookies")',
                '[data-automation-id="cookieBannerButton"]',
            ),
        )
        if banner is not None:
            try:
                banner.click(timeout=4000)
                page.wait_for_timeout(1500)
                logger.info("workday: cookie banner accepted")
            except Exception:  # noqa: BLE001 - banner is best-effort
                pass

    def _on_job_landing(self, page: Page) -> bool:
        return self._first_visible(page, self._APPLY_SELECTORS) is not None

    def enter_application_flow(self, page: Page) -> bool:
        """Drive landing -> sign-in dialog -> manual-apply entry.

        Returns True when a multi-step application form is present
        afterwards; False when the flow could not be completed.
        """

        # An email input is the Workday sign-in wall, not an application form.
        # It is intentionally absent from this entry predicate: identity must
        # be proven by the runner before this method is allowed to click Apply.
        if not self._first_visible(page, ('.wd-step, [data-automation-id="submitButton"]',)):
            if not self._on_job_landing(page) and not self._first_visible(page, self._APPLY_MANUALLY_SELECTORS):
                return False

        self._dismiss_cookie_banner(page)

        apply_control = self._first_visible(page, self._APPLY_SELECTORS)
        if apply_control is not None:
            apply_control.click(timeout=8000)
            try:
                # The Workday shell is a SPA; network-idle is advisory and
                # must not hold the owner thread while the DOM entry signal is
                # already available.
                page.wait_for_load_state("networkidle", timeout=2_000)
            except Exception:  # noqa: BLE001 - SPA may never settle
                pass
            page.wait_for_timeout(1_000)
            logger.info("workday: clicked Apply")

        manually = None
        for _attempt in range(10):
            manually = self._first_visible(page, self._APPLY_MANUALLY_SELECTORS)
            if manually is not None:
                break
            if self._first_visible(page, (".wd-step", '[data-automation-id="submitButton"]')):
                logger.info("workday: direct-form tenant detected")
                return True
            page.wait_for_timeout(1_000)
        if manually is not None:
            manually.click(timeout=8000)
            for _ in range(15):
                page.wait_for_timeout(1_500)
                if self._first_visible(page, (".wd-step", '[data-automation-id="submitButton"]')):
                    logger.info("workday: entered manual application")
                    return True
            logger.warning("workday: form did not appear after Apply Manually")
            return False
        return bool(self._first_visible(page, (".wd-step",)))

    # ------------------------------------------------------- step machine

    @staticmethod
    def is_final_submit_visible(page: Page) -> bool:
        """Whether THE proven final submission control is currently shown.

        Only ``[data-automation-id="submitButton"]`` qualifies.  Buttons
        whose ids end in NextButton / saveAndContinue etc. are step
        advances by definition and can never be final submits.
        """

        submit = page.locator(f'[data-automation-id="{_FINAL_SUBMIT_ID}"]')
        count = 0
        for index in range(submit.count()):
            candidate = submit.nth(index)
            try:
                if candidate.is_visible():
                    count += 1
            except Exception:  # noqa: BLE001
                continue
        return count == 1

    def advance_one_step(self, page: Page, *, max_wait_steps: int = 10) -> bool:
        """Advance exactly one step after the current step validates.

        Clicks the step's advance control; the portal either renders the next
        step (True) or re-renders validation errors (False — caller must not
        proceed).  Never clicks anything that is not a known advance control.
        """

        if self.is_final_submit_visible(page):
            # A final control is a review boundary, never an implicit Next.
            return False

        advance = None
        for selector in (_STEP_ADVANCE_IDS,):
            for automation_id in selector:
                candidate = self._first_visible(
                    page, (f'[data-automation-id="{automation_id}"]',)
                )
                if candidate is not None:
                    advance = candidate
                    break
            if advance is not None:
                break

        if advance is None:
            # Fall back to a plain visible "Next" text button inside the step.
            advance = self._first_visible(page, ('.wd-step button:has-text("Next")',))
        if advance is None:
            raise RuntimeError("No visible step-advance control found")

        before_step = self.current_step_marker(page)
        advance.click(timeout=10_000)
        try:
            # Step readiness is proven by the immutable DOM marker below;
            # network-idle is only a short hydration hint for SPA tenants.
            page.wait_for_load_state("networkidle", timeout=2_000)
        except Exception:  # noqa: BLE001
            pass

        # Delayed rendering: poll until the DOM marker changes or budget out.
        for _ in range(max_wait_steps):
            after_step = self.current_step_marker(page)
            if after_step != before_step:
                page.wait_for_timeout(500)  # let conditional fields hydrate
                return True
            page.wait_for_timeout(1_000)
        return False  # validation error or stalled render: do NOT proceed

    # Compatibility name retained for callers from the pre-navigator runner.
    def advance_if_valid(self, page: Page, *, max_wait_steps: int = 10) -> bool:
        return self.advance_one_step(page, max_wait_steps=max_wait_steps)

    @staticmethod
    def current_step_marker(page: Page) -> str:
        try:
            return page.evaluate(
                """() => {
                  const step = document.querySelector('.wd-step');
                  if (!step) return '';
                  const controls = Array.from(step.querySelectorAll('input,select,textarea'))
                    .map(control => [control.name, control.id, control.type || control.tagName,
                      control.value || ''].join(':')).join('|');
                  const action = step.querySelector('form')?.getAttribute('action') || '';
                  // Exclude values and validation copy: a browser-side error
                  // must not masquerade as a completed step transition.
                  const shape = Array.from(step.querySelectorAll('input,select,textarea,button'))
                    .map(control => [control.tagName, control.name, control.id,
                      control.type || '', control.getAttribute('data-automation-id') || ''].join(':')).join('|');
                  return [step.id || '', step.className || '', action, shape].join('::');
                }"""
            )
        except Exception:  # noqa: BLE001
            return ""

    _current_step_marker = current_step_marker

    @staticmethod
    def verify_destination_identity(
        page: Page,
        *,
        expected_employer: str,
        expected_role: str,
    ) -> bool:
        """Prove employer and role on the landing page before Apply clicks.

        This deliberately reads visible/explicit identity evidence only.  A
        sign-in email field is never consulted and cannot make this return
        true.
        """

        try:
            identity = page.evaluate(
                """() => ({
                  employer: document.querySelector('[data-employer], [data-company]')?.getAttribute('data-employer')
                    || document.querySelector('[data-employer], [data-company]')?.getAttribute('data-company') || '',
                  role: document.querySelector('[data-role], [data-job-title]')?.getAttribute('data-role')
                    || document.querySelector('[data-role], [data-job-title]')?.getAttribute('data-job-title') || '',
                  visible: ((document.title || '') + ' ' + (document.body?.innerText || '')).slice(0, 20000)
                })"""
            ) or {}
        except Exception:  # noqa: BLE001 - identity failure is fail-closed
            return False

        def phrase(expected: str, *candidates: str) -> bool:
            tokens = re.sub(r"[^a-z0-9]+", " ", expected.casefold()).split()
            if not tokens:
                return True
            for candidate in candidates:
                actual = re.sub(r"[^a-z0-9]+", " ", str(candidate).casefold()).split()
                width = len(tokens)
                if any(actual[i : i + width] == tokens for i in range(len(actual) - width + 1)):
                    return True
            return False

        return phrase(
            expected_employer,
            str(identity.get("employer", "")),
            str(identity.get("visible", "")),
        ) and phrase(
            expected_role,
            str(identity.get("role", "")),
            str(identity.get("visible", "")),
        )

    @staticmethod
    def detect_human_boundary(page: Page) -> tuple[bool, str]:
        """Detect CAPTCHA/MFA across the adopted page and all frames."""

        script = """() => {
          const visible = node => {
            if (!node) return false;
            const style = getComputedStyle(node), rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
          };
          const node = document.querySelector('iframe[src*="captcha" i], iframe[title*="captcha" i], [data-captcha], [data-sitekey], .g-recaptcha, .h-captcha, [id*="captcha" i], [class*="captcha" i]');
          if (visible(node)) return {found: true, reason: 'captcha_detected'};
          const text = (document.body?.innerText || '').toLowerCase();
          for (const [phrase, reason] of [['verify you are human','human_verification'],
            ['complete the captcha','captcha_detected'], ['multi-factor authentication','mfa_required'],
            ['multifactor authentication','mfa_required'], ['two-factor authentication','mfa_required'],
            ['two factor authentication','mfa_required'], ['one-time passcode','mfa_required'],
            ['one time passcode','mfa_required'], ['one-time password','mfa_required'],
            ['verification code','mfa_required'], ['security code','mfa_required'],
            ['authenticator app','mfa_required'], ['enter the code we sent','mfa_required'],
            ['use your authentication app','mfa_required'], ['enter the otp','mfa_required'],
            ['enter your otp','mfa_required']]) {
            if (text.includes(phrase)) return {found: true, reason};
          }
          return {found: false, reason: ''};
        }"""
        targets = []
        try:
            frames = page.frames
            targets = list(frames() if callable(frames) else frames or [])
        except Exception:  # noqa: BLE001
            targets = [page]
        if not targets:
            targets = [page]
        for target in targets:
            try:
                result = target.evaluate(script) or {}
                if result.get("found"):
                    return True, str(result.get("reason") or "captcha_detected")
            except Exception:  # noqa: BLE001 - a missing frame is not proof
                continue
        return False, ""

    def _fallback_step_inspection(self, page: Page) -> tuple[list[InspectedField], dict[str, Any]]:
        """Inspect an unbound synthetic Workday step for unit/fixture use.

        The generic root scanner remains authoritative for automation-ready
        real targets. This bounded fallback only makes the adapter's explicit
        ``.wd-step`` state machine observable when no resolution object is
        supplied (for example, pure adapter unit tests).
        """

        raw = page.evaluate(
            """() => {
              const root = document.querySelector('.wd-step');
              if (!root) return {fields: []};
              const visible = node => { const s = getComputedStyle(node); return s.display !== 'none' && s.visibility !== 'hidden' && node.getClientRects().length > 0; };
              const label = node => {
                if (node.id) { const l = document.querySelector(`label[for="${CSS.escape(node.id)}"]`); if (l) return l.innerText.trim(); }
                const wrapped = node.closest('label'); if (wrapped) return wrapped.innerText.replace(node.value || '', '').trim();
                return node.name || node.id || node.type || node.tagName;
              };
              const fields = [];
              root.querySelectorAll('input,select,textarea').forEach(node => {
                const type = (node.type || node.tagName).toLowerCase();
                if (['hidden','submit','button','reset'].includes(type) || !visible(node)) return;
                const selector = node.id ? '#' + CSS.escape(node.id) : `.wd-step [name="${(node.name || '').replaceAll('"','\\\\"')}"]`;
                fields.push({selector, label: label(node), field_type: type, name: node.name || '', required: !!node.required,
                  options: node.tagName.toLowerCase() === 'select' ? Array.from(node.options).map(o => o.textContent.trim()) : [],
                  control_type: type, value_attribute: node.value || ''});
              });
              return {fields};
            }"""
        ) or {}
        fields = [
            InspectedField(
                selector=str(item["selector"]),
                question=FormQuestion(
                    label=str(item.get("label", "")),
                    field_type=str(item.get("field_type", "text")),
                    name=str(item.get("name", "")),
                    required=bool(item.get("required")),
                    options=tuple(str(option) for option in item.get("options", [])),
                ),
                control_type=str(item.get("control_type", "text")),
                value_attribute=str(item.get("value_attribute", "")),
            )
            for item in raw.get("fields", [])
        ]
        return fields, {
            "root_found": bool(fields),
            "automation_ready": False,
            "binding_verified": False,
            "step_name": self.current_step_marker(page),
            "submit_present": self.is_final_submit_visible(page),
            "controls_enumerated": True,
        }

    def wait_for_step_ready(self, page: Page, *, timeout_ms: int = 12_000) -> bool:
        """Wait until delayed/conditional controls have hydrated."""

        deadline = timeout_ms
        waited = 0
        while waited < deadline:
            if self._first_visible(page, (".wd-step input, .wd-step select, .wd-step textarea",)) or self.is_final_submit_visible(page):
                return True
            page.wait_for_timeout(500)
            waited += 500
        return False

    # ------------------------------------------------------- generic overrides

    def inspect_with_evidence(self, page: Page) -> tuple[list[InspectedField], dict]:
        """Enter the flow lazily, then scope inspection to the current step."""

        if not self._first_visible(page, (".wd-step", '[data-automation-id="submitButton"]')):
            self.enter_application_flow(page)
        self.wait_for_step_ready(page)
        fields, evidence = super().inspect_with_evidence(page)
        if not fields and not evidence.get("page_root_found") and self._resolution is None:
            fields, evidence = self._fallback_step_inspection(page)
        evidence["step_name"] = str(self._current_step_marker(page))
        evidence["final_submit_present"] = self.is_final_submit_visible(page)
        # A Workday review step legitimately carries no inputs: explicit
        # step-level proof per the readiness contract.
        if not fields and evidence.get("submit_present"):
            evidence["step_requires_no_fields"] = True
        return fields, evidence

    def verify_step(
        self,
        page: Page,
        fields: list[InspectedField],
        expected_values: Mapping[str, str],
    ) -> bool:
        """Verify actual DOM values, browser validity and visible errors."""

        for field in fields:
            expected = expected_values.get(field.question.name)
            if expected is None:
                continue
            locator = page.locator(field.selector)
            if not locator.count():
                return False
            control = field.control_type.casefold()
            try:
                if control in {"checkbox", "radio"}:
                    if control == "checkbox" and field.question.option_label:
                        if not semantic_value_matches(
                            expected,
                            field.question.option_label,
                            label=field.question.label,
                            control_type=control,
                            alternatives=(field.value_attribute,),
                        ):
                            continue
                    actual = any(
                        locator.nth(index).is_checked()
                        and (
                            (locator.nth(index).get_attribute("value") or "").casefold() == str(expected).casefold()
                            or str(expected).casefold() in (locator.nth(index).inner_text() or "").casefold()
                        )
                        for index in range(locator.count())
                    )
                elif control == "file":
                    actual = True  # Playwright intentionally hides local paths.
                elif control == "select":
                    selected = locator.first.evaluate(
                        """element => {
                          const option = element.options?.[element.selectedIndex];
                          return {
                            value: element.value || '',
                            label: (option?.textContent || '').replace(/\\s+/g, ' ').trim(),
                          };
                        }"""
                    )
                    actual = semantic_value_matches(
                        expected,
                        selected.get("value", ""),
                        label=field.question.label,
                        control_type=control,
                        alternatives=(selected.get("label", ""),),
                    )
                else:
                    actual = semantic_value_matches(
                        expected,
                        locator.first.input_value(),
                        label=field.question.label,
                        control_type=control,
                    )
                if not actual:
                    return False
                valid = locator.first.evaluate("element => !element.willValidate || element.checkValidity()")
                if not valid:
                    return False
            except Exception:  # noqa: BLE001 - stale/dynamic controls fail closed
                return False
        try:
            errors = page.locator(
                '[aria-invalid="true"], .error:visible, [role="alert"]:visible, .validation-error:visible'
            )
            if any(errors.nth(index).is_visible() for index in range(errors.count())):
                return False
        except Exception:  # noqa: BLE001
            return False
        return True

    def submission_target(self, page: Page, handle: FormHandle) -> SubmissionTarget | None:
        if not self.is_final_submit_visible(page):
            return None
        return super().submission_target(page, handle)

    def inspect(self, page: Page) -> list[InspectedField]:
        fields, _evidence = self.inspect_with_evidence(page)
        return fields

    def submit(self, page: Page) -> None:
        """Walk remaining steps (validated advances only), then click THE
        single final submit control.  Refuse anything ambiguous."""

        for _step in range(12):
            if self.is_final_submit_visible(page):
                break
            try:
                if not self.advance_one_step(page):
                    continue  # controls may still be hydrating; poll within budget
            except RuntimeError as exc:
                # No advance control AND no final submit anywhere: fail closed
                # with the canonical no-submission-control error.
                raise RuntimeError("No visible submission control found") from exc
        else:
            raise RuntimeError("No visible submission control found")

        submit = page.locator(f'[data-automation-id="{_FINAL_SUBMIT_ID}"]')
        candidates = []
        for index in range(submit.count()):
            candidate = submit.nth(index)
            try:
                if candidate.is_visible() and candidate.is_enabled():
                    candidates.append(candidate)
            except Exception:  # noqa: BLE001
                continue
        if not candidates:
            raise RuntimeError("No visible submission control found")
        if len(candidates) > 1:
            raise RuntimeError("Ambiguous visible submission controls found")
        candidates[0].click(timeout=10_000)
