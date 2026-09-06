from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.verification import semantic_value_matches
from app.automation.targets import FormHandle, SubmissionTarget, trusted_provider_for_url


_STANDARD_ANSWER_ALIASES = {
    "work_authorisation": {
        "authorized to work": "yes",
        "legally authorized to work": "yes",
        "i am authorized to work": "yes",
        "i am legally authorized to work": "yes",
        "right to work": "yes",
        "i have the right to work": "yes",
        "not authorized to work": "no",
        "i am not authorized to work": "no",
        "i am not legally authorized to work": "no",
        "no right to work": "no",  # synthetic fixture vocabulary only
        "i do not have the right to work": "no",  # synthetic fixture vocabulary only
    },
    "sponsorship": {
        "require sponsorship": "yes",
        "requires sponsorship": "yes",
        "i require sponsorship": "yes",
        "i will require sponsorship": "yes",
        "sponsorship required": "yes",
        "do not require sponsorship": "no",  # synthetic fixture vocabulary only
        "does not require sponsorship": "no",  # synthetic fixture vocabulary only
        "i do not require sponsorship": "no",  # synthetic fixture vocabulary only
        "i will not require sponsorship": "no",  # synthetic fixture vocabulary only
        "no sponsorship required": "no",  # synthetic fixture vocabulary only
        "without sponsorship": "no",  # synthetic fixture vocabulary only
    },
}


