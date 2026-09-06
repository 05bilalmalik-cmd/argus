from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Page

from app.automation.adapters.verification import semantic_value_matches
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion
from app.automation.targets import (
    FormHandle,
    SubmissionTarget,
    TargetResolution,
    trusted_provider_for_url,
)


# Controls that are never application questions wherever they appear.
_NON_QUESTION_TYPES = frozenset({"hidden", "submit", "reset", "image", "button"})

_INSPECT_SCRIPT = r"""
async () => {
  // ---------------------------------------------------------------- helpers
  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return value.replace(/([^a-zA-Z0-9_-])/g, '\\$1');
  }

  function textOf(node) {
    return node ? (node.textContent || '').replace(/\s+/g, ' ').trim() : '';
  }

  function accessibleText(node) {
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
  }

  function listboxesFor(el, scopeEl) {
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
        // Legacy Greenhouse/React Select markup sometimes omits the listbox
        // role on the nearest option container. A direct ARIA reference or
        // nearest unique container still provides a conservative association.
        listbox = candidate;
      }
      if (listbox && !listboxes.includes(listbox)) listboxes.push(listbox);
    };

    for (const attribute of ['aria-controls', 'aria-owns']) {
      const ids = (el.getAttribute(attribute) || '').trim().split(/\s+/).filter(Boolean);
      ids.forEach(id => add(document.getElementById(id)));
    }
    const activeId = (el.getAttribute('aria-activedescendant') || '').trim();
    if (activeId) add(document.getElementById(activeId));
    const visibleListboxes = () => listboxes.filter(isVisible);
    if (visibleListboxes().length) return visibleListboxes();

    const associationIds = new Set([
      el.id,
      ...(el.getAttribute('aria-labelledby') || '').trim().split(/\s+/),
    ].filter(Boolean));
    const labelled = Array.from(document.querySelectorAll('[role="listbox"]')).filter(listbox =>
      isVisible(listbox) &&
      (listbox.getAttribute('aria-labelledby') || '').trim().split(/\s+/)
        .some(id => associationIds.has(id))
    );
    if (labelled.length === 1) return labelled;

    let ancestor = el.parentElement;
    for (let depth = 0; ancestor && depth < 6; depth += 1, ancestor = ancestor.parentElement) {
      const nearby = Array.from(ancestor.querySelectorAll('[role="listbox"]')).filter(isVisible);
      if (nearby.length === 1) return nearby;
      if (!nearby.length && ancestor.querySelectorAll('[role="option"]').length) {
        return [ancestor];
      }
      if (ancestor === scopeEl) break;
    }
    const scoped = Array.from(scopeEl.querySelectorAll('[role="listbox"]')).filter(isVisible);
    return scoped.length === 1 ? scoped : [];
  }

  function comboboxOptionTexts(el, scopeEl) {
    return listboxesFor(el, scopeEl).flatMap(listbox =>
      Array.from(listbox.querySelectorAll('[role="option"]'))
        .map(accessibleText)
        .filter(Boolean)
    );
  }

  let comboboxProbeCount = 0;
  async function optionTexts(el, scopeEl) {
    if (el.tagName.toLowerCase() === 'select') {
      return Array.from(el.options).map(o => textOf(o)).filter(Boolean);
    }
    if ((el.getAttribute('role') || '').toLowerCase() !== 'combobox') return [];

    let options = comboboxOptionTexts(el, scopeEl);
    if (options.length || comboboxProbeCount >= 8) return options;

    // Some ARIA comboboxes render their listbox only after interaction. This
    // read-only probe is bounded to 200 ms per control and acts only on the
    // combobox input itself; it never clicks a Next/Submit/navigation control.
    comboboxProbeCount += 1;
    const originalExpanded = el.getAttribute('aria-expanded');
    const originalFocus = document.activeElement;
    const originalUrl = location.href;
    try {
      el.focus({preventScroll: true});
      el.dispatchEvent(new MouseEvent('mousedown', {
        bubbles: true, cancelable: true, view: window, button: 0, buttons: 1
      }));
      el.dispatchEvent(new MouseEvent('mouseup', {
        bubbles: true, cancelable: true, view: window, button: 0
      }));
      el.dispatchEvent(new MouseEvent('click', {
        bubbles: true, cancelable: true, view: window, button: 0
      }));
      for (let attempt = 0; attempt < 4 && !options.length; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 25));
        options = comboboxOptionTexts(el, scopeEl);
      }
      if (!options.length) {
        el.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'ArrowDown', code: 'ArrowDown', bubbles: true, cancelable: true
        }));
        el.dispatchEvent(new KeyboardEvent('keyup', {
          key: 'ArrowDown', code: 'ArrowDown', bubbles: true, cancelable: true
        }));
        for (let attempt = 0; attempt < 4 && !options.length; attempt += 1) {
          await new Promise(resolve => setTimeout(resolve, 25));
          options = comboboxOptionTexts(el, scopeEl);
        }
      }
    } finally {
      if (originalExpanded !== 'true' && el.getAttribute('aria-expanded') === 'true') {
        el.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Escape', code: 'Escape', bubbles: true, cancelable: true
        }));
        el.dispatchEvent(new KeyboardEvent('keyup', {
          key: 'Escape', code: 'Escape', bubbles: true, cancelable: true
        }));
      }
      if (originalFocus && originalFocus !== el && originalFocus.isConnected) {
        originalFocus.focus({preventScroll: true});
      } else if (originalFocus !== el) {
        el.blur();
      }
    }
    return location.href === originalUrl ? options : [];
  }

  // Sibling-relative position: CSS :nth-of-type counts among siblings of
  // the same tag, so count previous siblings of the same tag name — NOT a
  // document-wide index (the old defect made selectors address wrong nodes).
  function siblingIndexOf(el) {
    let count = 1;
    let node = el.previousElementSibling;
    while (node) {
      if (node.tagName === el.tagName) count += 1;
      node = node.previousElementSibling;
    }
    return count;
  }

  function selectorWithin(el, root) {
    const parts = [];
    let node = el;
    while (node && node !== root) {
      const tag = node.tagName.toLowerCase();
      if (node.id) {
        // Prefer ids; they are unambiguous inside the document and therefore
        // also unambiguous within the root.
        parts.unshift('#' + cssEscape(node.id));
        break;
      }
      if (node.parentElement) {
        parts.unshift(tag + ':nth-of-type(' + siblingIndexOf(node) + ')');
      }
      node = node.parentElement;
      if (parts.length > 6) break; // bounded depth
    }
    return parts.join(' > ');
  }

  function scopedSelector(el, root, rootSelector) {
    const relative = selectorWithin(el, root);
    if (!rootSelector) return relative;
    if (!relative) return rootSelector;
    return rootSelector + ' ' + relative;
  }

  function labelFor(el) {
    if (el.type === 'radio' || el.type === 'checkbox') {
      const fieldset = el.closest('fieldset');
      const legend = fieldset && fieldset.querySelector(':scope > legend');
      if (legend && textOf(legend)) return textOf(legend);
    }
    if (el.id) {
      const explicit = document.querySelector('label[for="' + cssEscape(el.id) + '"]');
      if (explicit && textOf(explicit)) return textOf(explicit);
    }
    const wrapped = el.closest('label');
    if (wrapped) {
      const clone = wrapped.cloneNode(true);
      clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
      if (textOf(clone)) return textOf(clone);
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const joined = labelledBy.split(/\s+/).map(id => textOf(document.getElementById(id))).filter(Boolean).join(' ');
      if (joined) return joined;
    }
    return (el.placeholder || el.name || el.id || el.type || el.tagName).trim();
  }

  function fileLabelFor(el, scopeEl) {
    const direct = labelFor(el);
    if (!/^(?:attach|upload|file)(?:\s+(?:a\s+)?(?:file|document))?[\s*:]*$/i.test(direct)) return direct;
    // A generic Attach label is a widget action, not a document purpose.
    // Inspect only a local container owning exactly this file input. Never
    // borrow a neighbouring upload's heading or a page-wide job title.
    let node = el.parentElement;
    for (let depth = 0; node && node !== scopeEl && depth < 6; depth += 1, node = node.parentElement) {
      const uploads = node.querySelectorAll('input[type="file"]');
      if (uploads.length !== 1 || uploads[0] !== el) break;
      const headings = Array.from(node.querySelectorAll(
        ':scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > h6, :scope > legend, :scope > [role="heading"], :scope > .file-upload__label'
      )).filter(isVisible).map(textOf).filter(Boolean);
      if (headings.length) return [...new Set(headings), direct].join(' — ');
    }
    return direct;
  }

  function optionLabel(el) {
    if (el.id) {
      const explicit = document.querySelector('label[for="' + cssEscape(el.id) + '"]');
      if (explicit && textOf(explicit)) return textOf(explicit);
    }
    const wrapped = el.closest('label');
    if (wrapped) {
      const clone = wrapped.cloneNode(true);
      clone.querySelectorAll('input').forEach(n => n.remove());
      if (textOf(clone)) return textOf(clone);
    }
    return (el.value || '').trim();
  }

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    // NOTE: el.hidden is unsafe here — a named control like <input name="hidden">
    // shadows the IDL property on container elements via named access.
    if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true') return false;
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return el.getClientRects().length > 0;
  }

  function isFinalSubmitControl(el) {
    if (!isVisible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'button' ? 'submit' : '')).toLowerCase();
    if (el.getAttribute('data-automation-id') === 'submitButton') {
      return tag === 'button' || (tag === 'input' && type === 'submit');
    }
    return type === 'submit';
  }

  // ------------------------------------------------- application-root search
  // The application form is a bounded region: a form element carrying an
  // application marker, or the smallest marked container. Marketing-site
  // controls (search boxes, cookie banners, newsletter signup) live OUTSIDE
  // any such boundary and must never be scanned as questions.
  function findApplicationRoot() {
    const candidates = Array.from(document.querySelectorAll(
      'form, [data-ats-application], [data-automation-id*="form" i], section[aria-label*="application" i], div[data-testid*="application" i]'
    ));
    let best = null;
    for (const el of candidates) {
      if (!isVisible(el)) continue;
      const inputs = el.querySelectorAll('input:not([type=hidden]), select, textarea').length;
      const descriptor = [
        el.id, el.className, el.getAttribute('role'), el.getAttribute('aria-label'),
        el.getAttribute('data-testid'), el.getAttribute('data-automation-id'),
        el.getAttribute('action'), textOf(el).slice(0, 500)
      ].filter(Boolean).join(' ').toLowerCase();
      const knownProviderForm = /\b(grnhse_app|lever-application|posting-apply|wd-application|ats-form|greenhouse-form)\b/.test(descriptor);
      const explicitlyMarked = el.hasAttribute('data-ats-application')
        || /application/.test(String(el.getAttribute('aria-label') || ''))
        || /application/.test(String(el.getAttribute('data-testid') || ''))
        || /application/.test(String(el.getAttribute('data-automation-id') || ''))
        || knownProviderForm;
      const searchOnly = !!el.querySelector('input[type="search"]')
        && !el.querySelector('input:not([type="hidden"]):not([type="search"]), select, textarea');
      const excluded = /\b(search|newsletter|subscribe|cookie|consent|preferences?|settings?|sign[ -]?in|log[ -]?in|sign[ -]?up|contact|marketing|job[ -]?alert)\b/.test(descriptor)
        || el.getAttribute('role') === 'search'
        || !!el.querySelector('input[type="password"]')
        || searchOnly;
      // A mutable data marker cannot turn a marketing, preference, login, or
      // newsletter form into an application root.
      if (excluded) continue;
      const names = Array.from(el.querySelectorAll('input, select, textarea'))
        .map(control => [control.name, control.id, control.getAttribute('autocomplete')].filter(Boolean).join(' ').toLowerCase());
      const identitySignals = names.filter(name => /first.?name|last.?name|email|phone|resume|cv/.test(name)).length;
      const documentUpload = !!el.querySelector(
        'input[type="file"], [data-document-upload], [data-upload], [aria-label*="resume" i], [aria-label*="cv" i]'
      );
      const credibleControlInventory = identitySignals >= 2 && documentUpload;
      const submitText = Array.from(el.querySelectorAll('button, input[type="submit"]'))
        .map(control => (textOf(control) || control.value || '').toLowerCase()).join(' ');
      const identityContext = [];
      const roleContext = [];
      let contextNode = el;
      for (let depth = 0; contextNode && depth < 5; depth += 1, contextNode = contextNode.parentElement) {
        for (const attribute of ['data-role', 'data-job-title', 'data-employer', 'data-company', 'data-requisition', 'data-requisition-id', 'data-posting-id']) {
          const value = contextNode.getAttribute(attribute);
          if (value) {
            identityContext.push(value);
            if (attribute === 'data-role' || attribute === 'data-job-title') roleContext.push(value);
          }
        }
      }
      const identityText = identityContext.join(' ');
      const roleEvidence = roleContext.some(value => value.trim().length > 0);
      const requisitionEvidence = /(?:\bREQ[-_/:][a-z0-9]|\/(?:jobs?|postings?|requisitions?)\/[^/]+)/i.test(
        `${el.getAttribute('action') || ''} ${identityText}`
      );
      // ATS implementation IDs/classes and visible button copy are mutable;
      // neither can self-attest an application.  A marker is useful only when
      // bound to a role/requisition or an application-shaped action.  A
      // document-upload plus contact-identity inventory is the bounded generic
      // fallback for legitimate forms without provider markup.
      const actionEvidence = /(?:^|\/)(?:apply|application|jobs?|postings?|requisitions?)(?:\/|$)/i.test(
        String(el.getAttribute('action') || '')
      );
      const boundApplicationMarker = el.hasAttribute('data-ats-application')
        && (roleEvidence || requisitionEvidence || actionEvidence);
      const positiveRootIdentity = roleEvidence || requisitionEvidence
        || boundApplicationMarker || credibleControlInventory;
      const hasSubmit = !!Array.from(el.querySelectorAll(
        'button, input[type="submit"], [data-automation-id="submitButton"]'
      )).find(isFinalSubmitControl);
      const applicationText = /apply|application|candidate|resume|cover letter|submit application/.test(descriptor + ' ' + submitText);
      const qualifies = knownProviderForm
        ? (inputs >= 1 && identitySignals >= 2 && positiveRootIdentity)
        : explicitlyMarked
        ? (inputs >= 1 && hasSubmit && applicationText
          && positiveRootIdentity && identitySignals >= 1)
        : (hasSubmit && applicationText && positiveRootIdentity
          && identitySignals >= 1);
      if (!qualifies || (inputs < 1 && !knownProviderForm)) continue;
      // Prefer the SMALLEST qualifying container (deepest application form,
      // not a page-wide wrapper that happens to contain it).
      if (!best || best.contains(el)) best = el;
    }
    return best;
  }

  function rootIdentitySnapshot(element) {
    if (!element) return {};
    const values = {
      employer: [],
      role: [],
      requisition: [],
      provider: [],
      form_identity: '',
    };
    let contextNode = element;
    for (let depth = 0; contextNode && depth < 5; depth += 1, contextNode = contextNode.parentElement) {
      for (const attribute of ['data-employer', 'data-company']) {
        const value = contextNode.getAttribute(attribute);
        if (value) values.employer.push(value);
      }
      for (const attribute of ['data-role', 'data-job-title']) {
        const value = contextNode.getAttribute(attribute);
        if (value) values.role.push(value);
      }
      for (const attribute of ['data-requisition', 'data-requisition-id', 'data-posting-id']) {
        const value = contextNode.getAttribute(attribute);
        if (value) values.requisition.push(value);
      }
      for (const attribute of ['data-ats', 'data-provider']) {
        const value = contextNode.getAttribute(attribute);
        if (value) values.provider.push(value);
      }
      if (!values.form_identity) {
        values.form_identity = contextNode.getAttribute('data-argus-form-identity')
          || contextNode.getAttribute('data-form-identity')
          || contextNode.getAttribute('data-application-form-id')
          || '';
      }
    }
    const form = element.tagName.toLowerCase() === 'form'
      ? element : element.querySelector('form');
    if (form) {
      values.requisition.push(form.getAttribute('action') || '');
    }
    element.querySelectorAll('input, select, textarea').forEach(control => {
      const name = (control.name || control.id || '').toLowerCase();
      if (/(?:requisition|posting|job[_-]?id|req(?:uisition)?[_-]?id)/i.test(name)) {
        values.requisition.push(control.value || '');
      }
    });
    return {
      employer: values.employer.filter(Boolean).join(' '),
      role: values.role.filter(Boolean).join(' '),
      requisition: values.requisition.filter(Boolean).join(' '),
      provider: values.provider.filter(Boolean).join(' '),
      form_identity: values.form_identity,
      action: form ? (form.getAttribute('action') || '') : '',
    };
  }

  const root = findApplicationRoot();
  const rootIdentity = rootIdentitySnapshot(root);
  const rootSelector = root ? selectorWithin(root, root.parentElement) : '';
  // A selector alone is not an identity: a page can replace the element while
  // keeping the same id.  Bind the inspected DOM node to an in-page WeakMap;
  // submission revalidates this token before constructing a click target.
  const rootToken = root ? (() => {
    const registry = window.__argusFormRootTokens || (window.__argusFormRootTokens = new WeakMap());
    let token = registry.get(root);
    if (!token) {
      token = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()) + String(Date.now());
      registry.set(root, token);
    }
    return token;
  })() : '';

  const elementTokens = window.__argusElementTokens || (window.__argusElementTokens = new WeakMap());
  function tokenForElement(element) {
    if (!element) return '';
    let token = elementTokens.get(element);
    if (!token) {
      token = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()) + String(Date.now());
      elementTokens.set(element, token);
    }
    return token;
  }

  function hashFingerprint(value) {
    let first = 2166136261;
    let second = 16777619;
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index);
      first ^= code;
      first = Math.imul(first, 16777619);
      second ^= code + index;
      second = Math.imul(second, 2246822519);
    }
    return (first >>> 0).toString(16) + ':' + (second >>> 0).toString(16);
  }

  function subtreeFingerprint(element) {
    if (!element) return '';
    const identities = [tokenForElement(element)];
    element.querySelectorAll('*').forEach(node => identities.push(tokenForElement(node)));
    return hashFingerprint(identities.join('|'));
  }

  function controlFingerprint(element) {
    if (!element) return '';
    return hashFingerprint([
      element.tagName,
      element.id,
      element.getAttribute('type') || '',
      element.getAttribute('name') || '',
      element.getAttribute('value') || '',
      element.getAttribute('data-automation-id') || '',
      textOf(element),
    ].join('|'));
  }

  function formState(element) {
    if (!element) return { action: '', method: '' };
    const form = element.tagName.toLowerCase() === 'form' ? element : element.querySelector('form');
    return {
      action: form ? (form.getAttribute('action') || '') : '',
      method: form ? ((form.getAttribute('method') || 'post').toLowerCase()) : 'post',
    };
  }

  const ignoredTypes = new Set(['hidden', 'submit', 'reset', 'image', 'button']);
  async function scanControls(scopeEl) {
    const controls = Array.from(
      scopeEl.querySelectorAll('input, select, textarea')
    );
    const seenRadioNames = new Set();
    const result = [];
    for (const el of controls) {
      const inputType = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
      if (ignoredTypes.has(inputType)) continue;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true' || !isVisible(el)) continue;

      if (inputType === 'radio') {
        const name = el.name || selectorWithin(el, root);
        if (seenRadioNames.has(name)) continue;
        seenRadioNames.add(name);
        const group = el.name
          ? Array.from(scopeEl.querySelectorAll('input[type="radio"][name="' + cssEscape(el.name) + '"]'))
          : [el];
        result.push({
          // Radio GROUPS address every option by name (unique per form);
          // a positional selector would match only the first option and
          // break value matching at fill time.
          selector: el.name
            ? rootSelector + ' input[type="radio"][name="' + el.name.replace(/"/g, '\\"') + '"]'
            : scopedSelector(el, root, rootSelector),
          label: labelFor(el),
          field_type: 'radio',
          name: el.name || el.id || '',
          placeholder: '',
          required: group.some(item => item.required),
          options: group.map(optionLabel).filter(Boolean),
          control_type: 'radio',
          value_attribute: '',
          option_label: '',
        });
        continue;
      }

      const options = await optionTexts(el, scopeEl);
      result.push({
        selector: scopedSelector(el, root, rootSelector),
        label: inputType === 'file' ? fileLabelFor(el, scopeEl) : labelFor(el),
        field_type: inputType,
        name: el.name || el.id || '',
        placeholder: el.placeholder || '',
        required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
        options,
        control_type: el.getAttribute('role') === 'combobox' ? 'combobox' : inputType,
        value_attribute: el.value || '',
        option_label: inputType === 'checkbox' ? optionLabel(el) : '',
      });
    }
    return result;
  }

  // Assessment/CAPTCHA handoffs are PAGE-level signals, not form questions.
  // They stay document-scoped because they trigger a human handoff (never a
  // fill and never a click by ARGUS).
  const fields = [];

  const assessmentPattern = /(assessment|hirevue|video interview|coding test|psychometric|numerical reasoning)/i;
  for (const button of Array.from(document.querySelectorAll('button'))) {
    if (!isVisible(button)) continue;
    const label = textOf(button);
    if (!assessmentPattern.test(label)) continue;
    fields.push({
      selector: selectorWithin(button, button.parentElement),
      label,
      field_type: 'button',
      name: button.name || '',
      placeholder: '',
      required: true,
      options: [],
      control_type: 'button',
      value_attribute: '',
    });
  }

  const captcha = Array.from(document.querySelectorAll(
    '.g-recaptcha, .h-captcha, [data-sitekey], iframe[src*="captcha" i], iframe[title*="captcha" i]'
  )).find(node => {
    if (!isVisible(node) || node.closest('.grecaptcha-badge, .grecaptcha-logo')) return false;
    if (node.tagName.toLowerCase() !== 'iframe') return true;
    const descriptor = [node.src || '', node.title || '', node.name || ''].join(' ');
    return /challenge|bframe/i.test(descriptor);
  });

  if (captcha) {
    fields.push({
      selector: '.g-recaptcha, [data-sitekey], iframe[src*="captcha" i], iframe[title*="captcha" i]',
      label: 'Verify you are human',
      field_type: 'captcha',
      name: 'captcha',
      placeholder: '',
      required: true,
      options: [],
      control_type: 'captcha',
      value_attribute: '',
    });
  }

  if (root) {
    fields.push(...await scanControls(root));
  }

  const submitControl = root
    ? Array.from(root.querySelectorAll(
        'button, input[type="submit"], [data-automation-id="submitButton"]'
      )).find(isFinalSubmitControl)
    : null;
  const submitCandidate = !!submitControl;
  const currentFormState = formState(root);

  return {
    root_found: !!root,
    root_description: rootSelector,
    root_selector: rootSelector,
    root_token: rootToken,
    submit_present: submitCandidate,
    subtree_fingerprint: subtreeFingerprint(root),
    submit_token: tokenForElement(submitControl),
    submit_fingerprint: controlFingerprint(submitControl),
    form_action: currentFormState.action,
    form_method: currentFormState.method,
    root_identity: rootIdentity,
    fields,
  };
}
"""


