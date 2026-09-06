(() => {
  const root = document.documentElement;
  const toastHost = document.querySelector('[data-toasts]');
  const sidebar = document.getElementById('sidebar');
  const overlay = document.querySelector('[data-overlay]');

  function toast(message, type = 'success') {
    if (!toastHost) return;
    const node = document.createElement('div');
    node.className = `toast ${type}`;
    const icon = document.createElement('span');
    icon.textContent = type === 'success' ? '✓' : '!';
    const messageNode = document.createElement('p');
    messageNode.textContent = message;
    node.append(icon, messageNode);
    toastHost.append(node);
    setTimeout(() => node.classList.add('show'), 10);
    setTimeout(() => {
      node.classList.remove('show');
      setTimeout(() => node.remove(), 250);
    }, 4200);
  }

  function toggleMenu(force) {
    const open = force ?? !sidebar?.classList.contains('open');
    sidebar?.classList.toggle('open', open);
    overlay?.classList.toggle('open', open);
    root.classList.toggle('menu-open', open);
    const menuButton = document.querySelector('[data-menu]');
    if (menuButton) {
      menuButton.setAttribute('aria-expanded', String(open));
      menuButton.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    }
  }
  document.querySelector('[data-menu]')?.addEventListener('click', () => toggleMenu());
  overlay?.addEventListener('click', () => toggleMenu(false));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && sidebar?.classList.contains('open')) toggleMenu(false);
  });

  function updateClock() {
    const clock = document.querySelector('[data-clock]');
    if (clock) clock.textContent = new Intl.DateTimeFormat('en-GB', {hour: '2-digit', minute: '2-digit', second: '2-digit'}).format(new Date());
  }
  updateClock(); setInterval(updateClock, 1000);

  function coerce(name, value) {
    if (['graduation_year', 'min_graduation_year', 'max_graduation_year', 'max_characters', 'max_applications'].includes(name)) {
      return value === '' ? undefined : Number(value);
    }
    if (['rolling', 'cv_required', 'cover_letter_required', 'written_answers_required', 'approved', 'sensitive', 'work_authorisation_approved'].includes(name)) {
      return value === true || value === 'true' || value === 'on';
    }
    if (['requires_sponsorship', 'sponsorship_supported'].includes(name)) {
      if (value === '') return undefined;
      return value === true || value === 'true' || value === 'yes';
    }
    return value === '' ? undefined : value;
  }

  function formJSON(form) {
    const output = {};
    const fields = Array.from(form.elements).filter(el => el.name && !el.disabled && el.type !== 'file');
    const checkboxNames = new Set(fields.filter(el => el.type === 'checkbox').map(el => el.name));
    for (const field of fields) {
      if (field.type === 'checkbox') {
        output[field.name] = Boolean(field.checked);
        continue;
      }
      if (field.type === 'radio' && !field.checked) continue;
      const value = coerce(field.name, field.value);
      if (value !== undefined) output[field.name] = value;
    }
    for (const name of checkboxNames) if (!(name in output)) output[name] = false;
    return output;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    const type = response.headers.get('content-type') || '';
    const payload = type.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = payload?.detail || payload?.message || (typeof payload === 'string' ? payload : `Request failed (${response.status})`);
      const error = new Error(detail);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  // Every mutating form gets an action-specific confirmation, including
  // upload forms.  Keeping the map keyed by the server action makes the
  // prompt resilient to template refactors while still leaving the copy
  // visible to the user at the moment the exact request is submitted.
  const formConfirmationCopy = [
    ['/api/answers', 'Store this exact answer-bank response for possible reuse in future applications?'],
    ['/api/documents', 'Store this exact document in the local vault? It may be selected for future application packages.'],
    ['/api/mail/ingest', 'Import this exact email locally? ARGUS will parse it and link any detected application or deadline; it will not send mail.'],
    ['/api/opportunities/import-csv', 'Import this exact CSV locally? Existing records will be matched by their source identity.'],
    ['/api/opportunities', 'Add this exact opportunity to the local inbox?'],
    ['/api/profile', 'Save these verified profile changes? Work-authorisation wording may be reused exactly.'],
    ['/api/conflict-rules', 'Store this conflict rule? It will block matching applications until reviewed.'],
    ['/api/applications/', 'Apply this recorded blocker resolution only to the exact application shown? ARGUS will rerun checks and never submit.'],
  ];
  document.querySelectorAll('form[data-api-form], form[data-upload-form]').forEach(form => {
    if (form.dataset.confirmForm) return;
    const action = String(form.getAttribute('action') || '');
    const match = formConfirmationCopy.find(([prefix]) => action.startsWith(prefix));
    if (match) form.dataset.confirmForm = match[1];
  });

  document.querySelectorAll('[data-api-form]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (form.dataset.confirmForm && !window.confirm(form.dataset.confirmForm)) return;
      const button = form.querySelector('[type="submit"]');
      const before = button?.textContent;
      if (button) { button.disabled = true; button.textContent = 'Working…'; }
      try {
        // NOTE: form.method (the IDL attribute) normalises invalid HTML
        // values like method="PUT" to "get" — which would send a JSON body
        // with a GET and throw. Read the raw attribute / data-method first;
        // fall back to the IDL value only for real get/post forms.
        const rawMethod = (
          form.dataset.method
          || form.getAttribute('method')
          || form.method
          || 'POST'
        ).toUpperCase();
        await api(form.action, {
          method: rawMethod,
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(formJSON(form)),
        });
        toast('Saved and written to the audit trail.');
        if (form.dataset.refresh === 'true') setTimeout(() => location.reload(), 500);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        if (button) { button.disabled = false; button.textContent = before; }
      }
    });
  });

  document.querySelectorAll('[data-upload-form]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (form.dataset.confirmForm && !window.confirm(form.dataset.confirmForm)) return;
      const button = form.querySelector('[type="submit"]');
      const before = button?.textContent;
      if (button) { button.disabled = true; button.textContent = 'Uploading…'; }
      try {
        await api(form.action, {method: 'POST', body: new FormData(form)});
        toast('File verified and stored locally.');
        if (form.dataset.refresh === 'true') setTimeout(() => location.reload(), 500);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        if (button) { button.disabled = false; button.textContent = before; }
      }
    });
  });

  document.querySelectorAll('form[data-confirm-form]:not([data-api-form]):not([data-upload-form])').forEach(form => {
    form.addEventListener('submit', event => {
      if (!window.confirm(form.dataset.confirmForm || 'Confirm this action?')) event.preventDefault();
    });
  });

  document.querySelectorAll('[data-action]').forEach(button => {
    button.addEventListener('click', async () => {
      const confirmation = button.dataset.confirm || `Confirm ${button.textContent.trim().toLowerCase()}?`;
      if (!window.confirm(confirmation)) return;
      const before = button.textContent;
      button.dataset.navigatorOriginalText = before;
      button.disabled = true; button.textContent = 'Running…';
      try {
        const result = await api(button.dataset.action, {method: button.dataset.method || 'POST'});
        const resolutionAction = isTargetResolutionAction(button.dataset.action);
        const targetResolutionHandoff = (
          resolutionAction
          && result?.human_handoff_required
          && bindTargetResolutionHandoff(button, result)
        );
        const reference = result?.receipt?.reference ? ` Receipt ${result.receipt.reference}.` : '';
        const risk = Number.isInteger(result?.risk_level) ? ` Risk ${result.risk_level}.` : '';
        const blocked = [
          ...(Array.isArray(result?.blocked_reasons) ? result.blocked_reasons : []),
          ...(Array.isArray(result?.reason_codes) ? result.reason_codes : []),
        ].map(value => String(value).trim()).filter(Boolean);
        const state = String(result?.state || '').replaceAll('_', ' ').trim();
        if (result?.human_handoff_required) {
          toast(result.next_action || 'Action paused: visible human verification is required.', 'error');
        } else if (result?.confirmation_required) {
          toast('Action paused: exact application confirmation is still required.', 'error');
        } else if (blocked.length || ['BLOCKED', 'NEEDS_USER', 'NEEDS_OA', 'SUBMISSION_UNKNOWN'].includes(String(result?.state || '').toUpperCase())) {
          const detail = blocked.length ? ` ${blocked.join('; ')}.` : '';
          toast(`Action recorded as ${state || 'blocked'}; no submission was claimed.${detail}`, 'error');
        } else if (state) {
          toast(`Action recorded: ${state}.${risk}${reference}`);
        } else {
          toast(`Action completed and recorded.${risk}${reference}`);
        }
        // A 202 target-resolution response is a live, resumable Navigator
        // handoff.  Keep the panel/session visible so Continue can re-enter
        // the same owner thread; reloading here would strand the user.
        if (resolutionAction && result?.human_handoff_required && !targetResolutionHandoff) {
          button.disabled = false;
          button.textContent = before;
        } else if (!targetResolutionHandoff) {
          setTimeout(() => location.reload(), 700);
        }
      } catch (error) {
        toast(error.message, 'error');
        button.disabled = false; button.textContent = before;
      }
    });
  });

  document.querySelectorAll('input[type="file"]').forEach(input => {
    input.addEventListener('change', () => {
      const zone = input.closest('.dropzone');
      const label = zone?.querySelector('span');
      if (label && input.files?.[0]) label.textContent = input.files[0].name;
    });
  });

  // --- Persistent local Application Navigator ---------------------------------
  // Apply controls intentionally carry only an application id and a server-
  // supplied verification flag.  They never contain a source/listing href.
  const navigatorTimers = new WeakMap();
  const navigatorResolutionTimers = new WeakMap();
  const navigatorFlowEpochs = new WeakMap();
  const navigatorManifests = new WeakMap();
  // Authorities are memory-only and disappear on reload.
  const navigatorAuthorities = new WeakMap();
  // A submit authority is deliberately one-shot in the browser as well as on
  // the server.  This closes the small synchronous window where two click
  // events could otherwise queue two identical requests before the first
  // fetch resolves.
  const navigatorSubmitInFlight = new WeakSet();
  const navigatorResolutionButtons = new WeakMap();
  const navigatorResolutionBoundaries = new WeakMap();
  const navigatorTerminal = new Set([
    'CONFIRMED',
    'UNKNOWN',
    'SUBMISSION_UNKNOWN',
    'CONFIRMATION_VERIFIED',
    'CANCELLED',
    'EXPIRED',
    'FAILED',
    'BLOCKED',
    'ERROR',
  ]);
  const navigatorFreshStartTerminal = new Set(['CANCELLED', 'EXPIRED', 'FAILED', 'ERROR']);
  const navigatorActive = new Set(['OPENING', 'ACTIVE', 'HUMAN_REQUIRED', 'FINAL_REVIEW', 'READY_TO_SUBMIT']);

  function navigatorStorageKey(applicationId) {
    return `argus:navigator:${applicationId}`;
  }

  function readNavigatorBinding(applicationId) {
    const expectedApplicationId = String(applicationId || '').trim();
    if (!expectedApplicationId) return null;
    let raw = '';
    try { raw = window.sessionStorage.getItem(navigatorStorageKey(expectedApplicationId)) || ''; } catch (_error) { return null; }
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        const storedApplicationId = String(parsed.application_id || '').trim();
        const sessionId = String(parsed.session_id || '').trim();
        if (!storedApplicationId || storedApplicationId !== expectedApplicationId || !sessionId) {
          window.sessionStorage.removeItem(navigatorStorageKey(expectedApplicationId));
          return null;
        }
        const mode = String(parsed.mode || '').trim().toLowerCase();
        return {
          applicationId: storedApplicationId,
          sessionId,
          sourceResolution: parsed.source_resolution === true || mode === 'source_resolution' || mode === 'source-resolution',
        };
      }
    } catch (_error) {
      // Legacy ARGUS builds stored only the opaque session id.  Keep that
      // value resumable, but never treat it as a source-resolution handoff.
    }
    const legacySessionId = String(raw).trim();
    return legacySessionId ? {
      applicationId: expectedApplicationId,
      sessionId: legacySessionId,
      sourceResolution: false,
    } : null;
  }

  function readNavigatorSession(applicationId) {
    return readNavigatorBinding(applicationId)?.sessionId || '';
  }

  function saveNavigatorBinding(applicationId, sessionId, sourceResolution = false) {
    const exactApplicationId = String(applicationId || '').trim();
    const exactSessionId = String(sessionId || '').trim();
    if (!exactApplicationId) return;
    try {
      if (exactSessionId) {
        window.sessionStorage.setItem(navigatorStorageKey(exactApplicationId), JSON.stringify({
          application_id: exactApplicationId,
          session_id: exactSessionId,
          mode: sourceResolution ? 'source_resolution' : 'navigator',
          source_resolution: Boolean(sourceResolution),
        }));
      } else {
        window.sessionStorage.removeItem(navigatorStorageKey(exactApplicationId));
      }
    } catch (_error) { /* Private browsing may deny storage; polling still works. */ }
  }

  function clearNavigatorTimer(timerMap, panel) {
    const timer = timerMap.get(panel);
    if (timer !== undefined && timer !== null) {
      window.clearTimeout(timer);
      timerMap.delete(panel);
    }
  }

  function invalidateNavigatorFlow(panel) {
    const next = (navigatorFlowEpochs.get(panel) || 0) + 1;
    navigatorFlowEpochs.set(panel, next);
    clearNavigatorTimer(navigatorTimers, panel);
    clearNavigatorTimer(navigatorResolutionTimers, panel);
    return next;
  }

  function beginNavigatorFlow(panel, applicationId, sessionId) {
    const epoch = invalidateNavigatorFlow(panel);
    panel.dataset.sessionId = String(sessionId);
    panel.dataset.applicationId = String(applicationId);
    return epoch;
  }

  function navigatorRequestCurrent(panel, applicationId, sessionId, epoch) {
    return Boolean(
      panel
      && String(panel.dataset.applicationId || '') === String(applicationId || '')
      && String(panel.dataset.sessionId || '') === String(sessionId || '')
      && navigatorFlowEpochs.get(panel) === epoch
    );
  }

  function reenableTargetResolutionButton(panel) {
    const button = navigatorResolutionButtons.get(panel);
    if (!button) return;
    button.disabled = false;
    if (button.dataset.navigatorOriginalText) button.textContent = button.dataset.navigatorOriginalText;
  }

  function clearNavigatorSession(panel) {
    const applicationId = panel?.dataset?.applicationId || '';
    if (applicationId) saveNavigatorBinding(applicationId, '', false);
    if (panel) {
      navigatorAuthorities.delete(panel);
      navigatorManifests.delete(panel);
      invalidateNavigatorFlow(panel);
      delete panel.dataset.sessionId;
      delete panel.dataset.sourceResolution;
      delete panel.dataset.navigatorConfirmed;
      delete panel.dataset.navigatorSubmitPending;
      navigatorResolutionBoundaries.delete(panel);
      panel.querySelectorAll(
        '[data-navigator-submit], [data-navigator-confirm], [data-navigator-manifest-request]'
      ).forEach(button => button.remove());
      const manifest = panel.querySelector('[data-navigator-manifest]');
      if (manifest) {
        clearChildren(manifest);
        manifest.hidden = true;
      }
      reenableTargetResolutionButton(panel);
    }
  }

  function navigatorPayloadMatches(panel, payload, expectedSessionId = '', options = {}) {
    const expectedApplicationId = String(panel?.dataset?.applicationId || '');
    const expectedSession = String(expectedSessionId || panel?.dataset?.sessionId || '');
    const currentSession = String(panel?.dataset?.sessionId || '');
    // A response from a superseded session is stale, not a reason to clear or
    // overwrite the newer panel state.
    if (expectedSession && currentSession && currentSession !== expectedSession) return false;
    const actualApplicationId = String(payload?.application_id || '');
    if (!expectedApplicationId || actualApplicationId !== expectedApplicationId) {
      if (expectedSession && currentSession && currentSession !== expectedSession) return false;
      clearNavigatorSession(panel);
      renderNavigatorError(panel, 'ERROR', 'Navigator session application identity mismatch; session cleared.');
      return false;
    }
    if (expectedSession) {
      const actualSession = String(payload?.session_id || '');
      if (!actualSession && options.allowMissingSession === true) return true;
      if (!actualSession || actualSession !== expectedSession) {
        if (currentSession && currentSession !== expectedSession) return false;
        clearNavigatorSession(panel);
        renderNavigatorError(panel, 'ERROR', 'Navigator session identity mismatch; session cleared.');
        return false;
      }
    }
    return true;
  }

  function navigatorPanel(applicationId, source) {
    const local = source?.closest?.('[data-navigator-panel]');
    if (local && local.dataset.applicationId === applicationId) return local;
    const panels = Array.from(document.querySelectorAll('[data-navigator-panel]'));
    return panels.find(panel => panel.dataset.applicationId === applicationId) || null;
  }

  function navigatorNodes(panel) {
    return {
      state: panel.querySelector('[data-navigator-state]'),
      status: panel.querySelector('[data-navigator-status]'),
      reasons: panel.querySelector('[data-navigator-reasons]'),
      manifest: panel.querySelector('[data-navigator-manifest]'),
      sessionLabel: panel.querySelector('[data-navigator-session-label]'),
      session: panel.querySelector('[data-navigator-session]'),
      continueButton: panel.querySelector('[data-navigator-continue]'),
      cancelButton: panel.querySelector('[data-navigator-cancel]'),
    };
  }

  function clearChildren(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function setNavigatorReasons(node, values) {
    if (!node) return;
    clearChildren(node);
    const reasons = Array.isArray(values) ? values.filter(value => String(value).trim()) : [];
    reasons.forEach(value => {
      const item = document.createElement('li');
      item.textContent = String(value);
      node.appendChild(item);
    });
    node.hidden = reasons.length === 0;
  }

  function manifestValue(manifest, summary, keys, fallback = 'Not provided') {
    for (const key of keys) {
      const value = manifest?.[key] ?? summary?.[key];
      if (value !== undefined && value !== null && String(value).trim()) return value;
    }
    return fallback;
  }

  const navigatorManifestRequired = [
    ['application_id', ['application_id']],
    ['employer', ['employer', 'company']],
    ['role', ['role', 'role_title']],
    ['provider', ['provider', 'resolved_ats_type']],
    ['target fingerprint', ['target_fingerprint', 'target_fingerprint_sha256']],
    ['form/control fingerprint', ['control_fingerprint', 'form_fingerprint']],
    ['form action', ['form_action', 'action']],
    ['method', ['method', 'form_method']],
    ['expiry', ['expires_at']],
    ['final URL', ['final_url']],
    ['expected receipt URL', ['expected_receipt_url', 'expected_final_url']],
  ];

  function navigatorManifestMissing(manifest, applicationId) {
    const missing = [];
    const valueFor = keys => keys.some(key => {
      const value = manifest?.[key];
      return value !== undefined && value !== null && String(value).trim();
    });
    navigatorManifestRequired.forEach(([label, keys]) => {
      if (!valueFor(keys)) missing.push(label);
    });
    const manifestId = String(manifest?.application_id || '').trim();
    if (applicationId && manifestId && manifestId !== String(applicationId)) missing.push('application ID mismatch');
    const provider = String(manifest?.provider || '').trim().toLowerCase();
    if (['greenhouse', 'lever', 'workday'].includes(provider) && !valueFor(['requisition', 'requisition_id', 'job_id'])) {
      missing.push('requisition');
    }
    const expiry = String(manifest?.expires_at || '').trim();
    if (expiry) {
      const timestamp = Date.parse(expiry);
      if (!Number.isFinite(timestamp)) missing.push('invalid expiry');
      else if (timestamp <= Date.now()) missing.push('expired');
    }
    return [...new Set(missing)];
  }

  function valueText(value) {
    if (Array.isArray(value)) {
      return value.map(item => {
        if (item && typeof item === 'object') {
          return Object.entries(item).map(([key, entry]) => `${key}: ${entry}`).join(' · ');
        }
        return String(item);
      }).join('; ') || 'None recorded';
    }
    if (value && typeof value === 'object') return Object.entries(value).map(([key, entry]) => `${key}: ${entry}`).join(' · ') || 'Not provided';
    return String(value ?? 'Not provided');
  }

  function renderNavigatorManifest(panel, payload, button) {
    const node = navigatorNodes(panel).manifest;
    if (!node) return;
    const manifest = payload?.manifest && typeof payload.manifest === 'object' ? payload.manifest : {};
    const summary = payload?.summary && typeof payload.summary === 'object' ? payload.summary : {};
    const appId = panel.dataset.applicationId || button?.dataset.applicationId || '';
    const rows = [
      ['Application ID', manifestValue(manifest, {}, ['application_id'])],
      ['Employer', manifestValue(manifest, {}, ['employer', 'company'])],
      ['Role', manifestValue(manifest, {}, ['role', 'role_title'])],
      ['Requisition', manifestValue(manifest, {}, ['requisition', 'requisition_id', 'job_id'])],
      ['Provider', manifestValue(manifest, {}, ['provider', 'resolved_ats_type'])],
      ['Target fingerprint', manifestValue(manifest, {}, ['target_fingerprint', 'target_fingerprint_sha256'])],
      ['Form/control fingerprint', manifestValue(manifest, {}, ['control_fingerprint', 'form_fingerprint'])],
      ['Form action', manifestValue(manifest, {}, ['form_action', 'action'])],
      ['Method', manifestValue(manifest, {}, ['method', 'form_method'])],
      ['Expiry', manifestValue(manifest, {}, ['expires_at'])],
      ['Final URL', manifestValue(manifest, {}, ['final_url'])],
      ['Expected receipt URL', manifestValue(manifest, {}, ['expected_receipt_url', 'expected_final_url'])],
      ['Destination', manifestValue(manifest, summary, ['destination', 'application_url', 'url'], 'Not provided')],
      ['Documents', manifestValue(manifest, summary, ['documents', 'document_manifest'])],
      ['Answers', manifestValue(manifest, summary, ['answers', 'answer_manifest'])],
    ];
    clearChildren(node);
    const heading = document.createElement('b');
    heading.textContent = 'Exact manifest — review before confirming';
    node.appendChild(heading);
    const list = document.createElement('dl');
    rows.forEach(([label, value]) => {
      const term = document.createElement('dt');
      term.textContent = label;
      const detail = document.createElement('dd');
      detail.textContent = valueText(value);
      list.append(term, detail);
    });
    node.appendChild(list);
    node.hidden = false;
    navigatorManifests.set(panel, {payload, button, manifest, appId, missing: navigatorManifestMissing(manifest, appId)});
  }

  function removeNavigatorConfirmation(panel) {
    panel.querySelectorAll('[data-navigator-confirm]').forEach(button => button.remove());
  }

  function removeNavigatorManifestRequest(panel) {
    panel.querySelectorAll('[data-navigator-manifest-request]').forEach(button => button.remove());
  }

  async function requestNavigatorManifest(panel) {
    const applicationId = panel.dataset.applicationId || '';
    const sessionId = panel.dataset.sessionId || readNavigatorSession(applicationId);
    const requestButton = panel.querySelector('[data-navigator-manifest-request]');
    if (!sessionId) return;
    const epoch = navigatorFlowEpochs.get(panel) || 0;
    if (requestButton) { requestButton.disabled = true; requestButton.textContent = 'Reviewing…'; }
    try {
      const payload = await api(`/api/handoff/sessions/${encodeURIComponent(sessionId)}/final-manifest`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({application_id: applicationId}),
      });
      if (!navigatorRequestCurrent(panel, applicationId, sessionId, epoch)) return;
      if (!navigatorPayloadMatches(panel, payload, sessionId)) return;
      renderNavigatorSession(panel, payload, null);
    } catch (error) {
      if (navigatorRequestCurrent(panel, applicationId, sessionId, epoch)) renderNavigatorError(panel, error.status === 409 ? 'BLOCKED' : 'ERROR', `Manifest request refused: ${error.message}`);
    } finally {
      if (requestButton) { requestButton.disabled = false; requestButton.textContent = 'Review exact manifest'; }
    }
  }

  async function confirmNavigatorManifest(panel) {
    let details = navigatorManifests.get(panel);
    if (!details || panel.dataset.navigatorConfirmed === 'true') return;
    const sessionId = panel.dataset.sessionId || readNavigatorSession(details.appId);
    const epoch = navigatorFlowEpochs.get(panel) || 0;
    const confirmButton = panel.querySelector('[data-navigator-confirm]');
    if (confirmButton) { confirmButton.disabled = true; confirmButton.textContent = 'Rechecking…'; }
    try {
      // Persist the complete server-produced binding at the same action-time
      // boundary as the confirmation dialog.  A FINAL_REVIEW snapshot reached
      // by polling is display evidence only until this endpoint durably binds
      // every stable form/control/document field for this exact session.
      const reviewed = await api(`/api/handoff/sessions/${encodeURIComponent(sessionId)}/final-manifest`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({application_id: details.appId}),
      });
      if (!navigatorRequestCurrent(panel, details.appId, sessionId, epoch)) return;
      if (!navigatorPayloadMatches(panel, reviewed, sessionId)) return;
      if (String(reviewed.state || reviewed.status || '').toUpperCase() !== 'FINAL_REVIEW') {
        renderNavigatorError(panel, 'ERROR', 'Confirmation refused: the exact final review is no longer current.');
        return;
      }
      renderNavigatorManifest(panel, reviewed, details.button);
      details = navigatorManifests.get(panel);
      if (!details) return;
    } catch (error) {
      if (navigatorRequestCurrent(panel, details.appId, sessionId, epoch)) {
        renderNavigatorError(panel, error.status === 409 ? 'BLOCKED' : 'ERROR', `Confirmation refused: ${error.message}`);
      }
      return;
    } finally {
      if (confirmButton) { confirmButton.disabled = false; confirmButton.textContent = 'Confirm this exact manifest'; }
    }
    const manifestApplicationId = details.manifest?.application_id;
    if (manifestApplicationId && String(manifestApplicationId) !== String(details.appId)) {
      renderNavigatorError(panel, 'ERROR', 'Exact application ID mismatch; confirmation refused.');
      return;
    }
    const missing = navigatorManifestMissing(details.manifest, details.appId);
    if (missing.length) {
      renderNavigatorError(panel, 'ERROR', `Confirmation refused: exact manifest is incomplete or stale (${missing.join(', ')}).`);
      return;
    }
    const employer = manifestValue(details.manifest, details.payload?.summary, ['employer', 'company'], details.button?.dataset.employer || panel.dataset.employer || 'the employer');
    const role = manifestValue(details.manifest, details.payload?.summary, ['role', 'role_title'], details.button?.dataset.role || panel.dataset.role || 'the role');
    const provider = manifestValue(details.manifest, {}, ['provider'], 'unknown provider');
    const requisition = manifestValue(details.manifest, {}, ['requisition', 'requisition_id', 'job_id'], 'unknown requisition');
    const destination = manifestValue(details.manifest, {}, ['destination', 'form_action'], 'unknown destination');
    const control = manifestValue(details.manifest, {}, ['control_fingerprint', 'form_fingerprint'], 'unknown control');
    const confirmation = `Confirm the exact ARGUS manifest for ${employer} — ${role} (application ${details.appId}; provider ${provider}; requisition ${requisition}; destination ${destination}; control ${control})? This confirms this session only; it does not claim a submit click.`;
    if (!window.confirm(confirmation)) return;
    if (confirmButton) { confirmButton.disabled = true; confirmButton.textContent = 'Confirming…'; }
    try {
      const payload = await api(`/api/handoff/sessions/${encodeURIComponent(sessionId)}/confirm`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({application_id: details.appId}),
      });
      if (!navigatorRequestCurrent(panel, details.appId, sessionId, epoch)) return;
      if (!navigatorPayloadMatches(panel, payload, sessionId)) return;
      panel.dataset.navigatorConfirmed = 'true';
      if (payload?.authority?.authority_id) navigatorAuthorities.set(panel, payload.authority);
      renderNavigatorSession(panel, payload, details.button);
    } catch (error) {
      if (navigatorRequestCurrent(panel, details.appId, sessionId, epoch)) renderNavigatorError(panel, 'ERROR', `Confirmation refused: ${error.message}`);
      if (confirmButton) { confirmButton.disabled = false; confirmButton.textContent = 'Confirm this exact manifest'; }
    }
  }

  async function submitNavigatorAuthority(panel) {
    if (navigatorSubmitInFlight.has(panel)) return;
    const authority = navigatorAuthorities.get(panel);
    const applicationId = String(panel.dataset.applicationId || '');
    const sessionId = String(panel.dataset.sessionId || '');
    if (!authority?.authority_id || !applicationId || !sessionId) return;
    if (
      (authority.application_id && String(authority.application_id) !== applicationId)
      || (authority.session_id && String(authority.session_id) !== sessionId)
    ) {
      navigatorAuthorities.delete(panel);
      renderNavigatorError(panel, 'ERROR', 'Submit authority identity mismatch; submission was not attempted.');
      return;
    }
    navigatorSubmitInFlight.add(panel);
    panel.dataset.navigatorSubmitPending = 'true';
    const button = panel.querySelector('[data-navigator-submit]');
    if (button) { button.disabled = true; button.textContent = 'Submitting…'; }
    try {
      const payload = await api(`/api/applications/${encodeURIComponent(applicationId)}/run?mode=submit&headed=true`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({application_id: applicationId, session_id: sessionId, authority_id: authority.authority_id}),
      });
      const returnedApplicationId = String(payload?.application_id || applicationId);
      const returnedSessionId = String(payload?.session_id || sessionId);
      if (returnedApplicationId !== applicationId || returnedSessionId !== sessionId) {
        throw new Error('Submit outcome identity did not match the confirmed application session');
      }
      const outcome = {
        ...(payload && typeof payload === 'object' ? payload : {}),
        application_id: returnedApplicationId,
        session_id: returnedSessionId,
        // A successful HTTP response without a terminal state is not proof of
        // a receipt.  Keep the browser honest and require reconciliation.
        state: String(payload?.state || 'SUBMISSION_UNKNOWN').toUpperCase(),
      };
      navigatorAuthorities.delete(panel);
      renderNavigatorSession(panel, outcome, null);
      clearNavigatorSession(panel);
    } catch (error) {
      if (button) { button.disabled = true; button.textContent = 'Submit unavailable'; }
      renderNavigatorError(panel, error.status === 409 ? 'BLOCKED' : 'UNKNOWN', `Submit outcome: ${error.message}. Do not retry automatically.`);
      navigatorAuthorities.delete(panel);
      // A failed/ambiguous request must not leave the confirmed session or a
      // disabled authority button recoverable from DOM/sessionStorage.  The
      // server remains authoritative about whether the one-shot token was
      // consumed; the browser always requires a fresh review before another
      // action.
      clearNavigatorSession(panel);
    } finally {
      navigatorSubmitInFlight.delete(panel);
    }
  }

  function boundaryCapability(boundary, capability) {
    const capabilities = boundary?.capabilities && typeof boundary.capabilities === 'object' ? boundary.capabilities : {};
    const keys = capability === 'continue'
      ? ['can_continue', 'continue', 'canContinue']
      : ['can_cancel', 'cancel', 'canCancel'];
    for (const key of keys) {
      if (typeof boundary?.[key] === 'boolean') return boundary[key];
      if (typeof capabilities[key] === 'boolean') return capabilities[key];
    }
    return false;
  }

  function boundaryExpiryAllows(boundary, payload) {
    const expiry = String(boundary?.expires_at || payload?.expires_at || '').trim();
    const timestamp = Date.parse(expiry);
    return Boolean(expiry && Number.isFinite(timestamp) && timestamp > Date.now());
  }

  function humanBoundaryAllows(payload, capability, state) {
    const boundary = payload?.human_boundary && typeof payload.human_boundary === 'object' ? payload.human_boundary : {};
    if (state === 'HUMAN_REQUIRED' && !Object.keys(boundary).length) return false;
    return boundaryCapability(boundary, capability)
      && boundary.resumable === true
      && boundaryExpiryAllows(boundary, payload);
  }

  function navigatorCancelAllowed(payload, state) {
    if (!navigatorActive.has(state) || state === 'FINAL_REVIEW') return false;
    const boundary = payload?.human_boundary && typeof payload.human_boundary === 'object' ? payload.human_boundary : {};
    if (Object.keys(boundary).length) return humanBoundaryAllows(payload, 'cancel', state);
    return payload?.worker_alive === true && boundaryExpiryAllows({}, payload);
  }

  function sourceBoundaryForPayload(panel, payload, state) {
    const incoming = payload?.human_boundary && typeof payload.human_boundary === 'object' ? payload.human_boundary : {};
    const saved = panel.dataset.sourceResolution === 'true' ? navigatorResolutionBoundaries.get(panel) : null;
    if (!saved || state !== 'HUMAN_REQUIRED') return incoming;
    const merged = {...incoming};
    for (const key of ['can_continue', 'can_cancel', 'resumable']) {
      if (typeof saved[key] === 'boolean') {
        merged[key] = typeof incoming[key] === 'boolean' ? saved[key] && incoming[key] : saved[key];
      }
    }
    const savedExpiry = Date.parse(String(saved.expires_at || ''));
    const incomingExpiry = Date.parse(String(incoming.expires_at || ''));
    if (Number.isFinite(savedExpiry) && Number.isFinite(incomingExpiry)) {
      merged.expires_at = new Date(Math.min(savedExpiry, incomingExpiry)).toISOString();
    } else if (Number.isFinite(savedExpiry) && !incoming.expires_at) {
      merged.expires_at = saved.expires_at;
    }
    return merged;
  }

  function renderNavigatorSession(panel, payload, button) {
    if (!payload || !panel) return;
    panel.hidden = false;
    const tableRow = panel.closest('tr.navigator-row');
    if (tableRow) tableRow.hidden = false;
    const nodes = navigatorNodes(panel);
    const state = String(payload.state || payload.status || 'ERROR').toUpperCase();
    const summary = payload.summary && typeof payload.summary === 'object' ? payload.summary : {};
    const boundary = sourceBoundaryForPayload(panel, payload, state);
    const affordancePayload = boundary === payload.human_boundary ? payload : {...payload, human_boundary: boundary};
    const reason = String(payload.reason || boundary.reason || payload.outcome || '').trim();
    if (nodes.state) { nodes.state.textContent = state.replaceAll('_', ' '); nodes.state.dataset.state = state; }
    if (nodes.session && payload.session_id) {
      nodes.session.textContent = String(payload.session_id);
      nodes.session.dataset.navigatorSession = String(payload.session_id);
      panel.dataset.sessionId = String(payload.session_id);
      if (nodes.sessionLabel) nodes.sessionLabel.hidden = false;
    }
    setNavigatorReasons(nodes.reasons, reason ? [reason] : []);
    if (state === 'HUMAN_REQUIRED') {
      if (nodes.status) nodes.status.textContent = `Human action required: ${reason || 'complete the challenge in the same browser session'}. Continue rescans this session; it never submits.`;
    } else if (state === 'FINAL_REVIEW' || state === 'READY_TO_SUBMIT') {
      const manifestApplicationId = payload?.manifest?.application_id;
      if (manifestApplicationId && String(manifestApplicationId) !== String(panel.dataset.applicationId || '')) {
        clearNavigatorSession(panel);
        renderNavigatorError(panel, 'ERROR', 'Exact application ID mismatch; confirmation refused.');
        return;
      }
      const manifestMissing = navigatorManifestMissing(payload?.manifest && typeof payload.manifest === 'object' ? payload.manifest : {}, panel.dataset.applicationId || '');
      if (manifestMissing.length) {
        if (nodes.status) nodes.status.textContent = `Confirmation unavailable: exact manifest is incomplete or stale (${manifestMissing.join(', ')}).`;
        renderNavigatorManifest(panel, payload, button);
        removeNavigatorConfirmation(panel);
      } else if (nodes.status) {
        nodes.status.textContent = 'Exact manifest ready. Review employer, role, provider, destination, application ID, and evidence before the one-time confirmation.';
      }
      renderNavigatorManifest(panel, payload, button);
    } else if (state === 'CONFIRMED') {
      const authority = navigatorAuthorities.get(panel);
      if (authority?.authority_id && String(authority.session_id) === String(payload.session_id) && String(authority.application_id) === String(panel.dataset.applicationId || '')) {
        if (nodes.status) nodes.status.textContent = `Manifest confirmed; no submit click has occurred. One-time submit authority expires ${authority.expires_at}. Review the exact identity above before submitting.`;
        removeNavigatorConfirmation(panel);
        if (!panel.querySelector('[data-navigator-submit]')) {
          const submitButton = document.createElement('button');
          submitButton.className = 'button compact danger';
          submitButton.type = 'button';
          submitButton.dataset.navigatorSubmit = 'true';
          submitButton.textContent = 'Submit this exact application once';
          submitButton.addEventListener('click', () => submitNavigatorAuthority(panel));
          panel.querySelector('.navigator-controls')?.appendChild(submitButton);
        }
      } else {
        if (nodes.status) nodes.status.textContent = reason || 'Manifest confirmed; no submit authority is available after reload or terminal state.';
        panel.querySelectorAll('[data-navigator-submit]').forEach(button => button.remove());
      }
    } else if (state === 'CONFIRMATION_VERIFIED') {
      if (nodes.status) nodes.status.textContent = 'Submission confirmed by the exact receipt. No further submit is available.';
      removeNavigatorConfirmation(panel);
      panel.querySelectorAll('[data-navigator-submit]').forEach(button => button.remove());
    } else if (state === 'UNKNOWN' || state === 'SUBMISSION_UNKNOWN') {
      if (nodes.status) nodes.status.textContent = `Outcome unknown: ${reason || 'reconcile the employer record before any retry'}. Do not retry.`;
      removeNavigatorConfirmation(panel);
      panel.querySelectorAll('[data-navigator-submit]').forEach(button => button.remove());
    } else if (state === 'CANCELLED' || state === 'EXPIRED' || state === 'FAILED' || state === 'BLOCKED' || state === 'ERROR') {
      if (nodes.status) nodes.status.textContent = `${state === 'BLOCKED' ? 'Navigator blocked' : `Navigator ${state.toLowerCase()}`}: ${reason || 'no application action was issued'}`;
      removeNavigatorConfirmation(panel);
    } else {
      if (nodes.status) nodes.status.textContent = `Navigator ${state.toLowerCase()}; no submission has been issued.`;
      removeNavigatorConfirmation(panel);
    }
    if (nodes.continueButton) nodes.continueButton.hidden = state !== 'HUMAN_REQUIRED' || !humanBoundaryAllows(affordancePayload, 'continue', state);
    if (nodes.cancelButton) nodes.cancelButton.hidden = !navigatorCancelAllowed(affordancePayload, state);
    if (state === 'ACTIVE' || state === 'READY_TO_SUBMIT') {
      if (!panel.querySelector('[data-navigator-manifest-request]')) {
        const manifestButton = document.createElement('button');
        manifestButton.className = 'button compact ghost';
        manifestButton.type = 'button';
        manifestButton.dataset.navigatorManifestRequest = 'true';
        manifestButton.textContent = 'Review exact manifest';
        manifestButton.addEventListener('click', () => requestNavigatorManifest(panel));
        panel.querySelector('.navigator-controls')?.appendChild(manifestButton);
      }
    } else {
      removeNavigatorManifestRequest(panel);
    }
    const payloadManifestMissing = navigatorManifestMissing(
      payload?.manifest && typeof payload.manifest === 'object' ? payload.manifest : {},
      panel.dataset.applicationId || '',
    );
    if ((state === 'FINAL_REVIEW' || state === 'READY_TO_SUBMIT') && !payloadManifestMissing.length && !panel.querySelector('[data-navigator-confirm]')) {
      const confirmButton = document.createElement('button');
      confirmButton.className = 'button compact primary';
      confirmButton.type = 'button';
      confirmButton.dataset.navigatorConfirm = 'true';
      confirmButton.textContent = 'Confirm this exact manifest';
      confirmButton.addEventListener('click', () => confirmNavigatorManifest(panel));
      panel.querySelector('.navigator-controls')?.appendChild(confirmButton);
    }
    const memoryAuthorityDisplayed = state === 'CONFIRMED'
      && (() => { const authority = navigatorAuthorities.get(panel); return Boolean(authority?.authority_id && String(authority.session_id) === String(payload.session_id) && String(authority.application_id) === String(panel.dataset.applicationId || '')); })();
    if (navigatorTerminal.has(state) && !memoryAuthorityDisplayed) clearNavigatorSession(panel);
    panel.dispatchEvent(new CustomEvent('argus:navigator-state', {
      detail: {state, reason},
    }));
  }

  function renderNavigatorError(panel, state, message) {
    renderNavigatorSession(panel, {state, status: state, reason: message, summary: {}}, null);
  }

  function isTargetResolutionAction(url) {
    return /\/resolve-target(?:$|\?)/.test(String(url || ''));
  }

  function targetResolutionApplicationId(url) {
    const match = String(url || '').match(/\/api\/applications\/([^/?]+)\/resolve-target(?:$|\?)/);
    if (!match) return '';
    try { return decodeURIComponent(match[1]); } catch (_error) { return ''; }
  }

  function bindTargetResolutionHandoff(button, payload) {
    // Applications-page Resolve controls predate the panel binding and do
    // not carry a data-application-id.  Derive only the opaque route id (never
    // a URL destination) so the four identity checks remain exact.
    const buttonApplicationId = String(
      button?.dataset?.applicationId || targetResolutionApplicationId(button?.dataset?.action),
    ).trim();
    const panel = navigatorPanel(buttonApplicationId, button);
    const panelApplicationId = String(panel?.dataset?.applicationId || '').trim();
    const payloadApplicationId = String(payload?.application_id || '').trim();
    const handoff = payload?.handoff && typeof payload.handoff === 'object' ? payload.handoff : {};
    const handoffApplicationId = String(handoff.application_id || '').trim();
    const applicationIds = [buttonApplicationId, panelApplicationId, payloadApplicationId];
    if (handoffApplicationId) applicationIds.push(handoffApplicationId);
    const identitiesMatch = applicationIds.every(value => value && value === buttonApplicationId);
    const handoffSessionId = String(handoff.session_id || '').trim();
    const payloadSessionId = String(payload?.session_id || '').trim();
    const sessionConsistent = Boolean(handoffSessionId) && (!payloadSessionId || payloadSessionId === handoffSessionId);
    const existingBinding = panel ? readNavigatorBinding(buttonApplicationId) : null;
    const existingSessionId = String(panel?.dataset?.sessionId || existingBinding?.sessionId || '').trim();
    if (!panel || !identitiesMatch || !sessionConsistent || (existingSessionId && existingSessionId !== handoffSessionId)) {
      if (panel) {
        clearNavigatorSession(panel);
        renderNavigatorError(panel, 'ERROR', 'Source-resolution application/session binding was inconsistent; retry was refused.');
      }
      if (button) {
        button.disabled = false;
        if (button.dataset.navigatorOriginalText) button.textContent = button.dataset.navigatorOriginalText;
      }
      return false;
    }
    panel.dataset.sourceResolution = 'true';
    navigatorResolutionButtons.set(panel, button);
    navigatorResolutionBoundaries.set(panel, {...handoff});
    saveNavigatorBinding(buttonApplicationId, handoffSessionId, true);
    renderNavigatorSession(panel, {
      session_id: handoffSessionId,
      application_id: buttonApplicationId,
      state: handoff.state || 'HUMAN_REQUIRED',
      reason: handoff.reason || payload.next_action || 'Visible source inspection is required.',
      summary: {source_resolution: true},
      human_boundary: handoff,
    }, button);
    reenableTargetResolutionButton(panel);
    const epoch = beginNavigatorFlow(panel, buttonApplicationId, handoffSessionId);
    pollNavigator(panel, handoffSessionId, button, epoch);
    return true;
  }

  function pollTargetResolution(panel, applicationId, sessionId, button, attempt = 0, epoch = null) {
    const flowEpoch = epoch ?? beginNavigatorFlow(panel, applicationId, sessionId);
    const tick = async (currentAttempt = attempt) => {
      if (!navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) return;
      if (currentAttempt >= 80) {
        renderNavigatorSession(panel, {
          session_id: sessionId,
          application_id: applicationId,
          state: 'HUMAN_REQUIRED',
          reason: 'Source resolution is still waiting for the visible Navigator; press Continue to retry.',
          summary: {source_resolution: true},
          human_boundary: {
            application_id: applicationId,
            session_id: sessionId,
            can_continue: true,
            can_cancel: true,
            resumable: true,
            expires_at: new Date(Date.now() + 60_000).toISOString(),
          },
        }, button);
        return;
      }
      try {
        // The resolver accepts only the exact application binding.  No URL or
        // destination from the page is sent by the browser UI.
        const payload = await api(
          `/api/applications/${encodeURIComponent(applicationId)}/resolve-target`,
          {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({application_id: applicationId, confirmed: true}),
          },
        );
        if (!navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) return;
        const promoted = payload?.promoted === true && payload?.application_url;
        const handoff = payload?.handoff && typeof payload.handoff === 'object' ? payload.handoff : {};
        const handoffApplicationId = String(handoff.application_id || '').trim();
        const handoffSessionId = String(handoff.session_id || '').trim();
        // Resolution responses carry the exact session in the handoff
        // envelope; promotion responses may legitimately omit the envelope.
        // Validate the envelope-derived session before rendering either case.
        const responseSessionId = String(payload?.session_id || handoffSessionId || '').trim();
        const payloadForBinding = responseSessionId && !payload?.session_id
          ? {...payload, session_id: responseSessionId}
          : payload;
        if (!navigatorPayloadMatches(panel, payloadForBinding, sessionId, {allowMissingSession: Boolean(promoted && !responseSessionId)})) return;
        if ((handoffApplicationId && handoffApplicationId !== applicationId) || (handoffSessionId && handoffSessionId !== sessionId)) {
          clearNavigatorSession(panel);
          renderNavigatorError(panel, 'ERROR', 'Source-resolution session binding changed; retry was refused.');
          return;
        }
        if (promoted) {
          clearNavigatorSession(panel);
          toast('Exact application destination verified. ARGUS is refreshing the application view.');
          window.setTimeout(() => location.reload(), 400);
          return;
        }
        if (!handoffSessionId) {
          clearNavigatorSession(panel);
          renderNavigatorError(panel, 'ERROR', 'Source-resolution response omitted its exact session binding.');
          return;
        }
        renderNavigatorSession(panel, {
          session_id: sessionId,
          application_id: applicationId,
          state: handoff.state || 'HUMAN_REQUIRED',
          reason: handoff.reason || payload.next_action || 'Visible source inspection is required.',
          summary: {source_resolution: true},
          human_boundary: handoff,
        }, button);
        const state = String(handoff.state || handoff.status || '').toUpperCase();
        if (navigatorTerminal.has(state) || !navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) return;
        const timer = window.setTimeout(() => tick(currentAttempt + 1), 500);
        navigatorResolutionTimers.set(panel, timer);
      } catch (error) {
        if (navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) renderNavigatorError(panel, error.status === 409 ? 'BLOCKED' : 'ERROR', `Source resolution refused: ${error.message}`);
      }
    };
    tick(attempt);
  }

  function pollNavigator(panel, sessionId, button, epoch = null) {
    const applicationId = String(panel?.dataset?.applicationId || '');
    if (!applicationId || !sessionId) return;
    const flowEpoch = epoch ?? beginNavigatorFlow(panel, applicationId, sessionId);
    const tick = async () => {
      if (!navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) return;
      try {
        const payload = await api(`/api/handoff/sessions/${encodeURIComponent(sessionId)}`);
        if (!navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) return;
        if (!navigatorPayloadMatches(panel, payload, sessionId)) return;
        renderNavigatorSession(panel, payload, button);
        const state = String(payload.state || payload.status || '').toUpperCase();
        if (!navigatorTerminal.has(state) && navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) {
          const timer = window.setTimeout(tick, state === 'HUMAN_REQUIRED' ? 1200 : 500);
          navigatorTimers.set(panel, timer);
        }
      } catch (error) {
        if (!navigatorRequestCurrent(panel, applicationId, sessionId, flowEpoch)) return;
        if (error.status === 404) clearNavigatorSession(panel);
        const status = error.status ? ` (${error.status})` : '';
        renderNavigatorError(panel, 'ERROR', `Navigator status unavailable${status}: ${error.message}`);
      }
    };
    tick();
  }

  async function recoverNavigatorSession(panel, applicationId, button, originalError) {
    const known = readNavigatorBinding(applicationId);
    if (known) {
      panel.dataset.sourceResolution = known.sourceResolution ? 'true' : '';
      const epoch = beginNavigatorFlow(panel, applicationId, known.sessionId);
      if (known.sourceResolution) pollTargetResolution(panel, applicationId, known.sessionId, button, 0, epoch);
      else pollNavigator(panel, known.sessionId, button, epoch);
      return;
    }
    try {
      const sessions = await api('/api/handoff/sessions');
      const found = Array.isArray(sessions) ? sessions.find(item => String(item.application_id || '') === String(applicationId) && !navigatorTerminal.has(String(item.state || '').toUpperCase())) : null;
      if (found?.session_id) {
        saveNavigatorBinding(applicationId, found.session_id, false);
        pollNavigator(panel, found.session_id, button);
        return;
      }
    } catch (_error) { /* Preserve the original refusal below. */ }
    renderNavigatorError(panel, originalError?.status === 409 ? 'BLOCKED' : 'ERROR', originalError?.message || 'Navigator could not start');
  }

  async function startNavigator(button) {
    if (button.dataset.navigatorVerified !== 'true') return;
    const applicationId = String(button.dataset.applicationId || '');
    if (!applicationId) return;
    if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
    const panel = navigatorPanel(applicationId, button);
    if (!panel) return;
    panel.hidden = false;
    ['employer', 'role', 'provider'].forEach(key => {
      if (button.dataset[key]) panel.dataset[key] = button.dataset[key];
    });
    button.disabled = true;
    const before = button.textContent;
    button.textContent = 'Opening…';
    const known = readNavigatorBinding(applicationId);
    try {
      if (known) {
        panel.dataset.sourceResolution = known.sourceResolution ? 'true' : '';
        const epoch = beginNavigatorFlow(panel, applicationId, known.sessionId);
        if (known.sourceResolution) pollTargetResolution(panel, applicationId, known.sessionId, button, 0, epoch);
        else pollNavigator(panel, known.sessionId, button, epoch);
        return;
      }
      // A usable final review must be produced by the real adapter journey;
      // a bare browser-only handoff has no inspected form or exact submit
      // target.  The review run may stop at FINAL_REVIEW or a human boundary,
      // and both outcomes retain the same owner-thread session for polling.
      const payload = await api(`/api/applications/${encodeURIComponent(applicationId)}/run?mode=prefill&headed=true`, {method: 'POST'});
      const sessionId = String(payload.session_id || payload.handoff_session_id || '');
      if (!sessionId) throw new Error('Navigator did not return a session id');
      if (!navigatorPayloadMatches(panel, payload, sessionId)) return;
      saveNavigatorBinding(applicationId, sessionId, false);
      renderNavigatorSession(panel, payload, button);
      const epoch = beginNavigatorFlow(panel, applicationId, sessionId);
      pollNavigator(panel, sessionId, button, epoch);
    } catch (error) {
      await recoverNavigatorSession(panel, applicationId, button, error);
    } finally {
      button.disabled = false;
      button.textContent = before;
    }
  }

  async function navigatorContinue(button) {
    const panel = button.closest('[data-navigator-panel]');
    const applicationId = panel?.dataset.applicationId;
    const binding = readNavigatorBinding(applicationId || '');
    const sessionId = panel?.dataset?.sessionId || binding?.sessionId || '';
    if (!panel || !applicationId || !sessionId) return;
    const sourceResolution = panel.dataset.sourceResolution === 'true' || binding?.sourceResolution === true;
    const epoch = navigatorFlowEpochs.get(panel) || 0;
    button.disabled = true;
    try {
      if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
      const payload = await api(`/api/handoff/sessions/${encodeURIComponent(sessionId)}/continue`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({application_id: applicationId}),
      });
      if (!navigatorRequestCurrent(panel, applicationId, sessionId, epoch)) return;
      if (!navigatorPayloadMatches(panel, payload, sessionId)) return;
      renderNavigatorSession(panel, payload, null);
      const state = String(payload.state || payload.status || '').toUpperCase();
      if (navigatorTerminal.has(state)) return;
      const nextEpoch = beginNavigatorFlow(panel, applicationId, sessionId);
      if (sourceResolution) pollTargetResolution(panel, applicationId, sessionId, null, 0, nextEpoch);
      else pollNavigator(panel, sessionId, null, nextEpoch);
    } catch (error) {
      if (navigatorRequestCurrent(panel, applicationId, sessionId, epoch)) renderNavigatorError(panel, error.status === 409 ? 'BLOCKED' : 'ERROR', `Continue refused: ${error.message}`);
    } finally { button.disabled = false; }
  }

  async function navigatorCancel(button) {
    const panel = button.closest('[data-navigator-panel]');
    const applicationId = panel?.dataset.applicationId;
    const binding = readNavigatorBinding(applicationId || '');
    const sessionId = panel?.dataset?.sessionId || binding?.sessionId || '';
    if (!panel || !applicationId || !sessionId) return;
    const epoch = navigatorFlowEpochs.get(panel) || 0;
    button.disabled = true;
    try {
      if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
      const payload = await api(`/api/handoff/sessions/${encodeURIComponent(sessionId)}/cancel`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({application_id: applicationId}),
      });
      if (!navigatorRequestCurrent(panel, applicationId, sessionId, epoch)) return;
      if (!navigatorPayloadMatches(panel, payload, sessionId)) return;
      renderNavigatorSession(panel, payload, null);
    } catch (error) {
      if (navigatorRequestCurrent(panel, applicationId, sessionId, epoch)) renderNavigatorError(panel, error.status === 409 ? 'BLOCKED' : 'ERROR', `Cancel refused: ${error.message}`);
    } finally { button.disabled = false; }
  }

  document.querySelectorAll('[data-navigator-start]').forEach(button => {
    button.addEventListener('click', () => startNavigator(button));
  });
  document.querySelectorAll('[data-navigator-continue]').forEach(button => {
    button.addEventListener('click', () => navigatorContinue(button));
  });
  document.querySelectorAll('[data-navigator-cancel]').forEach(button => {
    button.addEventListener('click', () => navigatorCancel(button));
  });

  // Resume a session after a page refresh only when a matching local binding
  // exists.  No URL is read from storage and no source page is opened.
  document.querySelectorAll('[data-navigator-panel][data-application-id]').forEach(panel => {
    if (new Set(['NEEDS_OA', 'SUBMISSION_UNKNOWN', 'CONFIRMATION_VERIFIED', 'SUBMITTED', 'BLOCKED']).has(String(panel.dataset.applicationState || '').toUpperCase())) return;
    const binding = readNavigatorBinding(panel.dataset.applicationId || '') || {sourceResolution: false, sessionId: ''};
    // The server-rendered session is authoritative when the service still
    // owns a live browser. Storage is only a recovery hint for legacy pages;
    // this prevents a stale Continue control from surviving a closed browser.
    const serverSessionId = String(panel.dataset.sessionId || '').trim();
    const sessionId = serverSessionId || binding?.sessionId || '';
    if (!sessionId) return;
    panel.hidden = false;
    const sourceResolution = !serverSessionId && binding?.sourceResolution === true;
    panel.dataset.sourceResolution = sourceResolution ? 'true' : '';
    const epoch = beginNavigatorFlow(panel, panel.dataset.applicationId || '', sessionId);
    if (binding.sourceResolution) {
      if (sourceResolution) {
        pollTargetResolution(panel, panel.dataset.applicationId || '', binding.sessionId, null, 0, epoch);
      } else {
        pollNavigator(panel, sessionId, document.querySelector(`[data-navigator-start][data-application-id="${panel.dataset.applicationId}"]`), epoch);
      }
    } else {
      pollNavigator(panel, sessionId, document.querySelector(`[data-navigator-start][data-application-id="${panel.dataset.applicationId}"]`), epoch);
    }
  });
})();
