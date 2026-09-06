(() => {
  'use strict';

  const root = document.documentElement;
  const sidebar = document.querySelector('[data-sidebar]');
  const menuButton = document.querySelector('[data-menu-toggle]');
  const shortcutDialog = document.querySelector('[data-shortcuts]');
  const toastRegion = document.querySelector('[data-toasts]');
  const globalSearch = document.querySelector('[data-global-search]');

  function toast(message, type = 'success') {
    if (!toastRegion) return;
    const node = document.createElement('div');
    node.className = `toast-v2 ${type}`;
    node.textContent = String(message);
    toastRegion.append(node);
    window.setTimeout(() => node.remove(), 5000);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, options);
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json')
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const message = payload?.message || payload?.detail || (typeof payload === 'string' ? payload : `Request failed (${response.status})`);
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function setBusy(button, busy, label = 'Working…') {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.textContent;
      button.textContent = label;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
    } else {
      button.textContent = button.dataset.originalLabel || button.textContent;
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }

  function coerceField(field) {
    if (field.type === 'checkbox') return Boolean(field.checked);
    if (field.type === 'number') return field.value === '' ? undefined : Number(field.value);
    if (['requires_sponsorship', 'sponsorship_supported'].includes(field.name)) {
      if (field.value === '') return undefined;
      return field.value === 'true' || field.value === 'yes';
    }
    return field.value === '' ? undefined : field.value;
  }

  function formJSON(form) {
    const output = {};
    for (const field of Array.from(form.elements)) {
      if (!field.name || field.disabled || field.type === 'file') continue;
      if (field.type === 'radio' && !field.checked) continue;
      const value = coerceField(field);
      if (value !== undefined) output[field.name] = value;
    }
    return output;
  }

  function closeNavigation() {
    sidebar?.classList.remove('open');
    menuButton?.setAttribute('aria-expanded', 'false');
  }

  menuButton?.addEventListener('click', () => {
    const open = !sidebar?.classList.contains('open');
    sidebar?.classList.toggle('open', open);
    menuButton.setAttribute('aria-expanded', String(open));
  });

  const storedTheme = window.localStorage.getItem('argus-theme');
  if (storedTheme === 'light' || storedTheme === 'dark') root.dataset.theme = storedTheme;
  const themeButton = document.querySelector('[data-theme-toggle]');
  function updateThemeButton() {
    if (themeButton) themeButton.textContent = root.dataset.theme === 'light' ? 'Dark theme' : 'Light theme';
  }
  updateThemeButton();
  themeButton?.addEventListener('click', () => {
    root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
    window.localStorage.setItem('argus-theme', root.dataset.theme);
    updateThemeButton();
  });

  document.querySelector('[data-shortcuts-open]')?.addEventListener('click', () => shortcutDialog?.showModal());

  let awaitingJump = false;
  let jumpTimer = null;
  document.addEventListener('keydown', event => {
    const target = event.target;
    const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target?.isContentEditable;
    if (event.key === 'Escape') {
      if (shortcutDialog?.open) shortcutDialog.close();
      closeNavigation();
      awaitingJump = false;
      return;
    }
    if (typing) return;
    if (event.key === '/') {
      event.preventDefault();
      globalSearch?.focus();
      return;
    }
    if (event.key === '?') {
      event.preventDefault();
      shortcutDialog?.showModal();
      return;
    }
    // Press g then a documented section key to jump without a pointer.
    if (event.key.toLowerCase() === 'g') {
      awaitingJump = true;
      window.clearTimeout(jumpTimer);
      jumpTimer = window.setTimeout(() => { awaitingJump = false; }, 1200);
      return;
    }
    if (awaitingJump) {
      const destination = document.querySelector(`[data-jump-key="${event.key.toLowerCase()}"]`);
      awaitingJump = false;
      window.clearTimeout(jumpTimer);
      if (destination) {
        event.preventDefault();
        window.location.assign(destination.href);
      }
    }
  });

  document.querySelectorAll('[data-api-form][data-confirm-form], [data-api-form]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const confirmation = form.dataset.confirmForm;
      if (confirmation && !window.confirm(confirmation)) return;
      const button = form.querySelector('[type="submit"]');
      setBusy(button, true);
      try {
        const method = (form.dataset.method || form.getAttribute('method') || 'POST').toUpperCase();
        const result = await api(form.action, {
          method,
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(formJSON(form)),
        });
        if (form.matches('[data-manual-target-form]')) {
          const verification = result?.verification || {};
          const kind = result?.target_kind || verification.target_kind || 'target';
          const message = result?.message || (
            result?.promoted
              ? `Verified target (${kind}). ${result?.application_advanced ? 'Application advanced.' : 'Application still needs review.'}`
              : `Target verification failed at ${verification.failed_check || kind}. The application remains BLOCKED.`
          );
          toast(message, result?.promoted ? 'success' : 'error');
          if (form.dataset.refresh === 'true') window.setTimeout(() => window.location.reload(), 1200);
        } else {
          toast('Saved locally and passed to the server audit boundary.');
          if (form.dataset.refresh === 'true') window.setTimeout(() => window.location.reload(), 350);
        }
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        setBusy(button, false);
      }
    });
  });

  document.querySelectorAll('[data-upload-form]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const confirmation = form.dataset.confirmForm;
      if (confirmation && !window.confirm(confirmation)) return;
      const button = form.querySelector('[type="submit"]');
      setBusy(button, true, 'Uploading…');
      try {
        await api(form.action, {method: 'POST', body: new FormData(form)});
        toast('Verified file stored locally.');
        if (form.dataset.refresh === 'true') window.setTimeout(() => window.location.reload(), 350);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        setBusy(button, false);
      }
    });
  });

  document.querySelectorAll('body:not([data-legacy-workflows]) [data-action][data-confirm], body:not([data-legacy-workflows]) [data-action]').forEach(button => {
    button.addEventListener('click', async () => {
      const confirmation = button.dataset.confirm || `Confirm ${button.textContent.trim()}?`;
      if (!window.confirm(confirmation)) return;
      setBusy(button, true);
      try {
        await api(button.dataset.action, {method: button.dataset.method || 'POST'});
        toast('Action completed by the server.');
        window.setTimeout(() => window.location.reload(), 350);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        setBusy(button, false);
      }
    });
  });

  const modeForm = document.querySelector('[data-mode-form]');
  const armedConfirmation = modeForm?.querySelector('[data-armed-confirmation]');
  function updateModeConsequence() {
    if (!modeForm) return;
    const selected = modeForm.querySelector('input[name="mode"]:checked')?.value;
    if (armedConfirmation) armedConfirmation.hidden = selected !== 'ARMED';
    const copy = modeForm.querySelector('[data-mode-consequence]');
    if (copy) {
      copy.textContent = selected === 'ARMED'
        ? 'ARMED permits a caller to request submission. It does not bypass live-submit, allowlist, risk, identity, review, egress, or single-use authority gates.'
        : selected === 'REVIEW_ONLY'
          ? 'REVIEW ONLY may prepare evidence and always stops before submission.'
          : 'OFF stops scheduled automation work. No submission becomes possible.';
    }
  }
  modeForm?.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', updateModeConsequence));
  updateModeConsequence();
  modeForm?.addEventListener('submit', async event => {
    event.preventDefault();
    const data = formJSON(modeForm);
    const selected = String(data.mode || '');
    const consequence = selected === 'ARMED'
      ? 'This permits callers to request submission after every server guard. Continue with the exact typed confirmation?'
      : `Change automation to ${selected.replace('_', ' ')}? No submission will be made by this request.`;
    if (!window.confirm(consequence)) return;
    const button = modeForm.querySelector('[type="submit"]');
    setBusy(button, true, 'Requesting…');
    try {
      const result = await api('/control/mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          mode: selected,
          confirmation: String(data.confirmation || ''),
          dry_run_default: Boolean(data.dry_run_default),
        }),
      });
      toast(result.message);
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  });

  const selection = Array.from(document.querySelectorAll('[data-row-select]'));
  const selectionCount = document.querySelector('[data-selection-count]');
  const bulkButtons = Array.from(document.querySelectorAll('[data-bulk-action]'));
  function selectedRows() {
    return selection.filter(input => input.checked);
  }
  function updateSelection() {
    const count = selectedRows().length;
    if (selectionCount) selectionCount.textContent = String(count);
    bulkButtons.forEach(button => { button.disabled = count === 0; });
  }
  selection.forEach(input => input.addEventListener('change', updateSelection));
  updateSelection();
  bulkButtons.forEach(button => {
    button.addEventListener('click', async () => {
      const rows = selectedRows();
      const count = rows.length;
      if (!count) return;
      const action = button.dataset.bulkAction;
      if (!window.confirm(`${action[0].toUpperCase()}${action.slice(1)} exactly ${count} selected role${count === 1 ? '' : 's'}? This action never submits.`)) return;
      setBusy(button, true);
      try {
        const result = await api('/pipeline/bulk', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            action,
            opportunity_ids: rows.map(input => input.value),
            expected_count: count,
            confirmed: true,
          }),
        });
        toast(`${result.action}: ${result.affected} of ${result.selected} rows changed. Submissions: 0.`);
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        setBusy(button, false);
      }
    });
  });

  document.querySelectorAll('[data-user-status-select]').forEach(statusSelect => {
    statusSelect.dataset.committedValue = statusSelect.value;
    statusSelect.addEventListener('change', async () => {
      const opportunityId = statusSelect.dataset.opportunityId;
      const previous = statusSelect.dataset.committedValue;
      const requested = statusSelect.value;
      if (!opportunityId || requested === previous) return;
      statusSelect.disabled = true;
      statusSelect.setAttribute('aria-busy', 'true');
      try {
        const result = await api(`/api/opportunities/${encodeURIComponent(opportunityId)}/user-status`, {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status: requested}),
        });
        document.querySelectorAll(`[data-user-status-select][data-opportunity-id="${CSS.escape(opportunityId)}"]`).forEach(peer => {
          peer.value = result.status;
          peer.dataset.committedValue = result.status;
        });
        document.querySelectorAll(`[data-user-status-editor][data-opportunity-id="${CSS.escape(opportunityId)}"]`).forEach(editor => {
          editor.dataset.automationEligible = String(Boolean(result.automation_eligible));
          const reason = editor.querySelector('[data-user-status-reason]');
          if (reason) {
            reason.textContent = result.automation_exclusion_reason || '';
            reason.hidden = !result.automation_exclusion_reason;
          }
        });
        toast(result.changed ? 'Candidate status saved and audited locally.' : 'Candidate status is unchanged.');
      } catch (error) {
        statusSelect.value = previous;
        toast(error.message, 'error');
      } finally {
        statusSelect.disabled = false;
        statusSelect.removeAttribute('aria-busy');
      }
    });
  });

  // Candidate-owned preparation has a dedicated server route.  The browser
  // sends only the exact application id; mode, visibility, target URL, and
  // the no-submit boundary are all fixed server-side.
  function prefillScope(button) {
    return button.closest('.needs-row') || button.closest('.rule-section') || document;
  }

  function prefillStatus(scope) {
    return scope.querySelector('[data-prefill-status]');
  }

  async function startPrefill(button) {
    if (button.dataset.prefillInFlight === 'true') return;
    const applicationId = String(button.dataset.applicationId || '').trim();
    if (!applicationId) return;
    if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
    const scope = prefillScope(button);
    const status = prefillStatus(scope);
    button.dataset.prefillInFlight = 'true';
    setBusy(button, true, 'Preparing…');
    if (status) status.textContent = 'Preparing this application in the visible browser…';
    try {
      const result = await api(`/api/applications/${encodeURIComponent(applicationId)}/prefill`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
      });
      const wall = String(result?.wall || result?.message || 'Visible PREFILL is ready for human review.').trim();
      if (status) status.textContent = wall;
      toast(wall);
      window.setTimeout(() => window.location.assign(window.location.href), 450);
    } catch (error) {
      const wall = String(error?.payload?.wall || error?.payload?.message || error.message || 'PREFILL was refused.').trim();
      if (status) status.textContent = wall;
      toast(wall, 'error');
    } finally {
      button.dataset.prefillInFlight = 'false';
      setBusy(button, false);
    }
  }

  document.querySelectorAll('[data-prefill-start]').forEach(button => {
    button.addEventListener('click', () => startPrefill(button));
  });

  // Operator-supplied URL (fix-link) affordance
  document.querySelectorAll('[data-fix-link-toggle]').forEach(toggle => {
    toggle.addEventListener('click', () => {
      const form = toggle.closest('.target-url-panel').querySelector('[data-fix-link-form]');
      form.hidden = !form.hidden;
      if (!form.hidden) {
        const input = form.querySelector('[data-fix-link-input]');
        input.focus();
        input.select();
      }
    });
  });

  document.querySelectorAll('[data-fix-link-cancel]').forEach(cancel => {
    cancel.addEventListener('click', () => {
      const form = cancel.closest('[data-fix-link-form]');
      form.hidden = true;
      const status = form.querySelector('[data-fix-link-status]');
      const result = form.querySelector('[data-fix-link-result]');
      if (status) status.textContent = '';
      if (result) result.textContent = '';
    });
  });

  document.querySelectorAll('[data-fix-link-submit]').forEach(submit => {
    submit.addEventListener('click', async () => {
      const applicationId = String(submit.dataset.applicationId || '').trim();
      const form = submit.closest('[data-fix-link-form]');
      const input = form.querySelector('[data-fix-link-input]');
      const status = form.querySelector('[data-fix-link-status]');
      const result = form.querySelector('[data-fix-link-result]');
      const url = input.value.trim();
      if (!url) {
        if (status) status.textContent = 'Please paste a URL.';
        return;
      }
      if (status) status.textContent = 'Verifying…';
      if (result) result.textContent = '';
      setBusy(submit, true);
      try {
        const response = await api(`/api/applications/${encodeURIComponent(applicationId)}/operator-supplied-url`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({application_url: url}),
        });
        if (result) {
          // The server stores the link but does NOT verify it here, so the UI
          // must not say "verified" -- that was the wording that made an
          // unchecked page look ready to be filled with real candidate data.
          result.style.color = 'var(--accent)';
          result.textContent = response.message
            || 'Link saved. ARGUS will verify it on the next resolution pass before filling anything into it.';
        }
        if (status) status.textContent = '';
        window.setTimeout(() => window.location.reload(), 600);
      } catch (error) {
        if (status) status.textContent = '';
        if (result) {
          result.style.color = 'var(--red)';
          const detail = error?.payload?.message || error.message || 'URL could not be verified.';
          result.textContent = `Verification failed: ${detail}`;
        }
      } finally {
        setBusy(submit, false);
      }
    });
  });
})();