_BINDING_EMPLOYER_KEYS = frozenset({"employer", "employername", "company", "companyname"})
_BINDING_ROLE_KEYS = frozenset({"role", "roletitle", "jobtitle", "position", "positiontitle"})
_BINDING_REQUISITION_KEYS = frozenset(
    {"requisition", "requisitionid", "jobid", "jobidentifier", "postingid", "req", "reqid"}
)
_BINDING_FORM_KEYS = frozenset(
    {"form", "formid", "formidentity", "applicationform", "applicationroot", "formhandle"}
)


def _binding_items(value: Any, *, depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _binding_items(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            yield from _binding_items(item, depth=depth + 1)


def _binding_value(value: Any, aliases: frozenset[str]) -> str:
    for key, item in _binding_items(value):
        normalised = "".join(character for character in key.casefold() if character.isalnum())
        if normalised not in aliases:
            continue
        if isinstance(item, (str, int, float)):
            text = str(item).strip()
            if text and text.casefold() not in {"none", "null", "false"}:
                return text
    return ""


def _normalise_binding_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _http_origin(value: object) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(str(value or ""))
        scheme = parts.scheme.casefold()
        hostname = (parts.hostname or "").casefold()
        if scheme not in {"http", "https"} or not hostname:
            return None
        if parts.username or parts.password:
            return None
        port = parts.port
    except (TypeError, ValueError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    elif not 1 <= port <= 65535:
        return None
    return scheme, hostname, port


class GenericAdapter:
    name = "generic"

    def __init__(self, resolution: TargetResolution | None = None) -> None:
        # TargetResolution is frozen and recursively immutable. Keep the
        # object as the explicit trust context; page-controlled DOM evidence
        # never upgrades an unbound adapter to automation readiness.
        self._resolution = resolution
        self._last_form_handle: FormHandle | None = None

    def _verified_binding(self, page: Page, raw: dict[str, Any]) -> bool:
        resolution = self._resolution
        if resolution is None or not resolution.verified_for_automation:
            return False
        expected_provider = (resolution.provider or trusted_provider_for_url(resolution.final_url)).casefold()
        if not expected_provider:
            return False
        try:
            page_url = getattr(page, "url", "")
        except Exception:  # noqa: BLE001 - inaccessible frame/page URL fails closed
            return False
        expected_origin = _http_origin(resolution.final_url)
        page_origin = _http_origin(page_url)
        if expected_origin is None or page_origin is None or expected_origin != page_origin:
            return False
        page_identity = raw.get("root_identity")
        if not isinstance(page_identity, dict):
            return False
        # Greenhouse's current public React form no longer repeats job
        # identity in legacy ``data-*`` attributes. On exact provider-owned
        # hosts only, cross-bind the root against the page-authored Remix
        # loader record. Existing DOM identity values are never overwritten:
        # a contradictory value must still fail below.
        if expected_provider == "greenhouse" and trusted_provider_for_url(page_url) == "greenhouse":
            try:
                greenhouse_identity = page.evaluate(
                    """() => {
                      const hosts = new Set([
                        'boards.greenhouse.io',
                        'job-boards.greenhouse.io',
                        'job-boards.eu.greenhouse.io'
                      ]);
                      if (!hosts.has(location.hostname.toLowerCase())) return {};
                      const root = document.querySelector('form#application-form');
                      if (!root) return {};
                      const loaderData = window.__remixContext?.state?.loaderData;
                      if (!loaderData || typeof loaderData !== 'object') return {};
                      for (const item of Object.values(loaderData).slice(0, 100)) {
                        const job = item && typeof item === 'object' ? item.jobPost : null;
                        if (!job || typeof job !== 'object') continue;
                        const employer = String(job.company_name || '').trim();
                        const role = String(job.title || '').trim();
                        const requisition = String(item.jobPostId || '').trim();
                        if (employer && role && requisition) {
                          return {
                            employer, role, requisition,
                            provider: 'greenhouse',
                            form_identity: requisition
                          };
                        }
                      }
                      return {};
                    }"""
                ) or {}
            except Exception:  # noqa: BLE001 - missing/malformed loader data fails closed
                greenhouse_identity = {}
            if isinstance(greenhouse_identity, dict):
                page_identity = dict(page_identity)
                for key in ("employer", "role", "requisition", "provider", "form_identity"):
                    if not str(page_identity.get(key) or "").strip():
                        page_identity[key] = greenhouse_identity.get(key, "")
        page_provider = _normalise_binding_text(page_identity.get("provider"))
        provider_tokens = {
            token for token in re.split(r"[\s,|:/]+", page_provider) if token
        }
        if page_provider and expected_provider not in provider_tokens:
            return False

        expected_employer = _normalise_binding_text(
            _binding_value(resolution.evidence, _BINDING_EMPLOYER_KEYS)
        )
        expected_role = _normalise_binding_text(
            _binding_value(resolution.evidence, _BINDING_ROLE_KEYS)
        )
        expected_requisition = _normalise_binding_text(
            _binding_value(resolution.evidence, _BINDING_REQUISITION_KEYS)
        )
        expected_form = _normalise_binding_text(
            _binding_value(resolution.evidence, _BINDING_FORM_KEYS)
        )
        # A provider/origin alone proves only the destination, not the exact
        # application root. Require independently supplied role/requisition or
        # form identity before any page form can become automation-ready.
        if not (expected_role and (expected_requisition or expected_form)):
            return False
        identity_text = {
            key: _normalise_binding_text(value)
            for key, value in page_identity.items()
        }
        if expected_employer and expected_employer not in identity_text.get("employer", ""):
            return False
        if expected_role and expected_role not in identity_text.get("role", ""):
            return False
        if expected_requisition and expected_requisition not in identity_text.get("requisition", ""):
            return False
        if expected_form and expected_form != identity_text.get("form_identity", ""):
            return False
        return True

    @classmethod
    def matches(cls, url: str, html: str = "") -> bool:
        return True

    def inspect_with_evidence(self, page: Page) -> tuple[list[InspectedField], dict[str, Any]]:
        """Inspect the verified application root only, and report evidence.

        Returns the field list plus a readiness dict consumed by the runner's
        form-readiness contract. ``root_found=False`` means no bounded
        application form was identified on this page — the runner must treat
        the page as not-an-application-form regardless of control count.
        """

        # Never let a handle from a previous page/step survive a failed or
        # empty inspection.  The caller must earn a fresh root token each
        # time.
        self._last_form_handle = None
        raw: dict[str, Any] = page.evaluate(_INSPECT_SCRIPT) or {}
        fields: list[InspectedField] = []
        for item in raw.get("fields", []):
            fields.append(
                InspectedField(
                    selector=str(item["selector"]),
                    control_type=str(item["control_type"]),
                    value_attribute=str(item.get("value_attribute", "")),
                    question=FormQuestion(
                        label=str(item.get("label", "")).strip(),
                        field_type=str(item.get("field_type", "text")),
                        name=str(item.get("name", "")),
                        placeholder=str(item.get("placeholder", "")),
                        required=bool(item.get("required", False)),
                        options=tuple(str(option) for option in item.get("options", [])),
                        option_label=str(item.get("option_label", "")).strip(),
                    ),
                )
            )
        binding_verified = self._verified_binding(page, raw)
        automation_ready = bool(raw.get("root_found")) and binding_verified
        evidence = {
            "root_found": automation_ready,
            "page_root_found": bool(raw.get("root_found")),
            "automation_ready": automation_ready,
            "binding_verified": binding_verified,
            "root_description": str(raw.get("root_description", "")),
            "root_selector": str(raw.get("root_selector", "")),
            "root_token": str(raw.get("root_token", "")),
            "submit_present": bool(raw.get("submit_present")),
            "subtree_fingerprint": str(raw.get("subtree_fingerprint", "")),
            "submit_token": str(raw.get("submit_token", "")),
            "submit_fingerprint": str(raw.get("submit_fingerprint", "")),
            "form_action": str(raw.get("form_action", "")),
            "form_method": str(raw.get("form_method", "post")).casefold(),
            "root_identity": raw.get("root_identity", {}),
            "controls_enumerated": True,  # the scan itself always ran
            "step_name": "",
        }
        if evidence["root_found"] and evidence["root_selector"]:
            frame_url = str(getattr(page, "url", ""))
            if frame_url:
                self._last_form_handle = FormHandle(
                    page_id=str(id(page)),
                    frame_url=frame_url,
                    root_selector=evidence["root_selector"],
                    provider=self.name,
                    evidence={
                        "root_found": evidence["root_found"],
                        "control_count": len(fields),
                        "submit_present": evidence["submit_present"],
                        "root_token": evidence["root_token"],
                        "subtree_fingerprint": str(raw.get("subtree_fingerprint", "")),
                        "submit_token": str(raw.get("submit_token", "")),
                        "submit_fingerprint": str(raw.get("submit_fingerprint", "")),
                        "form_action": str(raw.get("form_action", "")),
                        "form_method": str(raw.get("form_method", "post")).casefold(),
                        "binding_verified": binding_verified,
                        "bound_target_url": self._resolution.final_url if self._resolution else "",
                        "bound_provider": self._resolution.provider if self._resolution else "",
                        "bound_role": _binding_value(self._resolution.evidence, _BINDING_ROLE_KEYS) if self._resolution else "",
                        "bound_requisition": _binding_value(self._resolution.evidence, _BINDING_REQUISITION_KEYS) if self._resolution else "",
                        "bound_form_identity": _binding_value(self._resolution.evidence, _BINDING_FORM_KEYS) if self._resolution else "",
                    },
                )
        return fields, evidence

    def inspect(self, page: Page) -> list[InspectedField]:
        fields, _evidence = self.inspect_with_evidence(page)
        return fields

    def fill(self, page: Page, field: InspectedField, value: str) -> None:
        locator = page.locator(field.selector)
        control = field.control_type.casefold()
        if control == "file":
            locator.set_input_files(value)
        elif control == "select":
            try:
                locator.select_option(label=value)
            except Exception:
                locator.select_option(value=value)
        elif control == "radio":
            group = page.locator(field.selector)
            count = group.count()
            target = None
            for index in range(count):
                option = group.nth(index)
                option_value = option.get_attribute("value") or ""
                option_id = option.get_attribute("id") or ""
                label = ""
                if option_id:
                    explicit = page.locator(f'label[for="{option_id}"]')
                    if explicit.count():
                        label = explicit.first.inner_text().strip()
                if not label:
                    wrapped = option.locator("xpath=ancestor::label[1]")
                    if wrapped.count():
                        label = wrapped.first.inner_text().strip()
                if value.casefold() in {option_value.casefold(), label.casefold()}:
                    target = option
                    break
            if target is None:
                raise ValueError(
                    f"No radio option matching {json.dumps(value)} for {field.question.label}"
                )
            target.check()
        elif control == "checkbox":
            if field.question.option_label:
                checked = semantic_value_matches(
                    value,
                    field.question.option_label,
                    label=field.question.label,
                    control_type=control,
                    alternatives=(field.value_attribute,),
                )
            else:
                checked = value.strip().casefold() in {"1", "true", "yes", "on", "checked"}
            locator.set_checked(checked)
        else:
            locator.fill(value)

    def submission_target(
        self, page: Page, handle: FormHandle
    ) -> SubmissionTarget | None:
        inspected = self._last_form_handle
        if inspected is None:
            return None
        if (
            handle.page_id != str(id(page))
            or handle.frame_url != str(getattr(page, "url", ""))
            or handle.page_id != inspected.page_id
            or handle.frame_url != inspected.frame_url
            or handle.root_selector != inspected.root_selector
            or handle.provider != inspected.provider
        ):
            return None
        root = page.locator(handle.root_selector)
        if root.count() != 1 or not root.first.is_visible():
            return None
        if not bool(handle.evidence.get("root_found", True)):
            return None
        inspected_root_token = str(inspected.evidence.get("root_token", "") or "")
        root_token = str(handle.evidence.get("root_token", "") or "")
        if root_token != inspected_root_token:
            return None
        if root_token:
            try:
                bound = root.first.evaluate(
                    """(element, token) => {
                      const registry = window.__argusFormRootTokens;
                      return !!registry && registry.get(element) === token;
                    }""",
                    root_token,
                )
            except Exception:
                return None
            if not bound:
                return None
        expected_subtree = str(inspected.evidence.get("subtree_fingerprint", "") or "")
        expected_action = str(inspected.evidence.get("form_action", "") or "")
        expected_method = str(inspected.evidence.get("form_method", "post") or "post").casefold()
        if expected_subtree or expected_action:
            try:
                current_state = root.first.evaluate(
                    """element => {
                      const tokens = window.__argusElementTokens;
                      const tokenFor = node => tokens && node ? tokens.get(node) || '' : '';
                      const hash = value => {
                        let first = 2166136261, second = 16777619;
                        for (let index = 0; index < value.length; index += 1) {
                          const code = value.charCodeAt(index);
                          first ^= code;
                          first = Math.imul(first, 16777619);
                          second ^= code + index;
                          second = Math.imul(second, 2246822519);
                        }
                        return (first >>> 0).toString(16) + ':' + (second >>> 0).toString(16);
                      };
                      const identities = [tokenFor(element)];
                      element.querySelectorAll('*').forEach(node => identities.push(tokenFor(node)));
                      const form = element.tagName.toLowerCase() === 'form' ? element : element.querySelector('form');
                      return {
                        subtree: hash(identities.join('|')),
                        action: form ? (form.getAttribute('action') || '') : '',
                        method: form ? ((form.getAttribute('method') || 'post').toLowerCase()) : 'post',
                      };
                    }"""
                )
            except Exception:
                return None
            if (
                expected_subtree
                and current_state.get("subtree") != expected_subtree
            ):
                return None
            if expected_action != str(current_state.get("action", "")):
                return None
            if expected_method != str(current_state.get("method", "post")).casefold():
                return None
        controls = root.first.locator(
            'button, input[type="submit"], [data-automation-id="submitButton"]'
        )
        candidates = []
        for index in range(controls.count()):
            candidate = controls.nth(index)
            if not candidate.is_visible() or not candidate.is_enabled():
                continue
            if candidate.get_attribute("aria-disabled") == "true":
                continue
            tag = (candidate.evaluate("element => element.tagName") or "").casefold()
            control_type = (candidate.get_attribute("type") or "submit").casefold()
            automation_id = candidate.get_attribute("data-automation-id") or ""
            if tag not in {"button", "input"}:
                continue
            if tag == "input" and control_type != "submit":
                continue
            if (
                tag == "button"
                and control_type != "submit"
                and automation_id != "submitButton"
            ):
                continue
            candidates.append(candidate)

        if not candidates:
            return None
        if len(candidates) > 1:
            raise RuntimeError("Ambiguous visible submission controls found")
        candidate = candidates[0]
        expected_submit_token = str(inspected.evidence.get("submit_token", "") or "")
        expected_submit_fingerprint = str(inspected.evidence.get("submit_fingerprint", "") or "")
        if expected_submit_token or expected_submit_fingerprint:
            try:
                current_submit = candidate.evaluate(
                    """element => {
                      const tokens = window.__argusElementTokens;
                      const token = tokens ? (tokens.get(element) || '') : '';
                      const hash = value => {
                        let first = 2166136261, second = 16777619;
                        for (let index = 0; index < value.length; index += 1) {
                          const code = value.charCodeAt(index);
                          first ^= code;
                          first = Math.imul(first, 16777619);
                          second ^= code + index;
                          second = Math.imul(second, 2246822519);
                        }
                        return (first >>> 0).toString(16) + ':' + (second >>> 0).toString(16);
                      };
                      return {
                        token,
                        fingerprint: hash([
                          element.tagName,
                          element.id,
                          element.getAttribute('type') || '',
                          element.getAttribute('name') || '',
                          element.getAttribute('value') || '',
                          element.getAttribute('data-automation-id') || '',
                          (element.textContent || '').replace(/\\s+/g, ' ').trim(),
                        ].join('|')),
                      };
                    }"""
                )
            except Exception:
                return None
            if expected_submit_token and current_submit.get("token") != expected_submit_token:
                return None
            if (
                expected_submit_fingerprint
                and current_submit.get("fingerprint") != expected_submit_fingerprint
            ):
                return None
        relative_selector = candidate.evaluate(
            """(element, selector) => {
              const root = document.querySelector(selector);
              if (!root || !root.contains(element)) return '';
              const esc = value => (window.CSS && CSS.escape)
                ? CSS.escape(value)
                : value.replace(/([^a-zA-Z0-9_-])/g, '\\\\$1');
              const index = node => {
                let n = 1, previous = node.previousElementSibling;
                while (previous) {
                  if (previous.tagName === node.tagName) n++;
                  previous = previous.previousElementSibling;
                }
                return n;
              };
              const parts = [];
              let node = element;
              while (node && node !== root) {
                parts.unshift(node.id
                  ? '#' + esc(node.id)
                  : node.tagName.toLowerCase() + ':nth-of-type(' + index(node) + ')');
                node = node.parentElement;
              }
              return parts.join(' > ');
            }""",
            handle.root_selector,
        )
        if not relative_selector:
            return None
        control_selector = f"{handle.root_selector} > {relative_selector}"
        form = root.first if root.first.evaluate("element => element.tagName") == "FORM" else root.first.locator("form").first
        action = form.get_attribute("action") if form.count() else ""
        method = (form.get_attribute("method") if form.count() else "post") or "post"
        destination = urljoin(page.url, action or page.url)
        # ``page_id`` is intentionally not control identity: a fresh review
        # and execution browser receive different owner-thread page IDs even
        # when they inspect the exact same form.  Keep page_id as a separate
        # SubmissionTarget equality guard, while deriving the authority-safe
        # control fingerprint from stable DOM/form evidence.
        inspected_submit_fingerprint = str(
            handle.evidence.get("submit_fingerprint", "") or ""
        )
        material = "\x1f".join(
            (
                handle.provider,
                handle.frame_url,
                handle.root_selector,
                control_selector,
                destination,
                method.upper(),
                inspected_submit_fingerprint,
            )
        )
        return SubmissionTarget(
            page_id=handle.page_id,
            frame_url=handle.frame_url,
            root_selector=handle.root_selector,
            provider=handle.provider,
            control_selector=control_selector,
            control_fingerprint=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            form_action=destination,
            method=method,
            destination=destination,
            evidence={"bound_to_verified_root": True},
        )

    def submit(self, page: Page) -> None:
        handle = getattr(self, "_last_form_handle", None)
        if handle is None:
            self.inspect_with_evidence(page)
            handle = getattr(self, "_last_form_handle", None)
        if handle is None:
            raise RuntimeError("No visible submission control found")
        target = self.submission_target(page, handle)
        if target is None:
            raise RuntimeError("No visible submission control found")
        page.locator(target.control_selector).click()