class ComboboxResolutionError(RuntimeError):
    """Fail-closed reason for a typeahead value that cannot be committed."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        # ``code`` keeps the exception compatible with the runner's other
        # safety errors while the public reason name remains explicit.
        self.code = reason_code


# Read only the unique control's rendered and submitted state. Never mutate
# provider labels/hidden inputs to simulate a framework selection or rollback.
_COMBOBOX_STATE_SCRIPT = r"""(element, includeNodes = false) => {
  let scope = element;
  for (let node = element.parentElement, depth = 0; node && depth < 8;
       node = node.parentElement, depth++) {
    if (['FORM', 'BODY', 'HTML'].includes(node.tagName) ||
        node.querySelectorAll('[role="combobox"]').length !== 1 ||
        node.querySelectorAll('.select__single-value').length > 1) break;
    scope = node;
  }
  return {
    ...(includeNodes ? {element,
      selectedNode: scope.querySelector('.select__single-value'),
      submittedNodes: Array.from(scope.querySelectorAll('input[type="hidden"][name]'))} : {}),
    selected: (scope.querySelector('.select__single-value')?.textContent || '').replace(/\s+/g, ' ').trim(),
    submitted: Array.from(scope.querySelectorAll('input[type="hidden"][name]'))
      .map(node => [node.name, node.value, node.disabled])
  };
}"""


_COMBOBOX_OPTIONS_SCRIPT = r"""(element, request) => {
  const document = element.ownerDocument;
  const scope = element.closest('form') || document.body || document.documentElement;
  const textOf = node => node
    ? (node.textContent || '').replace(/\s+/g, ' ').trim()
    : '';
  const accessibleText = node => {
    if (!node) return '';
    const ariaLabel = (node.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (ariaLabel) return ariaLabel;
    const labelledBy = (node.getAttribute('aria-labelledby') || '').trim();
    if (labelledBy) {
      const joined = labelledBy.split(/\s+/)
        .map(id => textOf(document.getElementById(id)))
        .filter(Boolean)
        .join(' ');
      if (joined) return joined;
    }
    return textOf(node);
  };
  const visible = node => {
    if (!node || node.hasAttribute('hidden') || node.getAttribute('aria-hidden') === 'true') {
      return false;
    }
    const style = node.ownerDocument.defaultView.getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden'
      && node.getClientRects().length > 0;
  };
  // Collect candidate listboxes, preferring the element's own aria-controls/aria-owns.
  const listboxes = [];
  const add = candidate => {
    if (!candidate) return;
    let listbox = candidate.matches?.('[role="listbox"]')
      ? candidate
      : candidate.closest?.('[role="listbox"]');
    if (!listbox) {
      const descendants = candidate.querySelectorAll?.('[role="listbox"]') || [];
      if (descendants.length === 1) listbox = descendants[0];
    }
    if (!listbox && candidate.querySelectorAll?.('[role="option"]').length) {
      listbox = candidate;
    }
    if (listbox && !listboxes.includes(listbox)) listboxes.push(listbox);
  };
  // FIRST: only the element's own aria-controls/aria-owns listbox.
  const explicitIds = [];
  for (const attribute of ['aria-controls', 'aria-owns']) {
    (element.getAttribute(attribute) || '').trim().split(/\s+/).filter(Boolean)
      .forEach(id => explicitIds.push(id));
  }
  if (explicitIds.length) {
    for (const id of explicitIds) {
      const lb = document.getElementById(id);
      if (lb) add(lb);
    }
    const options = listboxes.flatMap(lb =>
      Array.from(lb.querySelectorAll('[role="option"]'))
    ).filter(node => visible(node) && accessibleText(node));
    if (request && request.returnNode) {
      return options[Number(request.index)] || null;
    }
    return options.map(accessibleText);
  }
  // FALLBACK: when aria-controls/aria-owns is absent, use the original
  // multi-strategy search.  This handles non-react-select comboboxes.
  const activeId = (element.getAttribute('aria-activedescendant') || '').trim();
  if (activeId) add(document.getElementById(activeId));
  if (!listboxes.length) {
    const associationIds = new Set([
      element.id,
      ...(element.getAttribute('aria-labelledby') || '').trim().split(/\s+/),
    ].filter(Boolean));
    const labelled = Array.from(document.querySelectorAll('[role="listbox"]')).filter(lb =>
      (lb.getAttribute('aria-labelledby') || '').trim().split(/\s+/)
        .some(id => associationIds.has(id))
    );
    if (labelled.length === 1) listboxes.push(labelled[0]);
  }
  if (!listboxes.length) {
    let ancestor = element.parentElement;
    for (let depth = 0; ancestor && depth < 6; depth += 1, ancestor = ancestor.parentElement) {
      const nearby = Array.from(ancestor.querySelectorAll('[role="listbox"]'));
      if (nearby.length === 1) {
        listboxes.push(nearby[0]);
        break;
      }
      if (!nearby.length && ancestor.querySelectorAll('[role="option"]').length) {
        listboxes.push(ancestor);
        break;
      }
      if (ancestor === scope) break;
    }
  }
  if (!listboxes.length) {
    const scoped = Array.from(scope.querySelectorAll('[role="listbox"]'));
    if (scoped.length === 1) listboxes.push(scoped[0]);
  }
  if (!listboxes.length) {
    const globalVisible = Array.from(document.querySelectorAll('[role="listbox"]'))
      .filter(visible);
    if (globalVisible.length === 1) listboxes.push(globalVisible[0]);
  }
  const options = listboxes.flatMap(lb =>
    Array.from(lb.querySelectorAll('[role="option"]'))
  ).filter(node => visible(node) && accessibleText(node));
  if (request && request.returnNode) {
    return options[Number(request.index)] || null;
  }
  return options.map(accessibleText);
}
"""


_FILE_ATTACHMENT_READBACK_SCRIPT = r"""(element, expected) => {
  const normalise = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = node => {
    if (!node) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width > 0 && rect.height > 0;
  };
  const expectedName = normalise(expected);
  if (!expectedName) return false;
  // React may clear or replace the file input after the provider-side upload
  // succeeds.  If a local FileList is still present it must agree, but the
  // provider-visible filename is the authoritative readback after rerender.
  if (element.files?.length && normalise(element.files[0].name) !== expectedName) {
    return false;
  }
  const visibleText = node => {
    if (!visible(node)) return '';
    const tag = (node.tagName || '').toLowerCase();
    // File input values are set by Playwright, not visible to the user.
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return '';
    return normalise(
      node.textContent
      || node.getAttribute('data-file-name')
      || node.getAttribute('data-uploaded-file-name')
      || node.getAttribute('aria-label')
      || ''
    );
  };
  // Specific CSS class and data-attribute search first.
  let node = element;
  for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
    const candidates = node.querySelectorAll?.(
      '.file-upload__filename, [data-automation-id*="filename" i], '
      + '[data-file-name], [data-uploaded-file-name], '
      + '.file-upload-success, [class*="uploaded"], [class*="filename"]'
    ) || [];
    for (const candidate of candidates) {
      if (!visible(candidate)) continue;
      const descendants = [candidate, ...candidate.querySelectorAll('*')];
      for (const descendant of descendants) {
        if (visibleText(descendant) === expectedName) return true;
      }
    }
  }
  // Fallback: search all visible rendered text within the upload container
  // for the expected filename.  Greenhouse re-renders the upload area and
  // the exact class/attribute may not match our selector list.
  let container = element;
  for (let depth = 0; container && depth < 10; depth += 1, container = container.parentElement) {
    if (!container.querySelectorAll) continue;
    const allElements = container.querySelectorAll('*');
    for (const el of allElements) {
      const tag = (el.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') continue;
      if (!visible(el) || el.children?.length) continue;
      const text = visibleText(el);
      if (text && text.includes(expectedName)) return true;
    }
  }
  return false;
}
"""


class _EmbeddedProviderAdapter(GenericAdapter):
    """Provider surface that may be hosted in a popup or iframe.

    The active Playwright scope is always the exact page/frame that produced
    the verified :class:`FormHandle`; callers must use ``current_scope`` for
    fill and submission operations. A parent page or arbitrary frame is never
    substituted at click time.
    """

    _active_scope: Any | None = None

    def _candidate_scopes(self, page: Any) -> list[Any]:
        scopes: list[Any] = []
        pages: list[Any] = [page]
        try:
            context = page.context
            pages = list(context.pages)
        except Exception:  # noqa: BLE001 - a page without context is valid
            pass
        for candidate_page in pages:
            if not any(existing is candidate_page for existing in scopes):
                scopes.append(candidate_page)
            try:
                frames = candidate_page.frames
                frames = frames() if callable(frames) else frames
                for frame in list(frames or []):
                    if not any(existing is frame for existing in scopes):
                        scopes.append(frame)
            except Exception:  # noqa: BLE001 - popup/frame can close mid-scan
                continue
        return scopes

    def enter_application_flow(self, page: Any) -> bool:
        """Open an Apply popup when the verified entry is a job shell."""

        for scope in self._candidate_scopes(page):
            try:
                fields, evidence = super().inspect_with_evidence(scope)
            except Exception:  # noqa: BLE001 - wait for a newly opened frame
                continue
            if evidence.get("root_found"):
                self._active_scope = scope
                return True
            # Keep an explicitly located but not yet automation-ready root
            # available for a read-only target/egress diagnosis. The runner
            # will refuse mutation when binding evidence is absent; treating
            # this as an unresolved entry here would hide an actionable
            # submission-target guard behind ``application_entry_unresolved``.
            if evidence.get("page_root_found"):
                self._active_scope = scope
                return True
        # Entry controls are non-data-bearing navigation. They are reachable
        # only after the runner has verified destination identity and the
        # control is inside provider/form evidence. A page-wide text selector
        # would mistake job-alert/newsletter controls for an application entry.
        try:
            control_index = self._application_entry_control_index(page)
            if control_index is None:
                return False
            launch = page.locator(
                'a, button, [role="button"], input[type="button"], input[type="submit"]'
            ).nth(control_index)
            if launch.count() and launch.is_visible():
                launch.first.click(timeout=8_000)
                page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 - a missing entry control is review-only
            return False
        for _ in range(20):
            for scope in self._candidate_scopes(page):
                try:
                    _fields, evidence = super().inspect_with_evidence(scope)
                except Exception:  # noqa: BLE001
                    continue
                if evidence.get("root_found"):
                    self._active_scope = scope
                    return True
            try:
                page.wait_for_timeout(250)
            except Exception:  # noqa: BLE001
                break
        return False

    def _application_entry_control_index(self, page: Any) -> int | None:
        """Find a provider/form-bound Apply control, never a page-wide CTA."""

        resolution = getattr(self, "_resolution", None)
        trusted = trusted_provider_for_url(str(getattr(page, "url", "") or ""))
        resolution_provider = str(getattr(resolution, "provider", "") or "").casefold()
        resolution_verified = bool(
            resolution is not None
            and resolution_provider == self.name
            and (
                getattr(resolution, "verified_for_automation", False)
                or getattr(resolution, "identity_verified", False)
            )
        )
        if trusted != self.name and not (resolution_verified and resolution_provider == self.name):
            return None
        try:
            index = page.evaluate(
                """provider => {
                  const visible = node => {
                    if (!node) return false;
                    const style = getComputedStyle(node), rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                      && rect.width > 0 && rect.height > 0
                      && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
                  };
                  const blocked = /job[- ]?alert|newsletter|subscribe|alert me|talent community/i;
                  const controls = Array.from(document.querySelectorAll(
                    'a, button, [role="button"], input[type="button"], input[type="submit"]'
                  ));
                  return controls.findIndex(node => {
                    if (!visible(node)) return false;
                    const label = [
                      node.textContent || '', node.value || '', node.getAttribute('aria-label') || '',
                      node.getAttribute('data-automation-id') || ''
                    ].join(' ').trim();
                    const href = node.getAttribute('href') || '';
                    if (!/\\bapply\\b/i.test(label) && !/(?:^|\\/)apply(?:\\/|[?#]|$)/i.test(href)) return false;
                    let context = node;
                    let providerEvidence = false;
                    let formEvidence = false;
                    let blockedContext = false;
                    for (let depth = 0; context && depth < 6; depth += 1, context = context.parentElement) {
                      const descriptor = [
                        context.id || '', context.className || '', context.getAttribute('aria-label') || '',
                        context.getAttribute('data-ats') || '', context.getAttribute('data-provider') || '',
                        context.getAttribute('data-role') || '', context.getAttribute('data-job-title') || '',
                        context.getAttribute('data-employer') || '', context.getAttribute('data-company') || '',
                        context.getAttribute('data-requisition') || '', context.getAttribute('data-posting-id') || '',
                        context.getAttribute('action') || '', context.innerText || ''
                      ].join(' ');
                      if (blocked.test(descriptor)) blockedContext = true;
                      if ([context.getAttribute('data-ats'), context.getAttribute('data-provider')]
                        .some(value => String(value || '').trim().toLowerCase() === provider)) {
                        providerEvidence = true;
                      }
                      if (context.tagName === 'FORM') {
                        const action = context.getAttribute('action') || '';
                        const hasIdentity = context.querySelector(
                          'input[name*="name" i], input[type="email"], input[type="file"], textarea'
                        );
                        formEvidence = providerEvidence || /(?:^|\\/)(?:apply|application|jobs?|postings?)(?:\\/|$)/i.test(action)
                          || !!hasIdentity;
                      }
                    }
                    return !blockedContext && (providerEvidence || formEvidence);
                  });
                }""",
                self.name,
            )
            return int(index) if isinstance(index, int) and index >= 0 else None
        except Exception:  # noqa: BLE001 - malformed/detached DOM fails closed
            return None

    def current_scope(self, page: Any) -> Any:
        return self._active_scope or page

    def inspect_with_evidence(self, page: Any):
        self._active_scope = None
        for scope in self._candidate_scopes(page):
            try:
                fields, evidence = super().inspect_with_evidence(scope)
            except Exception:  # noqa: BLE001
                continue
            if evidence.get("root_found") or evidence.get("page_root_found"):
                self._active_scope = scope
                return fields, evidence
        # Return the primary scope's evidence for an honest review-only result.
        return super().inspect_with_evidence(page)

    def detect_human_boundary(self, page: Any) -> tuple[bool, str]:
        script = """() => {
          const visible = node => {
            if (!node) return false;
            const style = getComputedStyle(node), rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
          };
          const node = Array.from(document.querySelectorAll(
            'iframe[src*="captcha" i], iframe[title*="captcha" i], [data-sitekey], .g-recaptcha, .h-captcha, [id="captcha" i], [id^="captcha-" i], [class~="captcha"]'
          )).find(candidate => {
            if (!visible(candidate) || candidate.closest('.grecaptcha-badge, .grecaptcha-logo')) {
              return false;
            }
            if (candidate.tagName.toLowerCase() !== 'iframe') return true;
            const descriptor = [
              candidate.src || '', candidate.title || '', candidate.name || ''
            ].join(' ');
            return /challenge|bframe/i.test(descriptor);
          });
          if (visible(node)) return {found: true, reason: 'captcha_detected'};
          const text = (document.body?.innerText || '').toLowerCase();
          if (text.includes('verify you are human') || text.includes('complete the captcha')) {
            return {found: true, reason: 'human_verification'};
          }
          const mfaPhrases = [
            'multi-factor authentication', 'multifactor authentication',
            'two-factor authentication', 'two factor authentication',
            'one-time passcode', 'one time passcode', 'one-time password',
            'verification code', 'security code', 'authenticator app',
            'enter the code we sent', 'use your authentication app',
            'enter the otp', 'enter your otp'
          ];
          if (mfaPhrases.some(phrase => text.includes(phrase))) {
            return {found: true, reason: 'mfa_required'};
          }
          return {found: false, reason: ''};
        }"""
        for scope in self._candidate_scopes(page):
            try:
                result = scope.evaluate(script) or {}
                if result.get("found"):
                    return True, str(result.get("reason") or "captcha_detected")
            except Exception:  # noqa: BLE001
                continue
        return False, ""

    @staticmethod
    def _combobox_value_candidates(value: object) -> tuple[str, ...]:
        text = str(value or "").strip()
        normalised = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
        candidates = [text]
        if re.search(r"\b(?:bsc|bachelor)\b", normalised):
            candidates.append("Bachelor's Degree")
        elif re.search(r"\b(?:msc|master)\b", normalised):
            candidates.append("Master's Degree")
        elif re.search(r"\b(?:phd|doctor of philosophy)\b", normalised):
            candidates.append("Doctor of Philosophy (Ph.D.)")
        return tuple(dict.fromkeys(item for item in candidates if item))

    @staticmethod
    def _normalise_combobox_value(value: object) -> str:
        # Treat representation-only differences as equivalent while keeping
        # matching conservative: case, Unicode width, punctuation, hyphens,
        # whitespace, and the common ``&``/``and`` spelling are normalised;
        # arbitrary synonyms are not.
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        text = text.replace("&", " and ")
        normalised = re.sub(r"[^a-z0-9]+", " ", text).strip()
        return normalised.replace("authorised", "authorized").replace(
            "authorisation", "authorization"
        )

    @staticmethod
    def _normalise_exact_combobox_value(value: object) -> str:
        return _EmbeddedProviderAdapter._normalise_combobox_value(value)

    @staticmethod
    def _strip_label_suffix(value: str) -> str:
        return re.sub(r"\s*\+\d+\s*$", "", value)

    @classmethod
    def _question_family(cls, question_label: object) -> str:
        label = cls._normalise_combobox_value(question_label)
        if "sponsor" in label:
            return "sponsorship"
        if any(
            phrase in label
            for phrase in (
                "authorized to work",
                "authorization",
                "right to work",
                "permission to work",
            )
        ):
            return "work_authorisation"
        return ""

    @classmethod
    def _answer_polarity(cls, value: object, family: str) -> str:
        normalised = cls._normalise_combobox_value(value)
        first_word = normalised.partition(" ")[0]
        if first_word in {"yes", "no"}:
            return first_word
        return _STANDARD_ANSWER_ALIASES.get(family, {}).get(normalised, "")

    @classmethod
    def _option_polarity(cls, value: object, family: str) -> str:
        normalised = cls._normalise_combobox_value(value)
        leading = normalised.partition(" ")[0]
        leading = leading if leading in {"yes", "no"} else ""
        semantic = ""
        if family == "work_authorisation":
            negative = bool(
                re.search(
                    r"\b(?:not|cannot|unable to|do not have|no)\b.{0,40}"
                    r"\b(?:authorized to work|right to work|permission to work)\b",
                    normalised,
                )
            )
            work_phrase = bool(
                re.search(
                    r"\b(?:authorized to work|right to work|permission to work)\b",
                    normalised,
                )
            )
            if work_phrase:
                semantic = "no" if negative else "yes"
        elif family == "sponsorship":
            negative = bool(
                re.search(
                    r"\b(?:not|no|without|do not|does not)\b.{0,40}\bsponsor",
                    normalised,
                )
            )
            sponsor_phrase = "sponsor" in normalised
            if sponsor_phrase:
                semantic = "no" if negative else "yes"
        if leading and semantic and leading != semantic:
            return "conflict"
        return semantic or leading

    @classmethod
    def _polarity_compatible(
        cls,
        expected: object,
        option: object,
        *,
        question_label: object,
    ) -> bool:
        family = cls._question_family(question_label)
        expected_polarity = cls._answer_polarity(expected, family)
        if not expected_polarity:
            return True
        option_polarity = cls._option_polarity(option, family)
        return option_polarity == expected_polarity

    @classmethod
    def _alias_matches(
        cls,
        expected: object,
        option: object,
        *,
        question_label: object,
    ) -> bool:
        family = cls._question_family(question_label)
        expected_normalised = cls._normalise_combobox_value(expected)
        expected_polarity = _STANDARD_ANSWER_ALIASES.get(family, {}).get(
            expected_normalised
        )
        if not expected_polarity:
            return False
        option_normalised = cls._normalise_combobox_value(option)
        if family == "work_authorisation" and not re.search(
            r"\b(?:authorized to work|right to work|permission to work)\b",
            option_normalised,
        ):
            return False
        if family == "sponsorship" and "sponsor" not in option_normalised:
            return False
        return cls._option_polarity(option, family) == expected_polarity

    @classmethod
    def _degree_level_match_index(
        cls,
        expected: object,
        options: tuple[str, ...] | list[str],
        *,
        question_label: object,
    ) -> int | None:
        """Resolve a degree-level option only when the level is unique.

        Profile evidence commonly contains a full degree title while an ATS
        asks for a level.  This is a deterministic representation change, not
        a new fact.  Ambiguous same-level options remain refused.
        """

        prompt = cls._normalise_combobox_value(question_label)
        if "degree" not in prompt and "pursu" not in prompt:
            return None
        expected_text = cls._normalise_combobox_value(expected)
        levels = (
            ("bachelor", ("bachelor", "bsc")),
            ("master", ("master", "msc")),
            ("doctor", ("doctor", "phd")),
        )
        level_tokens = next(
            (tokens for level, tokens in levels if any(token in expected_text for token in tokens)),
            (),
        )
        if not level_tokens:
            return None
        matches = {
            index
            for index, option in enumerate(options)
            if any(token in cls._normalise_combobox_value(option) for token in level_tokens)
        }
        return next(iter(matches)) if len(matches) == 1 else None

    @classmethod
    def _match_combobox_option_index(
        cls,
        expected: object,
        options: tuple[str, ...] | list[str],
        *,
        question_label: object = "",
    ) -> int | None:
        index, _reason = cls._match_combobox_option_result(
            expected,
            options,
            question_label=question_label,
        )
        return index

    @classmethod
    def _match_combobox_option_result(
        cls,
        expected: object,
        options: tuple[str, ...] | list[str],
        *,
        question_label: object = "",
    ) -> tuple[int | None, str]:
        """Return one offered option, or a precise fail-closed reason."""

        if not options:
            return None, "no_matching_option"
        candidates = tuple(
            dict.fromkeys(
                cls._normalise_exact_combobox_value(candidate)
                for candidate in cls._combobox_value_candidates(expected)
                if cls._normalise_exact_combobox_value(candidate)
            )
        )
        option_values = tuple(cls._normalise_exact_combobox_value(option) for option in options)

        exact_matches = {
            index
            for index, option in enumerate(options)
            if option_values[index] in candidates
            and any(
                cls._polarity_compatible(
                    candidate,
                    option,
                    question_label=question_label,
                )
                for candidate in cls._combobox_value_candidates(expected)
                if cls._normalise_exact_combobox_value(candidate) == option_values[index]
            )
        }
        if exact_matches:
            if len(exact_matches) == 1:
                return next(iter(exact_matches)), "matched"
            return None, "ambiguous_option"

        # Suffix-stripped match: remove trailing dial-code decoration
        # (`` +44``) so an option labelled ``United Kingdom +44`` matches
        # the stored ``United Kingdom`` while remaining unique.
        suffix_stripped_matches = {
            index
            for index, option in enumerate(options)
            if cls._normalise_exact_combobox_value(
                cls._strip_label_suffix(str(option))
            ) in candidates
            and any(
                cls._polarity_compatible(
                    candidate,
                    option,
                    question_label=question_label,
                )
                for candidate in cls._combobox_value_candidates(expected)
                if cls._normalise_exact_combobox_value(candidate)
                == cls._normalise_exact_combobox_value(
                    cls._strip_label_suffix(str(option))
                )
            )
        }
        if suffix_stripped_matches:
            if len(suffix_stripped_matches) == 1:
                return next(iter(suffix_stripped_matches)), "matched"
            return None, "ambiguous_option"

        prefix_matches = {
            index
            for index, option in enumerate(options)
            if any(
                re.match(rf"^{re.escape(candidate)}(?=$|\W)", option_values[index])
                and cls._polarity_compatible(
                    raw_candidate,
                    option,
                    question_label=question_label,
                )
                for raw_candidate in cls._combobox_value_candidates(expected)
                if (candidate := cls._normalise_exact_combobox_value(raw_candidate))
            )
        }
        if prefix_matches:
            if len(prefix_matches) == 1:
                return next(iter(prefix_matches)), "matched"
            return None, "ambiguous_option"

        # Keep the degree representation bridge, but distinguish a unique
        # degree level from two equally plausible level options.
        degree_prompt = cls._normalise_combobox_value(question_label)
        degree_expected = cls._normalise_combobox_value(expected)
        degree_levels = (
            ("bachelor", ("bachelor", "bsc")),
            ("master", ("master", "msc")),
            ("doctor", ("doctor", "phd")),
        )
        degree_tokens = next(
            (
                tokens
                for _level, tokens in degree_levels
                if any(token in degree_expected for token in tokens)
            ),
            (),
        )
        if ("degree" in degree_prompt or "pursu" in degree_prompt) and degree_tokens:
            degree_matches = {
                index
                for index, option in enumerate(options)
                if any(token in cls._normalise_combobox_value(option) for token in degree_tokens)
            }
            if len(degree_matches) > 1:
                return None, "ambiguous_option"
            if len(degree_matches) == 1:
                return next(iter(degree_matches)), "matched"

        degree_match = cls._degree_level_match_index(
            expected,
            options,
            question_label=question_label,
        )
        if degree_match is not None:
            return degree_match, "matched"

        alias_matches = {
            index
            for index, option in enumerate(options)
            if any(
                cls._alias_matches(
                    candidate,
                    option,
                    question_label=question_label,
                )
                for candidate in cls._combobox_value_candidates(expected)
            )
        }
        if alias_matches:
            if len(alias_matches) == 1:
                return next(iter(alias_matches)), "matched"
            return None, "ambiguous_option"
        return None, "no_matching_option"

    @classmethod
    def _combobox_value_matches(
        cls,
        expected: object,
        actual: object,
        *,
        question_label: object = "",
    ) -> bool:
        """Check committed ``actual`` matches stored ``expected``.

        A bare dial code does not establish a country match. Provider suffix
        readbacks require the exact matched option at the commit boundary.
        """
        return cls._match_combobox_option_index(
            expected,
            (str(actual or ""),),
            question_label=question_label,
        ) == 0

    @staticmethod
    def _option_texts(locator: Any) -> tuple[str, ...]:
        values = locator.evaluate(_COMBOBOX_OPTIONS_SCRIPT, {}) or []
        return tuple(str(value).strip() for value in values if str(value).strip())

    @staticmethod
    def _selected_combobox_value(locator: Any) -> str:
        """Read the provider's committed label, not only its search input.

        The input element's ``value`` in a react-select / searchable combobox
        contains ONLY the search/filter text typed by the user — never the
        committed selection.  Reading ``element.value`` as a fallback was the
        root cause of the Country/visa "confirmed while empty" bug: typing
        ``l`` into the filter returned ``l`` and the adapter reported the
        field as filled.

        The ONLY authoritative committed-value source is the
        ``.select__single-value`` rendered label (or a native ``<select>``'s
        ``options[selectedIndex].text``, which is handled by the ``select``
        path, not this method).  If no committed widget is found, return empty
        — never fall back to the raw input value.
        """

        value = locator.evaluate(
                """element => {
                  let node = element;
                  for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                    if (node.querySelectorAll?.('[role="combobox"]').length > 1) break;
                        if (node.querySelectorAll?.('.select__single-value').length > 1) break;
                        const selected = node.querySelector?.('.select__single-value');
                    if (selected && (selected.textContent || '').trim()) {
                      return (selected.textContent || '').replace(/\\s+/g, ' ').trim();
                    }
                  }
                  return '';
                }"""
        )
        return str(value or "").strip()

    @staticmethod
    def _click_combobox_option(locator: Any, index: int) -> bool:
        handle = locator.evaluate_handle(
            _COMBOBOX_OPTIONS_SCRIPT,
            {"returnNode": True, "index": index},
        )
        try:
            option = handle.as_element()
            if option is None:
                return False
            option.click(timeout=8_000)
            return True
        finally:
            handle.dispose()

    @staticmethod
    def _restore_combobox(
        locator: Any,
        *,
        expanded: bool,
        value: str,
        provider_state: dict[str, Any],
    ) -> bool:
        """Restore search UI only; prove provider state unchanged or invalidate.

        A label/hidden-value write is not a framework rollback. Leave uncertain
        committed state visible, and require an explicit provider option click
        before clearing this control's native validity error.
        """
        try:
            if not expanded and locator.get_attribute("aria-expanded") == "true":
                locator.press("Escape")
            if locator.input_value() != value:
                locator.fill(value)
            if (locator.input_value() == value and
                    locator.evaluate(_COMBOBOX_STATE_SCRIPT) == provider_state):
                return True
        except Exception:  # noqa: BLE001 - inability to verify is not restoration
            pass
        try:
            locator.evaluate(
                r"""element => {
                  const readState = """ + _COMBOBOX_STATE_SCRIPT + r""";
                  const document = element.ownerDocument;
                  const form = element.form || element.closest('form');
                  const owner = form || document;
                  const guards = owner._argusComboboxGuards ||= new Map();
                  const key = element.id || element.name;
                  if (guards.has(key)) return;
                  const current = () => {
                    if (element.isConnected) return element;
                    const candidates = Array.from(owner.querySelectorAll('[role="combobox"]'))
                      .filter(node => key && (node.id || node.name) === key);
                    return candidates.length === 1 ? candidates[0] : null;
                  };
                  const priorError = element.validity.customError ? element.validationMessage : '';
                  const message = 'ARGUS could not verify this selection. Please reselect an option.';
                  // A form-owned validity sentinel survives replacement of the input.
                  // It has no name/value and never alters submitted provider data.
                  const sentinel = document.createElement('input');
                  sentinel.tabIndex = -1;
                  sentinel.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;pointer-events:none';
                  sentinel.setCustomValidity(message);
                  if (form) form.appendChild(sentinel);
                  const block = () => {
                    const node = current();
                    if (node) {
                      node._argusComboboxNeedsReselection = true;
                      node.setCustomValidity(message);
                    }
                    if (form && !form.contains(sentinel)) form.appendChild(sentinel);
                  };
                  guards.set(key, {current});
                  block();
                  const observer = new MutationObserver(block);
                  observer.observe(form || document, {childList: true, subtree: true});
                  const onSubmit = event => {event.preventDefault(); event.stopImmediatePropagation();};
                  if (form) form.addEventListener('submit', onSubmit, true);
                  const onClick = event => {
                    const node = current();
                    if (!event.isTrusted || !node) return;
                    const option = event.target.closest?.('[role="option"]');
                    const list = option?.closest('[role="listbox"]');
                    const owned = ['aria-controls', 'aria-owns'].flatMap(attribute =>
                      (node.getAttribute(attribute) || '').split(/\s+/).filter(Boolean));
                    if (!list?.id || !owned.includes(list.id)) return;
                    const selected = (option.textContent || '').replace(/\s+/g, ' ').trim();
                    const before = readState(node, true);
                    // Observe actual provider value-setter calls during this trusted
                    // click, including recommitting an unchanged correct hidden ID.
                    // A matching label, an attribute, or a no-op click is not proof.
                    const written = new Set();
                    const cleanups = before.submittedNodes.map(hidden => {
                      const own = Object.getOwnPropertyDescriptor(hidden, 'value');
                      const descriptor = own || Object.getOwnPropertyDescriptor(
                        document.defaultView.HTMLInputElement.prototype, 'value');
                      if (!descriptor?.set || !descriptor.get || own?.configurable === false) return () => {};
                      Object.defineProperty(hidden, 'value', {configurable: true,
                        get() {return descriptor.get.call(this);},
                        set(value) {descriptor.set.call(this, value); written.add(this);}});
                      return () => {
                        if (own) Object.defineProperty(hidden, 'value', own);
                        else delete hidden.value;
                      };
                    });
                    setTimeout(() => {
                      cleanups.forEach(cleanup => cleanup());
                      const live = current();
                      if (!live) return;
                      const state = readState(live, true);
                      const dialSuffix = selected.match(/\s(\+\d[\d ()-]*)$/);
                      const suffixMatches = before.submitted.length > 0 && dialSuffix &&
                        state.selected === dialSuffix[1].trim();
                      if (!selected || (state.selected !== selected && !suffixMatches) ||
                          state.submitted.some(([, value, disabled]) => disabled || !value)) return;
                      if (JSON.stringify(before.submitted.map(([name]) => name)) !==
                          JSON.stringify(state.submitted.map(([name]) => name))) return;
                      if (before.submitted.length) {
                        if (!state.submittedNodes.every((hidden, index) =>
                            written.has(hidden) || state.submitted[index][1] !== before.submitted[index][1])) return;
                      } else if (state.selected === before.selected) return;
                      if (live.validationMessage !== message) return;
                      observer.disconnect();
                      live.setCustomValidity(priorError);
                      delete live._argusComboboxNeedsReselection;
                      guards.delete(key);
                      sentinel.remove();
                      if (form) form.removeEventListener('submit', onSubmit, true);
                      document.removeEventListener('click', onClick, true);
                    }, 0);
                  };
                  document.addEventListener('click', onClick, true);
                }"""
            )
        except Exception:  # noqa: BLE001 - caller must still hand off, never claim blank
            pass
        return False

    @classmethod
    def _combobox_failure(cls, field: Any, reason_code: str) -> ComboboxResolutionError:
        label = str(getattr(getattr(field, "question", None), "label", "") or "combobox")
        messages = {
            "control_unavailable": "The combobox is unavailable",
            "no_matching_option": "No matching option was offered by the widget",
            "ambiguous_option": "More than one offered option matched equally well",
            "commit_failed": "The offered option was not committed by the widget",
            "restoration_unverified": "The selection could not be restored or verified; review and reselect an option",
        }
        detail = messages.get(reason_code, "The combobox could not be resolved")
        message = f"No unique approved option: {detail}"
        return ComboboxResolutionError(f"{message} for {label}", reason_code=reason_code)

    @classmethod
    def _wait_for_combobox_commit(
        cls,
        scope: Any,
        locator: Any,
        expected: object,
        *,
        question_label: object,
        matched_option_text: str = "",
        before_click: dict[str, Any] | None = None,
    ) -> bool:
        for _ in range(10):
            try:
                actual = cls._selected_combobox_value(locator)
            except Exception:
                actual = ""
            if actual and cls._combobox_value_matches(
                expected,
                actual,
                question_label=question_label,
            ):
                return True
            if actual and matched_option_text:
                stripped = cls._strip_label_suffix(str(matched_option_text))
                option_suffix = str(matched_option_text).replace(stripped, "").strip()
                if option_suffix and actual.strip() == option_suffix:
                    state = locator.evaluate(_COMBOBOX_STATE_SCRIPT)
                    # A pre-existing dial code plus a no-op option click cannot
                    # manufacture country proof. Hidden state, when present,
                    # must supply fresh submitted-selection evidence as well.
                    if before_click is not None and state != before_click:
                        submitted = state.get("submitted", [])
                        if not submitted or (
                            submitted != before_click.get("submitted", [])
                            and all(value and not disabled for _, value, disabled in submitted)
                        ):
                            return True
            try:
                scope.wait_for_timeout(100)
            except Exception:
                pass
        return False

    def fill(self, scope: Any, field: Any, value: str) -> None:
        if str(field.control_type).casefold() != "combobox":
            super().fill(scope, field, value)
            return
        locator = scope.locator(field.selector).first
        cache = getattr(self, "_committed_options", None)
        if cache is None:
            self._committed_options = cache = {}
        cache_key = (id(scope), str(getattr(scope, "url", "")), field.selector)
        previous = cache.pop(cache_key, None)
        if previous is not None:
            previous[2].dispose()
        if not locator.count() or not locator.is_visible():
            raise self._combobox_failure(field, "control_unavailable")
        if not locator.is_editable():
            raise self._combobox_failure(field, "commit_failed")
        original_expanded = locator.get_attribute("aria-expanded") == "true"
        original_value = locator.input_value()
        if locator.evaluate("element => !!element._argusComboboxNeedsReselection"):
            raise self._combobox_failure(field, "restoration_unverified")
        original_state = locator.evaluate(_COMBOBOX_STATE_SCRIPT)

        def restore_failure(reason: str) -> ComboboxResolutionError:
            restored = self._restore_combobox(
                locator,
                expanded=original_expanded,
                value=original_value,
                provider_state=original_state,
            )
            return self._combobox_failure(field, reason if restored else "restoration_unverified")

        def fill_search(search_value: str) -> None:
            try:
                locator.fill("")
                locator.type(search_value, delay=20)
            except Exception as exc:  # noqa: BLE001 - failed writes require verified restoration
                raise restore_failure("commit_failed") from exc

        try:
            locator.click(timeout=8_000)
        except Exception as exc:  # noqa: BLE001 - preserve a safe, typed reason
            raise restore_failure("commit_failed") from exc
        option_texts: tuple[str, ...] = ()
        for _ in range(10):
            option_texts = self._option_texts(locator)
            if option_texts:
                break
            scope.wait_for_timeout(100)
        if not option_texts:
            fill_search("")
            locator.focus()
            locator.press("ArrowDown")
            scope.wait_for_timeout(500)
            for _ in range(10):
                option_texts = self._option_texts(locator)
                if option_texts:
                    break
                scope.wait_for_timeout(100)
        match_index, match_reason = self._match_combobox_option_result(
            value,
            option_texts,
            question_label=field.question.label,
        )

        # A virtualised/searchable combobox may expose only a subset after it
        # opens. First type only an option label already enumerated during
        # inspection; the unvalidated stored answer is never used in that
        # path.
        if match_index is None and match_reason != "ambiguous_option" and field.question.options:
            approved_index, approved_reason = self._match_combobox_option_result(
                value,
                field.question.options,
                question_label=field.question.label,
            )
            if approved_index is not None:
                approved_option = field.question.options[approved_index]
                fill_search(approved_option)
                for _ in range(10):
                    option_texts = self._option_texts(locator)
                    match_index, match_reason = self._match_combobox_option_result(
                        approved_option,
                        option_texts,
                        question_label=field.question.label,
                    )
                    if match_index is not None or match_reason == "ambiguous_option":
                        break
                    scope.wait_for_timeout(100)
            # Inspection-time options cannot authorise an implicit form submit.
            if approved_reason == "ambiguous_option":
                match_reason = approved_reason

        # Searchable typeaheads (notably education-school controls) can expose
        # no options until the already-resolved, evidence-backed value is
        # entered. Type it through Playwright, then require an exact option
        # from the widget before accepting the selection. A value that
        # produces no unique option is restored and fails closed.
        if match_index is None and match_reason != "ambiguous_option":
            fill_search(value)
            for _ in range(10):
                option_texts = self._option_texts(locator)
                match_index, match_reason = self._match_combobox_option_result(
                    value,
                    option_texts,
                    question_label=field.question.label,
                )
                if match_index is not None or match_reason == "ambiguous_option":
                    break
                scope.wait_for_timeout(100)

        if match_index is None:
            # Enter can implicitly submit the surrounding application form.
            # No unique offered option means restore and hand off, never Enter.
            raise restore_failure(match_reason)
        try:
            before_click = locator.evaluate(_COMBOBOX_STATE_SCRIPT)
            clicked = self._click_combobox_option(locator, match_index)
        except Exception as exc:  # noqa: BLE001 - a click without readback is unsafe
            clicked = False
            click_error = exc
        else:
            click_error = None
        matched_option = option_texts[match_index] if match_index is not None and match_index < len(option_texts) else ""
        if not clicked or not self._wait_for_combobox_commit(
            scope,
            locator,
            value,
            question_label=field.question.label,
            matched_option_text=matched_option,
            before_click=before_click,
        ):
            error = restore_failure("commit_failed")
            if click_error is not None:
                raise error from click_error
            raise error

        # Private evidence from this exact successful click, bound to the DOM
        # node and page; never trust a page-authored attribute as proof.
        cache[cache_key] = (str(value), matched_option, locator.evaluate_handle(
            "element => {const read = " + _COMBOBOX_STATE_SCRIPT + "; return read(element, true);}"))

    @staticmethod
    def _fresh_combobox_proof(locator: Any, proof: Any) -> bool:
        return bool(locator.evaluate(
            "(element, proof) => {const read = " + _COMBOBOX_STATE_SCRIPT + """;
              const state = read(element, true);
              return element === proof.element && state.selectedNode === proof.selectedNode &&
                state.selected === proof.selected &&
                JSON.stringify(state.submitted) === JSON.stringify(proof.submitted) &&
                state.submittedNodes.length === proof.submittedNodes.length &&
                state.submittedNodes.every((node, index) => node === proof.submittedNodes[index]);
            }""", proof))

    def _verified_option_suffix(self, scope: Any, field: Any, locator: Any, expected: str, actual: str) -> bool:
        key = (id(scope), str(getattr(scope, "url", "")), field.selector)
        proof = getattr(self, "_committed_options", {}).get(key)
        if proof is None or proof[0] != str(expected):
            return False
        _, option, node = proof
        suffix = option.replace(self._strip_label_suffix(option), "").strip()
        if not suffix or actual.strip() != suffix or node is None:
            return False
        return self._fresh_combobox_proof(locator, node)

    def verify_step(self, scope: Any, fields: list, expected_values: dict[str, str]) -> bool:
        for field in fields:
            expected = expected_values.get(field.question.name)
            if expected is None:
                continue
            locator = scope.locator(field.selector)
            if not locator.count():
                return False
            try:
                if field.control_type == "combobox":
                    if locator.first.evaluate("element => !!element._argusComboboxNeedsReselection"):
                        return False
                    key = (id(scope), str(getattr(scope, "url", "")), field.selector)
                    proof = getattr(self, "_committed_options", {}).get(key)
                    if proof is not None and not self._fresh_combobox_proof(locator.first, proof[2]):
                        return False
                    actual = self._selected_combobox_value(locator.first)
                    if not self._combobox_value_matches(
                        expected,
                        actual,
                        question_label=field.question.label,
                    ) and not self._verified_option_suffix(scope, field, locator.first, expected, actual):
                        return False
                elif field.control_type == "select":
                    selected = locator.first.evaluate(
                        """element => {
                          const option = element.options?.[element.selectedIndex];
                          return {
                            value: element.value || '',
                            label: (option?.textContent || '').replace(/\\s+/g, ' ').trim(),
                          };
                        }"""
                    )
                    if not semantic_value_matches(
                        expected,
                        selected.get("value", ""),
                        label=field.question.label,
                        control_type=field.control_type,
                        alternatives=(selected.get("label", ""),),
                    ):
                        return False
                    actual = selected.get("value", "")
                elif field.control_type in {"checkbox", "radio"}:
                    if field.control_type == "checkbox" and field.question.option_label:
                        if not semantic_value_matches(
                            expected,
                            field.question.option_label,
                            label=field.question.label,
                            control_type=field.control_type,
                            alternatives=(field.value_attribute,),
                        ):
                            continue
                    actual = any(locator.nth(i).is_checked() for i in range(locator.count()))
                elif field.control_type == "file":
                    actual = True
                else:
                    actual = locator.first.input_value()
                if field.control_type not in {"checkbox", "radio", "file", "combobox", "select"} and not semantic_value_matches(
                    expected,
                    actual,
                    label=field.question.label,
                    control_type=field.control_type,
                ):
                    return False
                if field.control_type in {"checkbox", "radio"} and not actual:
                    return False
                # React-style comboboxes often keep the selected label in a
                # sibling widget element while their hidden search input is
                # intentionally empty.  The semantic readback above is the
                # validity evidence for that control; applying native
                # checkValidity() to the empty search input would reject a
                # correctly selected required option.  Native controls still
                # require browser validity.
                if field.control_type != "combobox" and not locator.first.evaluate(
                    "element => !element.willValidate || element.checkValidity()"
                ):
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True

    def advance_one_step(self, scope: Any, *, max_wait_steps: int = 10) -> bool:
        # Greenhouse and Lever generally expose one application form. If a
        # tenant adds a Next control, it is still one bounded step, never a
        # final-submit candidate.
        next_control = scope.locator(
            '[data-automation-id="submitNextButton"], button:has-text("Next")'
        )
        if not next_control.count() or not next_control.first.is_visible():
            return False
        before = self.current_step_marker(scope)
        if not before:
            return False
        next_control.first.click(timeout=8_000)
        try:
            scope.wait_for_timeout(100)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(max_wait_steps):
            after = self.current_step_marker(scope)
            if after and after != before:
                try:
                    scope.wait_for_timeout(100)
                except Exception:  # noqa: BLE001
                    pass
                return True
            try:
                scope.wait_for_timeout(100)
            except Exception:  # noqa: BLE001
                break
        return False

    @staticmethod
    def current_step_marker(scope: Any) -> str:
        """Return step/control shape while excluding validation copy and values."""

        try:
            return str(
                scope.evaluate(
                    """() => {
                      const visible = node => {
                        if (!node) return false;
                        const style = getComputedStyle(node);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                      };
                      const root = Array.from(document.querySelectorAll(
                        '[data-step], [data-page], [aria-current="step"], form'
                      )).find(visible);
                      if (!root) return '';
                      const form = root.tagName.toLowerCase() === 'form'
                        ? root : (root.querySelector('form') || root);
                      const attributes = [
                        root.id || '', root.className || '', root.getAttribute('data-step') || '',
                        root.getAttribute('data-page') || '', root.getAttribute('aria-current') || '',
                        root.getAttribute('aria-label') || '', form.getAttribute('action') || '',
                        form.getAttribute('method') || ''
                      ];
                      const controls = Array.from(form.querySelectorAll(
                        'input, select, textarea, button'
                      )).map(control => [
                        control.tagName, control.name || '', control.id || '',
                        control.getAttribute('type') || '',
                        control.getAttribute('data-automation-id') || '',
                        control.getAttribute('aria-label') || ''
                      ].join(':')).join('|');
                      return attributes.concat(controls).join('::');
                    }"""
                )
                or ""
            )
        except Exception:  # noqa: BLE001 - detached scopes fail closed
            return ""

    def final_submission_target(self, page: Any) -> SubmissionTarget | None:
        scope = self.current_scope(page)
        handle = getattr(self, "_last_form_handle", None)
        if handle is None:
            return None
        return self.submission_target(scope, handle)


class GreenhouseAdapter(_EmbeddedProviderAdapter):
    name = "greenhouse"

    _FILE_READBACK_ATTEMPTS = 300
    _FILE_READBACK_INTERVAL_MS = 100
    _FILE_READBACK_LOCATOR_TIMEOUT_MS = 250

    @classmethod
    def matches(cls, url: str, html: str = "") -> bool:
        del html  # DOM labels are mutable and cannot establish provider trust.
        return trusted_provider_for_url(url) == "greenhouse"

    def fill(self, scope: Any, field: Any, value: str) -> None:
        if str(field.control_type).casefold() != "file":
            super().fill(scope, field, value)
            return

        # Playwright can populate the local FileList even when the provider's
        # upload handler rejects or never completes the upload.  Greenhouse's
        # public form exposes the filename only after its provider-side state
        # has accepted the attachment, so require that visible readback before
        # the runner can count the field as prefilled.
        locator = scope.locator(field.selector).first
        if not locator.count():
            raise RuntimeError(f"File control is unavailable: {field.question.label}")
        upload_root = locator.locator(
            "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
            "' file-upload ')][1]"
        ).first
        if not upload_root.count():
            raise RuntimeError(
                f"File upload wrapper is unavailable: {field.question.label}"
            )
        upload_root_handle = upload_root.element_handle(
            timeout=self._FILE_READBACK_LOCATOR_TIMEOUT_MS
        )
        if upload_root_handle is None:
            raise RuntimeError(
                f"File upload wrapper is unavailable: {field.question.label}"
            )
        super().fill(scope, field, value)
        expected_filename = str(value).replace("\\", "/").rsplit("/", 1)[-1]
        for _ in range(self._FILE_READBACK_ATTEMPTS):
            try:
                if upload_root_handle.evaluate(
                    _FILE_ATTACHMENT_READBACK_SCRIPT,
                    expected_filename,
                ):
                    return
            except Exception:  # noqa: BLE001 - detached/provider DOM fails closed
                pass
            scope.wait_for_timeout(self._FILE_READBACK_INTERVAL_MS)
        raise RuntimeError(
            f"Provider attachment readback failed for {field.question.label}"
        )
