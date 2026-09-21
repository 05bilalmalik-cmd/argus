'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const labels = {year_in_industry:'Industrial placement', spring_week:'Spring week', summer:'Summer internship'};
  const stages = {not_applied:'Not applied', applied:'Applied', assessment:'Assessment', interview:'Interview', offer:'Offer', rejected:'Rejected', withdrawn:'Withdrawn'};
  const pipelineStages = ['discovery', 'verification', 'delivery'];
  const state = {
    view: 'all', offset: 0, jobs: [], currentJob: null, requestId: 0,
    lastFinished: undefined, statusReady: false, profile: null,
    profileLoading: false, profileLoaded: false, profileDirtyFields: new Set(), alertsLoading: false,
  };
  const pageSize = 100;
  let toastTimer;
  let searchDebounce;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }

  function isObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function clone(value) {
    if (!isObject(value) && !Array.isArray(value)) return {};
    try { return JSON.parse(JSON.stringify(value)); } catch { return {}; }
  }

  function valueText(value, fallback = 'Not reported') {
    if (value === null || value === undefined || value === '') return fallback;
    if (typeof value === 'object') {
      try { return JSON.stringify(value); } catch { return fallback; }
    }
    return String(value);
  }

  function numberValue(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
    const number = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function displayNumber(value) {
    const number = numberValue(value);
    return number === null ? '—' : number.toLocaleString('en-GB');
  }

  function metric(object, keys) {
    if (!isObject(object)) return null;
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(object, key) && object[key] !== null && object[key] !== undefined) return object[key];
    }
    return null;
  }

  function list(value) {
    return Array.isArray(value) ? value.filter(item => item !== null && item !== undefined).map(String) : [];
  }

  function toast(message) {
    const element = $('toast');
    element.textContent = valueText(message, 'Request completed');
    element.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { element.hidden = true; }, 6000);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: 'no-store',
      ...options,
      headers: {'Content-Type': 'application/json', 'X-Argus-Tracker': '1', ...(options.headers || {})},
    });
    const raw = await response.text();
    let data = {};
    if (raw) {
      try { data = JSON.parse(raw); }
      catch { data = {detail: raw.slice(0, 240)}; }
    }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request refused (${response.status})`);
    return data;
  }

  function relative(value) {
    if (!value) return 'Not checked';
    const date = new Date(value);
    const elapsed = Date.now() - date.getTime();
    if (!Number.isFinite(elapsed)) return 'Unknown';
    if (elapsed < 0) return `in ${date.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}`;
    if (elapsed < 60000) return 'Just now';
    if (elapsed < 3600000) return `${Math.floor(elapsed / 60000)}m ago`;
    if (elapsed < 86400000) return `${Math.floor(elapsed / 3600000)}h ago`;
    return `${Math.floor(elapsed / 86400000)}d ago`;
  }

  function dateLabel(value) {
    if (!value) return 'Not stated';
    const date = new Date(typeof value === 'string' && value.length === 10 ? `${value}T12:00:00` : value);
    return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'numeric'});
  }

  function dateTimeLabel(value) {
    if (!value) return 'Not reported';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString('en-GB', {dateStyle:'medium', timeStyle:'short'});
  }

  function safeLink(element, url) {
    try {
      if (typeof url !== 'string' || !url.trim()) throw new Error('Missing URL');
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('Invalid URL');
      element.href = parsed.href;
      element.target = '_blank';
      element.rel = 'noopener noreferrer';
      element.removeAttribute('aria-disabled');
      return true;
    } catch {
      element.removeAttribute('href');
      element.removeAttribute('target');
      element.removeAttribute('rel');
      element.setAttribute('aria-disabled', 'true');
      return false;
    }
  }

  function normalized(value) {
    return typeof value === 'string' ? value.toLowerCase().replaceAll('-', '_') : '';
  }

  function statusLabel(value, family = 'pipeline') {
    const key = normalized(value);
    if (!key) return 'NOT REPORTED';
    const known = {
      open: 'OPEN / EVIDENCE', closed: 'CLOSED / EVIDENCE', unknown: 'UNKNOWN / NOT CHECKED',
      potential: 'POTENTIAL', review: 'REVIEW', excluded: 'EXCLUDED',
      never_checked: 'NEVER CHECKED', not_reported: 'NOT REPORTED', not_checked: 'NOT CHECKED', running: 'RUNNING', ok: 'OK', partial: 'PARTIAL',
      blocked: 'BLOCKED', error: 'ERROR', empty: 'EMPTY / CHECKED',
      pending: 'PENDING', delivered: 'DELIVERED / CONFIRMED', sent: 'SENT / DELIVERY NOT CONFIRMED',
      disabled: 'DISABLED', unconfigured: 'UNCONFIGURED', failed: 'FAILED',
    };
    if (known[key]) return known[key];
    return family === 'availability' ? 'UNKNOWN / NOT CHECKED' : `${key.toUpperCase()} / NOT CONFIRMED`;
  }

  function statusClass(value) {
    const key = normalized(value);
    if (['open', 'potential', 'ok', 'delivered', 'ready'].includes(key)) return 'positive';
    if (['review', 'unknown', 'never_checked', 'running', 'partial', 'pending', 'sent', 'empty'].includes(key)) return 'caution';
    if (['closed', 'excluded', 'blocked', 'blocked_disabled', 'error', 'failed', 'unconfigured', 'disabled'].includes(key)) return 'negative';
    return 'neutral';
  }

  function chip(value, family = 'pipeline') {
    const element = node('span', statusLabel(value, family), `status-chip ${statusClass(value)}`);
    element.dataset.status = normalized(value) || 'not_reported';
    return element;
  }

  function params() {
    const query = new URLSearchParams();
    query.set('q', $('search').value);
    query.set('programmes', $('programme').value);
    query.set('stage', $('stage').value);
    query.set('sort', $('sort').value);
    query.set('uk_only', String($('uk-only').checked));
    query.set('new_only', String($('new-only').checked));
    query.set('saved_only', String(state.view === 'saved'));
    query.set('deadline_soon', String($('deadline-soon').checked));
    query.set('include_expired', String($('include-expired').checked));
    query.set('match_status', $('match-status').value);
    query.set('availability', $('availability').value);
    return query;
  }

  function setMetric(id, value) { $(id).textContent = displayNumber(value); }

  function stageSelect(job) {
    const select = node('select', null, 'row-stage');
    select.setAttribute('aria-label', `Progress for ${valueText(job.employer, 'role')} ${valueText(job.title, '')}`);
    Object.entries(stages).forEach(([key, text]) => { const option = node('option', text); option.value = key; select.append(option); });
    select.value = Object.prototype.hasOwnProperty.call(stages, job.stage) ? job.stage : 'not_applied';
    select.addEventListener('change', async () => {
      const previous = select.value;
      select.disabled = true;
      try {
        await api(`/api/jobs/${encodeURIComponent(job.id)}`, {method:'PATCH', body:JSON.stringify({stage:previous})});
        toast('Progress saved. No application was sent.');
        await loadJobs();
      } catch (error) {
        select.value = job.stage || 'not_applied';
        toast(error.message);
      } finally { select.disabled = false; }
    });
    return select;
  }

  function sourceCount(job) {
    if (!Array.isArray(job.sources)) return 'Sources not reported';
    const sources = list(job.sources);
    return `${sources.length} source${sources.length === 1 ? '' : 's'}`;
  }

  function renderJobs(data) {
    const rows = Array.isArray(data && data.jobs) ? data.jobs : [];
    state.jobs = rows;
    const body = $('job-rows');
    body.replaceChildren();
    $('jobs-table-wrap').setAttribute('aria-busy', 'false');
    for (const job of rows) {
      const row = node('tr');
      row.dataset.jobId = valueText(job.id, '');
      const saveCell = node('td');
      const saved = job.saved === true;
      const save = node('button', saved ? '★' : '☆', `save${saved ? ' saved' : ''}`);
      save.type = 'button';
      save.setAttribute('aria-label', `${saved ? 'Unsave' : 'Save'} ${valueText(job.employer, 'role')} ${valueText(job.title, '')}`);
      save.setAttribute('aria-pressed', String(saved));
      save.addEventListener('click', async () => {
        save.disabled = true;
        try { await api(`/api/jobs/${encodeURIComponent(job.id)}`, {method:'PATCH', body:JSON.stringify({saved:!saved})}); await Promise.all([loadJobs(), loadStatus()]); }
        catch (error) { toast(error.message); save.disabled = false; }
      });
      saveCell.append(save); row.append(saveCell);

      const main = node('td', null, 'job-main');
      main.append(node('span', valueText(job.employer, 'Employer not reported'), 'employer'));
      const title = node('button', valueText(job.title, 'Untitled role'), 'role-title');
      title.type = 'button';
      title.addEventListener('click', () => openDetails(job, true));
      main.append(title);
      const meta = node('div', null, 'row-meta');
      if (job.new === true) meta.append(node('span', 'NEW TO TRACKER', 'badge'));
      if (job.stale === true) meta.append(node('span', 'STALE OBSERVATION', 'badge warn'));
      if (job.expired === true) meta.append(node('span', 'DEADLINE PASSED', 'badge warn'));
      if (job.due_date) meta.append(node('span', `Next action ${dateLabel(job.due_date)}`, 'badge warn'));
      meta.append(node('span', sourceCount(job)));
      main.append(meta); row.append(main);
      row.append(node('td', valueText(labels[job.programme] || job.programme, 'Programme not reported'), 'programme'));
      row.append(node('td', valueText(job.location, 'Location not specified'), 'location'));

      const fitCell = node('td', null, 'fit-cell');
      fitCell.append(chip(job.match_status));
      const reasons = list(job.match_reasons), unknowns = list(job.match_unknowns);
      if (reasons.length) fitCell.append(node('small', `${reasons.length} reason${reasons.length === 1 ? '' : 's'}`, 'cell-note'));
      if (unknowns.length) fitCell.append(node('small', `${unknowns.length} unknown${unknowns.length === 1 ? '' : 's'}`, 'cell-note caution-text'));
      row.append(fitCell);

      const availability = node('td', null, 'availability-cell');
      availability.append(chip(job.availability, 'availability'));
      availability.append(node('small', job.verified_at ? `Verified ${relative(job.verified_at)}` : 'Not checked', 'cell-note'));
      if (job.verification_error) availability.append(node('small', job.verification_error, 'cell-note error-text'));
      row.append(availability);

      const deadline = node('td', null, 'evidence-cell');
      deadline.append(node('span', job.deadline_text || (job.deadline ? `Parsed date ${dateLabel(job.deadline)}` : 'Not stated by source'), 'evidence-value'));
      if (job.deadline && job.deadline_text) deadline.append(node('small', `Parsed date: ${dateLabel(job.deadline)}`, 'cell-note'));
      if (job.deadline_basis) deadline.title = `Basis: ${valueText(job.deadline_basis)}`;
      row.append(deadline);

      const posted = node('td', null, 'evidence-cell');
      posted.append(node('span', `Source posted: ${job.posted_text || (job.posted_at ? dateLabel(job.posted_at) : 'Not stated by source')}`, 'evidence-value'));
      posted.append(node('small', `First detected by ARGUS: ${dateLabel(job.first_seen)}`, 'cell-note'));
      row.append(posted);

      const progress = node('td'); progress.append(stageSelect(job)); row.append(progress);
      const openCell = node('td');
      const link = node('a', '↗', 'apply');
      if (!safeLink(link, job.url)) { link.textContent = '—'; link.title = 'No safe employer link was provided'; }
      link.setAttribute('aria-label', `Open ${valueText(job.employer, 'role')} ${valueText(job.title, '')} on employer site`);
      openCell.append(link); row.append(openCell);
      body.append(row);
    }
    const total = numberValue(data && data.total);
    $('empty').hidden = total !== null ? total !== 0 : rows.length !== 0;
    $('result-count').textContent = total === null ? 'Matching role count not reported' : `${displayNumber(total)} matching role${total === 1 ? '' : 's'}`;
    $('page-info').textContent = total === null ? (rows.length ? 'Current page · total not reported' : 'No results reported') : total ? `${state.offset + 1}–${state.offset + rows.length} of ${displayNumber(total)}` : '0 results';
    $('previous').disabled = state.offset === 0;
    $('next').disabled = data && data.has_more !== true;
    void openHashJob();
  }

  async function loadJobs() {
    const id = ++state.requestId;
    const query = params();
    $('export').href = '/api/export.csv?' + query.toString();
    query.set('offset', state.offset); query.set('limit', pageSize);
    $('jobs-table-wrap').setAttribute('aria-busy', 'true');
    $('result-count').textContent = 'Loading evidence…';
    try {
      const data = await api('/api/jobs?' + query.toString());
      if (id !== state.requestId) return;
      const total = numberValue(data && data.total);
      if (state.offset && total !== null && state.offset >= total) { state.offset = 0; return loadJobs(); }
      renderJobs(data);
    } catch (error) {
      if (id === state.requestId) { $('jobs-table-wrap').setAttribute('aria-busy', 'false'); $('result-count').textContent = 'Could not load roles'; toast(error.message); }
    }
  }

  function renderSources(sources) {
    const rows = Array.isArray(sources) ? sources : [];
    const body = $('source-rows'); body.replaceChildren();
    $('source-empty').hidden = rows.length !== 0;
    if (!rows.length) {
      const row = node('tr'), cell = node('td', 'NEVER CHECKED · No source check has completed.', 'source-empty-row');
      cell.colSpan = 6; row.append(cell); body.append(row); return;
    }
    for (const source of rows) {
      const row = node('tr'), nameCell = node('td');
      const link = node('a', valueText(source.name, 'Unnamed source'), 'source-name');
      if (!safeLink(link, source.url)) link.textContent = valueText(source.name, 'Unnamed source');
      nameCell.append(link); row.append(nameCell);
      const rawStatus = source.status || 'never_checked';
      const stateCell = node('td'); stateCell.append(chip(rawStatus)); row.append(stateCell);
      row.append(node('td', displayNumber(source.row_count), 'date-cell'));
      row.append(node('td', relative(source.checked_at || source.last_attempt_at), 'date-cell'));
      row.append(node('td', source.last_success_at ? relative(source.last_success_at) : 'Not established', 'date-cell'));
      let detail = source.error || source.detail;
      if (!detail) detail = normalized(rawStatus) === 'never_checked' ? 'No source check has completed.' : normalized(rawStatus) === 'empty' ? 'Validated response returned no matching roles.' : 'No detail reported.';
      row.append(node('td', detail));
      body.append(row);
    }
  }

  function resultText(result) {
    if (result === null || result === undefined || result === '') return 'Result not reported';
    if (typeof result === 'string') return result;
    if (!isObject(result)) return String(result);
    const entries = Object.entries(result).filter(([, value]) => value !== null && value !== undefined && value !== '');
    if (!entries.length) return 'Result reported without detail';
    return entries.slice(0, 3).map(([key, value]) => `${key}: ${valueText(value)}`).join(' · ');
  }

  function renderPipeline(pipeline) {
    const data = isObject(pipeline) ? pipeline : {};
    $('pipeline-version').textContent = valueText(data.version, '—');
    $('pipeline-started').textContent = data.started_at ? dateTimeLabel(data.started_at) : 'Not reported';
    $('pipeline-finished').textContent = data.finished_at ? dateTimeLabel(data.finished_at) : 'Not reported';
    const stageData = isObject(data.stages) ? data.stages : {};
    const statuses = [];
    for (const name of pipelineStages) {
      const item = isObject(stageData[name]) ? stageData[name] : {};
      const rawStatus = item.status || 'not_reported';
      statuses.push(rawStatus);
      const row = document.querySelector(`[data-stage="${name}"]`);
      if (!row) continue;
      const stageStatus = row.querySelector('.stage-status');
      stageStatus.textContent = statusLabel(rawStatus);
      stageStatus.className = `stage-status status-chip ${statusClass(rawStatus)}`;
      stageStatus.dataset.status = normalized(rawStatus) || 'not_reported';
      const stageDetail = item.last_error ? `Error: ${valueText(item.last_error)}` : resultText(item.result);
      row.querySelector('.stage-result').textContent = stageDetail;
      row.dataset.status = normalized(rawStatus) || 'not_reported';
    }
    const explicit = data.status || '';
    let overall = explicit;
    if (!overall && statuses.length) {
      if (statuses.some(value => ['error', 'failed'].includes(normalized(value)))) overall = 'error';
      else if (statuses.some(value => normalized(value) === 'running')) overall = 'running';
      else if (statuses.some(value => normalized(value) === 'blocked')) overall = 'blocked';
      else if (statuses.some(value => normalized(value) === 'partial')) overall = 'partial';
      else if (statuses.every(value => normalized(value) === 'never_checked')) overall = 'never_checked';
      else if (statuses.every(value => normalized(value) === 'ok')) overall = 'ok';
    }
    const pipelineChip = $('pipeline-state');
    pipelineChip.textContent = statusLabel(overall);
    pipelineChip.className = `status-chip ${statusClass(overall)}`;
    pipelineChip.dataset.status = normalized(overall) || 'not_reported';
    const message = data.last_error || (normalized(overall) === 'never_checked' ? 'No pipeline run has been recorded.' : normalized(overall) === 'blocked' ? 'A stage is blocked; inspect its result before treating delivery as healthy.' : 'Stage state is available above.');
    $('pipeline-message').textContent = message;
  }

  function alertState(summary) {
    if (!isObject(summary)) return '';
    return normalized(summary.status || summary.config_status);
  }

  function alertDeliveryLabel(summary) {
    const status = alertState(summary);
    if (status === 'delivered') return 'DELIVERED / PROVIDER CONFIRMED';
    if (status === 'sent') return 'SENT / DELIVERY NOT CONFIRMED';
    if (status === 'pending') return 'PENDING / NOT DELIVERED';
    if (status === 'ready') return 'READY / DELIVERY NOT CONFIRMED';
    if (status === 'unconfigured') return 'UNCONFIGURED / DELIVERY NOT CONFIRMED';
    if (status === 'disabled' || status === 'blocked_disabled') return 'DISABLED / DELIVERY NOT CONFIRMED';
    if (status === 'blocked') return 'BLOCKED / DELIVERY NOT CONFIRMED';
    return status ? `${status.toUpperCase()} / DELIVERY NOT CONFIRMED` : 'NOT REPORTED / DELIVERY NOT CONFIRMED';
  }

  function renderAlertSummary(summary) {
    const data = isObject(summary) ? summary : {};
    const status = alertState(data);
    const label = alertDeliveryLabel(data);
    $('alert-state').textContent = label;
    $('alert-state').className = `status-text ${statusClass(status)}`;
    $('alert-pending').textContent = displayNumber(metric(data, ['pending', 'pending_count']));
    $('alert-last-error').textContent = valueText(data.last_error || data.config_reason, 'None reported');
    const unavailable = ['unconfigured', 'disabled', 'blocked', 'blocked_disabled', 'unknown', 'not_reported', ''].includes(status);
    const configured = data.configured === true || status === 'ready';
    const enabled = data.enabled === true || status === 'ready';
    const confirmed = status === 'delivered';
    const needsSetup = !confirmed && (unavailable || !configured || !enabled);
    $('email-settings-link').hidden = !needsSetup;
    const delivery = $('delivery-health');
    delivery.replaceChildren();
    delivery.append(node('span', `Email delivery: ${label}`));
    const link = node('a', needsSetup ? 'Review mail setup ↗' : 'Mail settings ↗');
    link.href = '/settings/email';
    delivery.append(link);
  }

  function renderIntelligence(summary) {
    const data = isObject(summary) ? summary : {};
    setMetric('count-potential', metric(data, ['potential', 'potential_count', 'potential_roles']));
    setMetric('count-review', metric(data, ['review', 'review_count', 'review_roles']));
    setMetric('count-verified', metric(data, ['verified_open', 'verified_open_count', 'open']));
    setMetric('count-unknown', metric(data, ['unknown', 'unknown_count', 'availability_unknown']));
    const potential = numberValue(metric(data, ['potential', 'potential_count', 'potential_roles']));
    const review = numberValue(metric(data, ['review', 'review_count', 'review_roles']));
    $('nav-fit').textContent = potential !== null && review !== null ? displayNumber(potential + review) : '—';
    const stateChip = $('intelligence-state');
    const raw = data.status || 'not_reported';
    stateChip.textContent = `FIT SCREEN · ${statusLabel(raw)}`;
    stateChip.className = `status-chip ${statusClass(raw)}`;
  }

  function renderStatus(data) {
    const payload = isObject(data) ? data : {};
    const summary = isObject(payload.summary) ? payload.summary : {};
    const refresh = isObject(payload.refresh) ? payload.refresh : {};
    const sources = Array.isArray(payload.sources) ? payload.sources : [];
    setMetric('count-total', summary.total);
    setMetric('count-new', summary.new_24h);
    setMetric('count-saved', summary.saved);
    setMetric('count-sources', summary.source_attention);
    $('nav-total').textContent = displayNumber(summary.total);
    $('nav-saved').textContent = displayNumber(summary.saved);
    $('nav-health').textContent = displayNumber(summary.source_attention);
    $('nav-automation').textContent = statusLabel(payload.pipeline && payload.pipeline.status);
    const pending = metric(payload.alerts, ['pending', 'pending_count']);
    $('nav-alerts').textContent = displayNumber(pending);
    renderIntelligence(payload.intelligence || payload.intelligence_summary);
    renderPipeline(payload.pipeline);
    renderAlertSummary(payload.alerts);
    renderSources(sources);
    $('connection').textContent = refresh.running === true ? 'COLLECTING · PUBLIC SOURCES' : 'CONNECTED · LOCAL TRACKER';
    $('refresh').disabled = refresh.running === true;
    $('refresh').textContent = refresh.running === true ? 'Pipeline running…' : 'Refresh pipeline ↻';
    if (refresh.automatic === true && numberValue(refresh.interval_seconds) !== null) {
      $('next-refresh').textContent = `AUTO REFRESH / ${Math.round(Number(refresh.interval_seconds) / 60)} MIN · NEXT ${relative(refresh.next_refresh_at)}`;
    } else {
      $('next-refresh').textContent = refresh.automatic === false ? 'AUTOMATIC REFRESH DISABLED' : 'AUTOMATIC REFRESH NOT CONFIRMED';
    }
    const attention = numberValue(summary.source_attention);
    const warning = $('source-warning');
    warning.replaceChildren();
    let warningText = '';
    if (refresh.last_error) warningText = valueText(refresh.last_error);
    else if (!sources.length) warningText = 'No source check has completed; coverage and source health are unknown.';
    else if (attention !== null && attention > 0) warningText = `${displayNumber(attention)} source${attention === 1 ? '' : 's'} incomplete or unavailable. Existing roles are retained; coverage is not complete.`;
    warning.hidden = !warningText;
    if (warningText) {
      warning.append(node('span', warningText));
      const action = node('button', 'Inspect sources →'); action.type = 'button'; action.addEventListener('click', () => switchView('sources')); warning.append(action);
    }
    const finished = Object.prototype.hasOwnProperty.call(refresh, 'last_finished_at') ? refresh.last_finished_at : undefined;
    if (!state.statusReady) { state.lastFinished = finished; state.statusReady = true; }
    else if (finished !== state.lastFinished) { state.lastFinished = finished; if (!['sources', 'automation', 'alerts'].includes(state.view)) void loadJobs(); }
  }

  async function loadStatus() {
    try { renderStatus(await api('/api/status')); }
    catch (error) {
      $('connection').textContent = 'DISCONNECTED · HEALTH NOT CONFIRMED';
      $('next-refresh').textContent = 'Refresh status unavailable';
      $('refresh').disabled = false;
      const warning = $('source-warning'); warning.replaceChildren(node('span', `Tracker status unavailable: ${error.message}`)); warning.hidden = false;
      const action = node('button', 'Inspect sources →'); action.type = 'button'; action.addEventListener('click', () => switchView('sources')); warning.append(action);
    }
  }

  function switchView(next) {
    const previous = state.view;
    state.view = next; state.offset = 0;
    if (next === 'fit') $('match-status').value = 'potential,review';
    else if (previous === 'fit' && ['all', 'saved'].includes(next)) $('match-status').value = '';
    document.querySelectorAll('[data-view]').forEach(button => {
      const active = button.dataset.view === state.view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-current', active ? 'page' : 'false');
    });
    $('sources-panel').hidden = state.view !== 'sources';
    $('automation-panel').hidden = state.view !== 'automation';
    $('alerts-panel').hidden = state.view !== 'alerts';
    $('openings-panel').hidden = ['sources', 'automation', 'alerts'].includes(state.view);
    const titles = {
      all: ['Internship radar.', 'Summer internships, industrial placements and spring weeks.'],
      saved: ['Your next moves.', 'Your shortlist, deadlines and application progress.'],
      fit: ['Fit screen.', 'Known signals first; unresolved facts stay visible.'],
      sources: ['Source health.', 'A refresh is only as good as the sources it actually reached.'],
      automation: ['Automation control.', 'Profile facts, pipeline stages and decision boundaries.'],
      alerts: ['Alert history.', 'Durable events, provider evidence and delivery uncertainty.'],
    };
    const title = titles[state.view] || titles.all;
    $('page-title').textContent = title[0]; $('page-subtitle').textContent = title[1];
    if (['sources', 'automation', 'alerts'].includes(state.view)) {
      if (state.view === 'automation') void loadProfile();
      if (state.view === 'alerts') void loadAlerts();
    } else void loadJobs();
  }

  function appendFact(parent, key, value) { parent.append(node('dt', key), node('dd', valueText(value))); }

  function renderListItems(element, values, emptyText) {
    element.replaceChildren();
    if (!values.length) { element.append(node('li', emptyText)); return; }
    values.forEach(value => element.append(node('li', value)));
  }

  function renderEvidence(evidence) {
    const listElement = $('detail-evidence'); listElement.replaceChildren();
    if (!Array.isArray(evidence) || !evidence.length) { listElement.append(node('li', 'No evidence records returned.')); return; }
    evidence.forEach(item => {
      const data = isObject(item) ? item : {};
      const entry = node('li'), heading = node('div', null, 'evidence-heading');
      heading.append(node('strong', valueText(data.source || data.source_name || data.label, 'Source observation')));
      const observed = data.observed_at || data.checked_at || data.created_at;
      if (observed) heading.append(node('span', `Observed ${dateTimeLabel(observed)}`, 'evidence-time'));
      entry.append(heading);
      if (data.quote || data.text || data.snippet) entry.append(node('q', valueText(data.quote || data.text || data.snippet), 'evidence-quote'));
      const url = data.source_url || data.url;
      if (url) {
        const link = node('a', 'Open source observation ↗', 'evidence-link');
        if (safeLink(link, url)) entry.append(link);
      }
      listElement.append(entry);
    });
  }

  function populateDetails(job) {
    state.currentJob = job;
    $('detail-title').textContent = valueText(job.title, 'Untitled role');
    $('detail-employer').textContent = `${valueText(job.employer, 'Employer not reported')} · ${valueText(job.location, 'Location not specified')}`;
    $('detail-summary').textContent = `Fit: ${statusLabel(job.match_status)} · Availability: ${statusLabel(job.availability, 'availability')}`;
    $('detail-description').textContent = valueText(job.description, 'No description supplied by source.');
    const facts = $('detail-facts'); facts.replaceChildren();
    appendFact(facts, 'Programme', labels[job.programme] || job.programme);
    appendFact(facts, 'Fit screen', statusLabel(job.match_status));
    appendFact(facts, 'Availability', statusLabel(job.availability, 'availability'));
    appendFact(facts, 'Last verified', job.verified_at ? dateTimeLabel(job.verified_at) : 'Not checked');
    appendFact(facts, 'Verification note', job.verification_error || 'No verification error reported');
    appendFact(facts, 'Source deadline', job.deadline_text || (job.deadline ? dateLabel(job.deadline) : 'Not stated by source'));
    appendFact(facts, 'Deadline basis', job.deadline_basis || 'Not reported');
    appendFact(facts, 'Parsed deadline', job.deadline ? dateLabel(job.deadline) : 'Not parsed');
    appendFact(facts, 'Source posted', job.posted_text || (job.posted_at ? dateLabel(job.posted_at) : 'Not stated by source'));
    appendFact(facts, 'Parsed posted date', job.posted_at ? dateLabel(job.posted_at) : 'Not parsed');
    appendFact(facts, 'First detected by ARGUS', job.first_seen ? `${dateTimeLabel(job.first_seen)} · ${relative(job.first_seen)}` : 'Not reported');
    appendFact(facts, 'Last observed by ARGUS', job.last_seen ? `${dateTimeLabel(job.last_seen)} · ${relative(job.last_seen)}` : 'Not reported');
    appendFact(facts, 'Sources', list(job.sources).join(', ') || 'Not reported');
    renderListItems($('detail-reasons'), list(job.match_reasons), 'No positive reasons recorded.');
    renderListItems($('detail-unknowns'), list(job.match_unknowns), 'No unknowns recorded.');
    renderEvidence(job.evidence);
    $('detail-stage').value = Object.prototype.hasOwnProperty.call(stages, job.stage) ? job.stage : 'not_applied';
    $('detail-due').value = job.due_date || '';
    $('detail-notes').value = job.notes || '';
    $('detail-error').textContent = '';
    const employerLink = $('detail-apply');
    if (!safeLink(employerLink, job.url)) { employerLink.textContent = 'Employer link unavailable'; employerLink.classList.add('disabled'); }
    else { employerLink.textContent = 'Open employer ↗'; employerLink.classList.remove('disabled'); }
  }

  function setJobHash(id) { if (id) window.history.replaceState(null, '', `#job=${encodeURIComponent(id)}`); }
  function clearJobHash() { if (window.location.hash) window.history.replaceState(null, '', window.location.pathname + window.location.search); }

  function closeDetails() {
    const dialog = $('details');
    if (dialog.open) dialog.close();
    else dialog.removeAttribute('open');
    state.currentJob = null;
    clearJobHash();
  }

  async function openDetails(job, updateHash) {
    if (!job || job.id === undefined || job.id === null) return;
    if (updateHash) setJobHash(job.id);
    populateDetails(job);
    const dialog = $('details');
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  }

  async function openHashJob() {
    const raw = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : '';
    const id = new URLSearchParams(raw).get('job');
    if (!id || state.currentJob && String(state.currentJob.id) === id && $('details').open) return;
    const local = state.jobs.find(job => String(job.id) === id);
    if (local) { await openDetails(local, false); return; }
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(id)}`);
      const job = isObject(result && result.job) ? result.job : result;
      if (!isObject(job) || job.id === undefined) throw new Error('Role detail was not reported');
      await openDetails(job, false);
    } catch (error) { toast(`Could not open role: ${error.message}`); }
  }

  async function loadProfile() {
    if (state.profileLoading) return;
    state.profileLoading = true;
    $('profile-load-state').textContent = 'LOADING'; $('profile-load-state').className = 'status-chip caution';
    $('profile-status').textContent = 'Loading matching profile…';
    try {
      const data = await api('/api/profile');
      const profile = isObject(data && data.profile) ? data.profile : data;
      if (!isObject(profile)) throw new Error('Profile response was not an object');
      state.profile = clone(profile); state.profileLoaded = true;
      if (state.profileDirtyFields.size) {
        // A late fetch must never clobber user input typed while the load
        // was in flight, but untouched controls still need initialising from
        // the loaded profile (e.g. role/location checkboxes). Snapshot the
        // edited fields, render, then restore exactly those fields.
        const preserved = new Map();
        for (const id of state.profileDirtyFields) {
          const field = $(id);
          if (field) preserved.set(id, field.type === 'checkbox' ? field.checked : field.value);
        }
        renderProfile(state.profile);
        for (const [id, value] of preserved) {
          const field = $(id);
          if (!field) continue;
          if (field.type === 'checkbox') field.checked = value; else field.value = value;
        }
        $('profile-load-state').textContent = 'LOADED'; $('profile-load-state').className = 'status-chip positive';
      } else {
        renderProfile(state.profile);
        $('profile-status').textContent = 'Profile loaded. Unknown facts remain unknown.';
      }
    } catch (error) {
      $('profile-load-state').textContent = 'LOAD ERROR'; $('profile-load-state').className = 'status-chip negative';
      $('profile-status').textContent = `Could not load profile: ${error.message}`;
    } finally { state.profileLoading = false; }
  }

  function checkboxValues(ids) { return ids.filter(id => $(id).checked).map(id => $(id).value); }
  function extraValues(input) { return $(input).value.split(',').map(value => value.trim()).filter(Boolean); }
  function uniqueValues(values) { return [...new Set(values)]; }

  function renderProfile(profile) {
    const data = isObject(profile) ? profile : {};
    const years = isObject(data.graduation_years) ? data.graduation_years : {};
    // Never write into the control the user is actively editing: a render
    // landing inside a fill/keystroke would corrupt the in-progress value.
    // The focused field keeps its typed content (tracked as dirty and
    // restored by the caller); checkboxes/radios are always rendered so
    // untouched controls still initialise from the loaded profile.
    const active = document.activeElement;
    const setText = (id, value) => {
      const field = $(id);
      if (field && field !== active) field.value = value;
    };
    setText('profile-degree', typeof data.degree === 'string' ? data.degree : '');
    setText('profile-summer-year', years.summer ?? '');
    setText('profile-industry-year', years.year_in_industry ?? '');
    setText('profile-spring-year', years.spring_week ?? '');
    const roles = list(data.desired_roles), locations = list(data.desired_locations);
    [['profile-role-summer', 'summer'], ['profile-role-industry', 'year_in_industry'], ['profile-role-spring', 'spring_week']].forEach(([id, value]) => { $(id).checked = roles.includes(value); });
    [['profile-location-uk', 'UK'], ['profile-location-london', 'London'], ['profile-location-remote', 'Remote']].forEach(([id, value]) => { $(id).checked = locations.includes(value); });
    $('profile-roles-other').value = roles.filter(value => !['summer', 'year_in_industry', 'spring_week'].includes(value)).join(', ');
    $('profile-locations-other').value = locations.filter(value => !['UK', 'London', 'Remote'].includes(value)).join(', ');
    const degree = valueText(data.degree, 'Degree not reported');
    const yearText = ['summer', 'year_in_industry', 'spring_week'].map(key => years[key] ? `${key.replaceAll('_', ' ')} ${years[key]}` : null).filter(Boolean).join(' · ') || 'Graduation years not reported';
    $('profile-summary').textContent = `${degree} · ${yearText}`;
    const known = new Set(['degree', 'graduation_years', 'desired_roles', 'desired_locations']);
    const extras = Object.keys(data).filter(key => !known.has(key));
    $('profile-extra-fields').textContent = extras.length ? `Additional fields retained: ${extras.join(', ')}` : 'No additional profile fields reported; future fields will be retained.';
    $('profile-load-state').textContent = 'LOADED'; $('profile-load-state').className = 'status-chip positive';
  }

  function profilePayload() {
    if (!state.profileLoaded || !isObject(state.profile)) throw new Error('Profile is not loaded');
    const payload = clone(state.profile);
    const degree = $('profile-degree').value.trim();
    if (!degree) throw new Error('Degree / subject is required');
    const yearIds = [['summer', 'profile-summer-year'], ['year_in_industry', 'profile-industry-year'], ['spring_week', 'profile-spring-year']];
    const years = isObject(payload.graduation_years) ? {...payload.graduation_years} : {};
    for (const [key, id] of yearIds) {
      const raw = $(id).value.trim(), value = Number(raw);
      if (!raw || !Number.isInteger(value) || value < 2000 || value > 2100) throw new Error(`${key.replaceAll('_', ' ')} graduation year must be between 2000 and 2100`);
      years[key] = value;
    }
    const roles = uniqueValues([...checkboxValues(['profile-role-summer', 'profile-role-industry', 'profile-role-spring']), ...extraValues('profile-roles-other')]);
    const locations = uniqueValues([...checkboxValues(['profile-location-uk', 'profile-location-london', 'profile-location-remote']), ...extraValues('profile-locations-other')]);
    if (!roles.length) throw new Error('Choose at least one tracked programme');
    if (!locations.length) throw new Error('Choose at least one desired location');
    payload.degree = degree; payload.graduation_years = years; payload.desired_roles = roles; payload.desired_locations = locations;
    return payload;
  }

  async function saveProfile(event) {
    event.preventDefault();
    const submit = event.submitter || $('profile-form').querySelector('button[type="submit"]');
    try {
      if (state.profileLoading) {
        // Order the save after the in-flight baseline load so the payload
        // merges fresh server fields with the user's typed input instead of
        // failing on a not-yet-loaded profile. The late fetch cannot clobber
        // the form (see loadProfile's dirty guard); this only waits for it.
        const deadline = Date.now() + 5000;
        while (state.profileLoading && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 25));
        }
      }
      const payload = profilePayload();
      submit.disabled = true; $('profile-status').textContent = 'Saving profile…';
      const data = await api('/api/profile', {method:'PUT', body:JSON.stringify(payload)});
      const saved = isObject(data && data.profile) ? data.profile : payload;
      state.profileDirtyFields.clear();
      state.profile = clone(saved); renderProfile(state.profile);
      $('profile-status').textContent = 'Saved. Cached matching refreshed; no source refresh requested.';
      toast('Profile saved. Source verification was not run.');
    } catch (error) { $('profile-status').textContent = error.message; toast(error.message); }
    finally { submit.disabled = false; }
  }

  function renderAlerts(data) {
    const payload = isObject(data) ? data : {};
    renderAlertSummary(payload.summary);
    const events = Array.isArray(payload.events) ? payload.events : [];
    const body = $('alert-rows'); body.replaceChildren(); $('alerts-empty').hidden = events.length !== 0;
    if (!events.length) { const row = node('tr'), cell = node('td', 'NO EVENTS · No alert events have been recorded.', 'source-empty-row'); cell.colSpan = 6; row.append(cell); body.append(row); return; }
    for (const event of events) {
      const item = isObject(event) ? event : {};
      const row = node('tr');
      row.append(node('td', dateTimeLabel(item.created_at), 'date-cell'));
      row.append(node('td', valueText(item.event_key || item.kind, 'Event not reported')));
      const statusCell = node('td'); statusCell.append(chip(item.status || 'not_reported')); row.append(statusCell);
      row.append(node('td', valueText(item.subject, 'Subject not reported')));
      let provider = 'No provider delivery evidence';
      if (item.delivered_at) provider = `Delivered at ${dateTimeLabel(item.delivered_at)}`;
      else if (item.provider_delivery_id) provider = `Provider delivery id: ${valueText(item.provider_delivery_id)} · delivery not confirmed`;
      else if (item.message_id) provider = `Provider message id: ${valueText(item.message_id)} · delivery not confirmed`;
      else if (item.sent_at) provider = `Provider handoff at ${dateTimeLabel(item.sent_at)} · delivery not confirmed`;
      row.append(node('td', provider));
      row.append(node('td', valueText(item.last_error, '—')));
      body.append(row);
    }
  }

  async function loadAlerts() {
    if (state.alertsLoading) return;
    state.alertsLoading = true; $('alerts-panel').setAttribute('aria-busy', 'true');
    $('alert-rows').replaceChildren(); $('alerts-empty').hidden = true;
    try { renderAlerts(await api('/api/alerts')); state.alertsLoaded = true; }
    catch (error) { $('alerts-empty').hidden = false; $('alerts-empty').replaceChildren(node('strong', 'ALERT HISTORY UNAVAILABLE'), node('span', error.message)); }
    finally { state.alertsLoading = false; $('alerts-panel').setAttribute('aria-busy', 'false'); }
  }

  $('details-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!state.currentJob) return;
    const submit = event.submitter || $('details-form').querySelector('button[type="submit"]');
    submit.disabled = true; $('detail-error').textContent = '';
    try {
      await api(`/api/jobs/${encodeURIComponent(state.currentJob.id)}`, {method:'PATCH', body:JSON.stringify({stage:$('detail-stage').value, due_date:$('detail-due').value, notes:$('detail-notes').value})});
      closeDetails(); toast('Notes saved'); await loadJobs();
    } catch (error) { $('detail-error').textContent = error.message; toast(error.message); }
    finally { submit.disabled = false; }
  });
  $('close-dialog').addEventListener('click', closeDetails);
  $('details').addEventListener('close', () => { state.currentJob = null; clearJobHash(); });
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  $('search').addEventListener('input', () => { clearTimeout(searchDebounce); searchDebounce = setTimeout(() => { state.offset = 0; void loadJobs(); }, 220); });
  ['programme', 'stage', 'sort', 'match-status', 'availability', 'uk-only', 'new-only', 'deadline-soon', 'include-expired'].forEach(id => $(id).addEventListener('change', () => { state.offset = 0; void loadJobs(); }));
  $('clear').addEventListener('click', () => {
    $('search').value = ''; $('programme').value = ''; $('stage').value = ''; $('match-status').value = ''; $('availability').value = ''; $('sort').value = 'newest'; $('uk-only').checked = true;
    ['new-only', 'deadline-soon', 'include-expired'].forEach(id => $(id).checked = false); state.offset = 0; void loadJobs();
  });
  $('previous').addEventListener('click', () => { state.offset = Math.max(0, state.offset - pageSize); void loadJobs(); });
  $('next').addEventListener('click', () => { state.offset += pageSize; void loadJobs(); });
  $('refresh').addEventListener('click', async () => {
    const button = $('refresh'); button.disabled = true; button.textContent = 'Starting pipeline…';
    try { await api('/api/refresh', {method:'POST'}); toast('Pipeline started. Existing roles remain available.'); await loadStatus(); }
    catch (error) { toast(error.message); button.disabled = false; button.textContent = 'Refresh pipeline ↻'; }
  });
  $('profile-form').addEventListener('submit', saveProfile);
  $('profile-form').addEventListener('input', event => { if (event.target && event.target.id) state.profileDirtyFields.add(event.target.id); });
  $('profile-form').addEventListener('change', event => { if (event.target && event.target.id) state.profileDirtyFields.add(event.target.id); });
  $('profile-cancel').addEventListener('click', () => { state.profileDirtyFields.clear(); void loadProfile(); });
  window.addEventListener('hashchange', () => { void openHashJob(); });
  document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) && !$('details').open) { event.preventDefault(); $('search').focus(); } });
  try { document.documentElement.dataset.theme = localStorage.getItem('argus-tracker-theme') || 'dark'; } catch { document.documentElement.dataset.theme = 'dark'; }
  $('theme').addEventListener('click', () => { const theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'; document.documentElement.dataset.theme = theme; try { localStorage.setItem('argus-tracker-theme', theme); } catch {} });
  void loadJobs(); void loadStatus(); setInterval(() => { void loadStatus(); }, 5000);
})();
