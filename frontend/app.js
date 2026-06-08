// app.js

const API_KEY = '';  // Set if API_KEY is configured in .env; leave empty for dev

// ─── State ────────────────────────────────────────────────────────────────────
let selectedAccountId = null;
let currentFormSpec   = null;
const lockGroupUserSet = new Set();  // field names the user has explicitly entered

// ─── Phase colours matching CSS variables ─────────────────────────────────────
const PHASE_CLASSES = {
  interpretation: 'phase-perceive',
  selection:      'phase-plan',
  form:           'phase-plan',    // same colour as selection — it's still a planning phase
  execution:      'phase-act',
  log:            'phase-log',
  error:          'phase-observe',
};

// ─── Tag classification for colour coding ─────────────────────────────────────
const TAG_CLASS_MAP = {
  'Refund eligible':          'tag-ok',
  'High value':               'tag-info',
  'No refund':                'tag-error',
  'Refund escalates':         'tag-warn',
  'Blocked':                  'tag-error',
  'Has pending operation':    'tag-warn',
  'Currently paused':         'tag-warn',
  'Plan change blocked':      'tag-warn',
};

// ─── Archetype loading ────────────────────────────────────────────────────────
async function loadArchetypes() {
  const grid = document.getElementById('archetype-grid');
  try {
    const res = await fetch('/accounts');
    const archetypes = await res.json();
    renderArchetypes(archetypes);
  } catch (err) {
    grid.innerHTML = `<div style="color:var(--error);font-size:0.82rem">Failed to load profiles: ${escapeHtml(err.message)}</div>`;
  }
}

function renderArchetypes(archetypes) {
  const grid = document.getElementById('archetype-grid');
  grid.innerHTML = '';

  archetypes.forEach(a => {
    const card = document.createElement('div');
    card.className = 'archetype-card';
    card.dataset.id = a.id;

    const tagsHtml = (a.tags || []).map(t => {
      const cls = TAG_CLASS_MAP[t] || '';
      return `<span class="tag ${cls}">${escapeHtml(t)}</span>`;
    }).join('');

    card.innerHTML = `
      <div class="archetype-card-label">${escapeHtml(a.label)}</div>
      <div class="archetype-tags">${tagsHtml}</div>
      <div class="archetype-desc">${escapeHtml(a.description)}</div>
    `;

    card.addEventListener('click', () => selectArchetype(a));
    grid.appendChild(card);
  });

  // ── Custom account card (always last) ───────────────────────────────────────
  const customCard = document.createElement('div');
  customCard.className = 'archetype-card';
  customCard.dataset.id = 'custom';
  customCard.innerHTML = `
    <div class="archetype-card-label">⊕ Custom Account</div>
    <div class="archetype-desc">Configure any combination of account parameters to test edge cases.</div>
  `;
  customCard.addEventListener('click', openCustomForm);
  grid.appendChild(customCard);
}

function openCustomForm() {
  const form   = document.getElementById('custom-form');
  const isOpen = form.style.display === 'block';

  if (isOpen) {
    form.style.display = 'none';
    if (selectedAccountId !== 'custom') {
      document.querySelectorAll('.archetype-card').forEach(c => c.classList.remove('selected'));
    }
    return;
  }

  form.style.display = 'block';
  document.querySelectorAll('.archetype-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.id === 'custom');
  });

  const dateInput = document.getElementById('cf-last-payment-date');
  if (!dateInput.value) {
    const d = new Date();
    d.setDate(d.getDate() - 5);
    dateInput.value = d.toISOString().split('T')[0];
  }

  document.getElementById('account-detail').style.display = 'none';
}

async function applyCustomAccount() {
  const plan        = document.getElementById('cf-plan').value;
  const status      = document.getElementById('cf-status-select').value;
  const billing     = document.getElementById('cf-billing-cycle').value;
  const lastPayment = document.getElementById('cf-last-payment-date').value;
  const refundHist  = document.getElementById('cf-refund-history').checked;
  const pendingAct  = document.getElementById('cf-pending-action').checked;
  const statusMsg   = document.getElementById('cf-status-msg');

  if (!lastPayment) {
    statusMsg.textContent = 'Set a last payment date first.';
    return;
  }

  const btn = document.getElementById('cf-apply-btn');
  btn.disabled = true;
  statusMsg.textContent = 'Applying…';

  try {
    const res = await fetch('/accounts/custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plan,
        status,
        billing_cycle:           billing,
        last_payment_date:       lastPayment,
        refund_history_present:  refundHist,
        pending_action:          pendingAct,
      }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const { resolved_status: resolvedStatus = status } = await res.json();

    selectedAccountId = 'custom';

    document.querySelectorAll('.archetype-card').forEach(c => {
      c.classList.toggle('selected', c.dataset.id === 'custom');
    });

    const planLabel   = plan === 'black_card' ? 'Black Card' : 'Pass';
    const cycleLabel  = billing.charAt(0).toUpperCase() + billing.slice(1);
    const statusClass = resolvedStatus === 'active'
      ? 'tag-ok'
      : ['paused', 'cancelled'].includes(resolvedStatus)
        ? 'tag-warn'
        : 'tag-error';

    const tagsHtml = [
      `<span class="tag">${escapeHtml(planLabel)} · ${escapeHtml(cycleLabel)}</span>`,
      `<span class="tag ${statusClass}">${escapeHtml(resolvedStatus)}</span>`,
      refundHist ? `<span class="tag tag-warn">Refund history</span>` : '',
      pendingAct ? `<span class="tag tag-warn">Pending action</span>` : '',
    ].join('');

    const detail = document.getElementById('account-detail');
    detail.style.display = 'block';
    detail.innerHTML = `
      <div class="detail-id">custom</div>
      <div class="detail-row">${tagsHtml}</div>
    `;

    document.getElementById('custom-form').style.display = 'none';
    document.getElementById('no-account-notice').style.display = 'none';
    statusMsg.textContent = '';
  } catch (err) {
    statusMsg.textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

function selectArchetype(archetype) {
  selectedAccountId = archetype.id;

  document.getElementById('custom-form').style.display = 'none';

  document.querySelectorAll('.archetype-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.id === archetype.id);
  });

  const detail = document.getElementById('account-detail');
  detail.style.display = 'block';
  detail.innerHTML = `
    <div class="detail-id">${escapeHtml(archetype.id)}</div>
    <div class="detail-row">
      ${(archetype.tags || []).map(t => {
        const cls = TAG_CLASS_MAP[t] || '';
        return `<span class="tag ${cls}">${escapeHtml(t)}</span>`;
      }).join('')}
    </div>
  `;

  document.getElementById('no-account-notice').style.display = 'none';
}

// ─── Phase summary extractors ─────────────────────────────────────────────────
function phaseSummary(phase, data) {
  switch (phase) {
    case 'interpretation':
      return [
        data.classification || 'input received',
        data.account_id     ? `account: ${data.account_id}` : '',
        data.history_length ? `${data.history_length} prior turn(s)` : '',
      ].filter(Boolean).join(' · ');

    case 'selection': {
      if (data.intent === 'ambiguous') return 'ambiguous — clarifying question sent';
      if (data.halt)                  return `halted: ${data.halt}`;
      const tool = data.selected_tool || '—';
      const miss = data.missing_params?.length ? ` · missing: ${data.missing_params.join(', ')}` : '';
      const j    = data.justification ? ` — ${data.justification.slice(0, 60)}` : '';
      return `→ ${tool}${j}${miss}`;
    }

    case 'form':
      return `${data.tool_name || '—'} · missing: ${(data.missing_fields || []).join(', ')}`;

    case 'execution':
      if (data.confirmation_required) return `${data.tool_name} · awaiting confirmation`;
      if (data.has_error)             return `${data.tool_name} · error`;
      return `${data.tool_name || '—'} · ${data.halt_reason || 'executed'}`;

    case 'log': {
      const parts = [`log id ${data.log_id || '?'}`];
      if (data.verified_changes && data.verified_changes.length)
        parts.push(data.verified_changes[0].slice(0, 60));
      if (data.form_required) parts.push(`form: ${data.form_tool}`);
      return parts.join(' · ');
    }

    case 'error':
      return data.exception ? data.exception.slice(0, 80) : 'internal error';

    default:
      return '';
  }
}

// ─── Render helpers ───────────────────────────────────────────────────────────
function renderTrace(steps) {
  const container = document.getElementById('trace-container');
  container.innerHTML = '';

  steps.forEach(({ phase, data }) => {
    const phaseClass = PHASE_CLASSES[phase] || '';
    const summary    = phaseSummary(phase, data);
    const body       = JSON.stringify(data, null, 2);

    const details = document.createElement('details');
    details.className = `trace-step ${phaseClass}`;
    details.innerHTML = `
      <summary>
        <span class="phase-dot"></span>
        <span class="phase-name">${phase}</span>
        <span class="phase-summary">${summary}</span>
      </summary>
      <div class="trace-step-body">${escapeHtml(body)}</div>
    `;
    container.appendChild(details);
  });
}

function renderError(error) {
  const block = document.getElementById('error-block');
  if (!error) { block.style.display = 'none'; return; }

  block.style.display = 'block';
  document.getElementById('error-message').textContent =
    error.message || error.code || JSON.stringify(error);
  document.getElementById('error-raw').textContent = JSON.stringify(error, null, 2);
}

function setStatus(text, loading = false) {
  const el = document.getElementById('status');
  el.innerHTML = loading ? `<span class="spinner"></span> ${text}` : text;
}

function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function setBadge(haltReason, hasError) {
  const badge = document.getElementById('halt-badge');
  if (hasError) {
    badge.className = 'badge badge-error';
    badge.textContent = haltReason || 'error';
  } else if (haltReason === 'ambiguity' || haltReason === 'max_iterations') {
    badge.className = 'badge badge-warn';
    badge.textContent = haltReason;
  } else if (haltReason === 'confirmation_pending') {
    badge.className = 'badge badge-warn';
    badge.textContent = 'confirmation required';
  } else if (haltReason === 'form_required') {
    badge.className = 'badge badge-warn';
    badge.textContent = 'form required';
  } else {
    badge.className = 'badge badge-success';
    badge.textContent = haltReason || 'success';
  }
}

// ─── Session ID management ────────────────────────────────────────────────────
function getSessionId() {
  const input = document.getElementById('session-input');
  if (!input.value.trim()) {
    input.value = crypto.randomUUID();
  }
  return input.value.trim();
}

// ─── Confirmation controls ────────────────────────────────────────────────────
function showConfirmControls(show) {
  document.getElementById('confirm-controls').style.display = show ? 'flex' : 'none';
}

async function sendConfirmation(confirmed) {
  showConfirmControls(false);
  await _doRequest({ confirmed });
}

// ─── Form controls ────────────────────────────────────────────────────────────

function showFormControls(formSpec) {
  currentFormSpec = formSpec;
  lockGroupUserSet.clear();

  const container = document.getElementById('form-controls');
  container.innerHTML = '';
  container.style.display = 'block';

  const title = document.createElement('div');
  title.className = 'form-controls-title';
  title.textContent = 'Required details';
  container.appendChild(title);

  const grid = document.createElement('div');
  grid.className = 'form-fields-grid';
  container.appendChild(grid);

  const lockGroup = formSpec.lock_group || null;

  for (const field of (formSpec.fields || [])) {
    const row = document.createElement('div');
    row.className = 'form-field-row';
    if (field.type === 'boolean') row.classList.add('full-width');

    const label = document.createElement('label');
    label.className = 'form-field-label';
    label.htmlFor = `form-field-${field.name}`;
    label.textContent = field.label + (field.required !== false ? '' : ' (optional)');
    row.appendChild(label);

    let input;

    if (field.type === 'select') {
      input = document.createElement('select');
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = '— select —';
      input.appendChild(blank);
      for (const opt of (field.options || [])) {
        const option = document.createElement('option');
        option.value  = opt.value;
        option.textContent = opt.label;
        input.appendChild(option);
      }
    } else if (field.type === 'boolean') {
      const wrapper = document.createElement('label');
      wrapper.className = 'cf-check-label';
      input = document.createElement('input');
      input.type = 'checkbox';
      const span = document.createElement('span');
      span.textContent = field.hint || field.label;
      wrapper.appendChild(input);
      wrapper.appendChild(span);
      row.appendChild(wrapper);
    } else if (field.type === 'integer') {
      input = document.createElement('input');
      input.type = 'number';
      input.step = '1';
      if (field.min_value !== undefined) input.min = field.min_value;
      if (field.max_value !== undefined) input.max = field.max_value;
    } else {
      // date or fallback text
      input = document.createElement('input');
      input.type = field.type === 'date' ? 'date' : 'text';
    }

    input.id = `form-field-${field.name}`;
    input.dataset.fieldName = field.name;

    // Pre-fill extracted values
    if (field.prefilled !== undefined) {
      if (field.type === 'boolean') {
        input.checked = field.prefilled === 'True' || field.prefilled === 'true';
      } else {
        input.value = field.prefilled;
        if (lockGroup && lockGroup.fields.includes(field.name) && field.prefilled) {
          lockGroupUserSet.add(field.name);
        }
      }
    }

    // For non-boolean, append input to row here (boolean already appended via wrapper)
    if (field.type !== 'boolean') row.appendChild(input);

    if (field.hint && field.type !== 'boolean') {
      const hint = document.createElement('div');
      hint.className = 'form-field-hint';
      hint.textContent = field.hint;
      row.appendChild(hint);
    }

    // Lock group event listeners
    if (lockGroup && lockGroup.fields.includes(field.name)) {
      const handler = () => _onLockGroupChange(formSpec, field.name, input);
      input.addEventListener('input',  handler);
      input.addEventListener('change', handler);
    }

    grid.appendChild(row);
  }

  // Apply initial lock state for pre-filled values
  _applyLockGroupState(formSpec);

  // ── Pause live preview section (only for tools that supply preview_context) ──
  if (formSpec.preview_context) {
    const preview = document.createElement('div');
    preview.className = 'pause-preview';
    preview.innerHTML = `
      <div class="pause-preview-header">Changes</div>
      <table class="pause-changes-table" id="pause-preview-table">
        <tbody id="pause-preview-body">
          <tr class="preview-muted"><td colspan="2">Fill in the period above to see changes</td></tr>
        </tbody>
      </table>
      <div id="pause-preview-note">
        <div class="pause-notice">
          Pauses cannot be cancelled once applied. For any changes to an active pause, contact support@fitness.com.
        </div>
      </div>
    `;
    container.appendChild(preview);
    _initPausePreview(formSpec);
  }

  // Footer: confirm + cancel
  const footer = document.createElement('div');
  footer.className = 'form-footer';
  footer.innerHTML = `
    <button id="form-submit-btn" onclick="submitForm()">Confirm</button>
    <button id="form-cancel-btn" onclick="cancelForm()">Cancel</button>
    <span id="form-error-msg"></span>
  `;
  container.appendChild(footer);
}

function hideFormControls() {
  currentFormSpec = null;
  lockGroupUserSet.clear();
  const container = document.getElementById('form-controls');
  container.style.display = 'none';
  container.innerHTML = '';
}

function submitForm() {
  const spec = currentFormSpec;
  if (!spec) return;

  const errEl = document.getElementById('form-error-msg');
  errEl.textContent = '';

  const form_data = {};
  for (const field of (spec.fields || [])) {
    const el = document.getElementById(`form-field-${field.name}`);
    if (!el) continue;

    if (field.type === 'boolean') {
      form_data[field.name] = el.checked;
    } else {
      const val = el.value.trim();
      if (field.required !== false && !val) {
        errEl.textContent = `"${field.label}" is required.`;
        return;
      }
      if (val) {
        form_data[field.name] = field.type === 'integer' ? parseInt(val, 10) : val;
      }
    }
  }

  hideFormControls();
  _doRequest({ confirmed: true, form_data });
}

function cancelForm() {
  hideFormControls();
  _doRequest({ confirmed: false });
}

// ─── Date helpers (UTC-safe, shared by lock group and pause preview) ──────────

function _toUTC(s) {
  const [y, m, d] = s.split('-').map(Number);
  return Date.UTC(y, m - 1, d);
}

function _fromUTC(ms) {
  return new Date(ms).toISOString().split('T')[0];
}

// ─── Pause live preview ───────────────────────────────────────────────────────

function _updatePausePreview(formSpec) {
  const ctx     = formSpec.preview_context;
  const bodyEl  = document.getElementById('pause-preview-body');
  const noteEl  = document.getElementById('pause-preview-note');
  if (!ctx || !bodyEl) return;

  const startEl    = document.getElementById('form-field-start_date');
  const durationEl = document.getElementById('form-field-duration_days');
  const endEl      = document.getElementById('form-field-end_date');
  const hasDocsEl  = document.getElementById('form-field-has_documentation');

  const startDate   = startEl?.value   || '';
  const durationRaw = durationEl?.value || '';
  const endDate     = endEl?.value      || '';
  const hasDocs     = hasDocsEl?.checked || false;
  const duration    = parseInt(durationRaw, 10);

  // Documentation escalation path
  if (hasDocs) {
    bodyEl.innerHTML = `
      <tr><td>Pause period</td><td>${escapeHtml(_pausePeriodText(startDate, endDate))}</td></tr>
      <tr><td>Fee</td><td>Waived — pending document validation</td></tr>
      <tr><td>New renewal</td><td>Set once documents verified</td></tr>
      <tr><td>Guest pass</td><td>${ctx.has_guest_pass ? 'Paused during pause period' : 'N/A'}</td></tr>
    `;
    if (noteEl) noteEl.innerHTML = `
      <div class="pause-escalation-note">
        Your request will be escalated to support for document validation.
        The pause is applied once staff verify your documents.
        Submit to <strong>support@fitness.com</strong>.
      </div>
      <div class="pause-notice">
        Pauses cannot be cancelled once applied. For any changes to an active pause, contact support@fitness.com.
      </div>
    `;
    return;
  }

  // Large duration escalation path (91–180 days, no docs)
  if (!isNaN(duration) && duration >= ctx.escalation_threshold_days) {
    bodyEl.innerHTML = `
      <tr><td>Duration</td><td>${duration} days</td></tr>
      <tr><td>Outcome</td><td>Escalation to support required (4–6 month pause)</td></tr>
    `;
    if (noteEl) noteEl.innerHTML = `
      <div class="pause-escalation-note">
        Pauses over ${ctx.fee_max_days} days require manual approval by support.
        Your request will be submitted for review.
      </div>
      <div class="pause-notice">
        Pauses cannot be cancelled once applied. For any changes to an active pause, contact support@fitness.com.
      </div>
    `;
    return;
  }

  // Standard path — show live-calculated fee and dates
  if (isNaN(duration) || duration < ctx.min_days || (!startDate && !endDate)) {
    bodyEl.innerHTML = `<tr class="preview-muted"><td colspan="2">Fill in the period above to see changes</td></tr>`;
    if (noteEl) noteEl.innerHTML = `
      <div class="pause-notice">
        Pauses cannot be cancelled once applied. For any changes to an active pause, contact support@fitness.com.
      </div>
    `;
    return;
  }

  const fee         = Math.ceil(duration / 30) * ctx.fee_per_period;
  const periods     = Math.ceil(duration / 30);
  const renewalDate = _fromUTC(_toUTC(ctx.subscription_valid_until) + duration * 86400000);
  const periodText  = _pausePeriodText(startDate, endDate);
  const guestText   = ctx.has_guest_pass
    ? (startDate && endDate ? `Paused ${startDate} → ${endDate}` : 'Paused during pause period')
    : 'N/A — Pass plan has no guest pass';

  bodyEl.innerHTML = `
    <tr><td>Pause period</td><td>${escapeHtml(periodText)}</td></tr>
    <tr><td>Pause fee</td><td>€${fee.toFixed(2)} (€${ctx.fee_per_period}/30-day period × ${periods})</td></tr>
    <tr><td>New renewal</td><td>${escapeHtml(renewalDate)}</td></tr>
    <tr><td>Guest pass</td><td>${escapeHtml(guestText)}</td></tr>
  `;
  if (noteEl) noteEl.innerHTML = `
    <div class="pause-notice">
      Pauses cannot be cancelled once applied. For any changes to an active pause, contact support@fitness.com.
    </div>
  `;
}

function _pausePeriodText(start, end) {
  if (start && end) return `${start} → ${end}`;
  if (start)        return `${start} → (end not set)`;
  if (end)          return `(start not set) → ${end}`;
  return '—';
}

function _initPausePreview(formSpec) {
  if (!formSpec.preview_context) return;
  const watchFields = ['start_date', 'duration_days', 'end_date', 'has_documentation'];
  for (const name of watchFields) {
    const el = document.getElementById(`form-field-${name}`);
    if (!el) continue;
    const update = () => _updatePausePreview(formSpec);
    el.addEventListener('input',  update);
    el.addEventListener('change', update);
  }
  _updatePausePreview(formSpec);
}

// ─── Lock group logic ─────────────────────────────────────────────────────────

function _onLockGroupChange(formSpec, fieldName, input) {
  const isEmpty = input.value.trim() === '';

  if (isEmpty) {
    // Field cleared → remove from userSet, unlock all
    lockGroupUserSet.delete(fieldName);
    _unlockAllLockGroup(formSpec);
  } else {
    lockGroupUserSet.add(fieldName);
  }

  _applyLockGroupState(formSpec);
}

function _unlockAllLockGroup(formSpec) {
  for (const name of (formSpec.lock_group?.fields || [])) {
    const el = document.getElementById(`form-field-${name}`);
    if (el) {
      el.disabled = false;
      el.classList.remove('field-locked');
    }
  }
}

function _applyLockGroupState(formSpec) {
  const lg = formSpec.lock_group;
  if (!lg) return;

  // Always start by ensuring all lock group fields are unlocked
  _unlockAllLockGroup(formSpec);

  if (lockGroupUserSet.size < 2) return;

  // Find the missing field (not in userSet)
  const toCalc = lg.fields.find(f => !lockGroupUserSet.has(f));
  if (!toCalc) return;  // all 3 set (only possible if all were pre-filled)

  // Collect current values
  const vals = {};
  for (const f of lg.fields) {
    const el = document.getElementById(`form-field-${f}`);
    if (el) vals[f] = el.value;
  }

  const calculated = _calculateLockField(toCalc, vals);
  if (calculated === null) return;

  const el = document.getElementById(`form-field-${toCalc}`);
  if (el) {
    el.value = calculated;
    el.disabled = true;
    el.classList.add('field-locked');
  }

  // Refresh pause preview — programmatic field update won't fire input/change events
  if (formSpec.preview_context) _updatePausePreview(formSpec);
}

function _calculateLockField(targetField, vals) {
  const { start_date, duration_days, end_date } = vals;

  if (targetField === 'end_date' && start_date && duration_days) {
    return _fromUTC(_toUTC(start_date) + parseInt(duration_days, 10) * 86400000);
  }

  if (targetField === 'duration_days' && start_date && end_date) {
    const days = Math.round((_toUTC(end_date) - _toUTC(start_date)) / 86400000);
    return days > 0 ? String(days) : null;
  }

  if (targetField === 'start_date' && duration_days && end_date) {
    return _fromUTC(_toUTC(end_date) - parseInt(duration_days, 10) * 86400000);
  }

  return null;
}

// ─── Main request handler ─────────────────────────────────────────────────────
async function submitRequest() {
  const message = document.getElementById('message-input').value.trim();
  if (!message) return;

  if (!selectedAccountId) {
    document.getElementById('no-account-notice').style.display = 'block';
    return;
  }

  showConfirmControls(false);
  hideFormControls();
  await _doRequest({ message });
}

async function _doRequest({ message, confirmed = null, form_data = null }) {
  const resolvedMessage = message ?? document.getElementById('message-input').value.trim() ?? '';

  const sessionId    = getSessionId();
  const allowHistory = document.getElementById('history-toggle').checked;
  const btn          = document.getElementById('submit-btn');

  btn.disabled = true;
  setStatus('Running…', true);
  document.getElementById('result-area').style.display = 'none';
  document.getElementById('error-block').style.display = 'none';

  const headers = { 'Content-Type': 'application/json' };
  if (API_KEY) headers['X-API-Key'] = API_KEY;

  const body = {
    message: resolvedMessage,
    session_id: sessionId,
    allow_history_reference: allowHistory,
    account_id: selectedAccountId,
  };
  if (confirmed  !== null) body.confirmed  = confirmed;
  if (form_data  !== null) body.form_data  = form_data;

  let data;
  try {
    const res = await fetch('/run', {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });

    const raw = await res.text();
    try {
      data = JSON.parse(raw);
    } catch {
      throw new Error(`HTTP ${res.status}: ${raw.slice(0, 200)}`);
    }

    if (!res.ok) {
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
  } catch (err) {
    setStatus('');
    renderError({ code: 'REQUEST_FAILED', message: err.message });
    document.getElementById('error-block').style.display = 'block';
    document.getElementById('result-area').style.display = 'block';
    btn.disabled = false;
    return;
  }

  const hasError    = !!data.error;
  const isPending   = data.halt_reason === 'confirmation_pending';
  const isFormRequired = data.halt_reason === 'form_required';

  setBadge(data.halt_reason, hasError);
  document.getElementById('meta-text').textContent =
    `session ${data.session_id} · ${data.iterations_used} iteration(s)` +
    (selectedAccountId ? ` · ${selectedAccountId}` : '');

  document.getElementById('response-text').textContent = data.result || '';
  renderTrace(data.trace || []);
  renderError(data.error || null);

  document.getElementById('raw-json').textContent = JSON.stringify(data, null, 2);
  document.getElementById('result-area').style.display = 'block';

  showConfirmControls(isPending);

  if (isFormRequired && data.form_spec) {
    showFormControls(data.form_spec);
  } else {
    hideFormControls();
  }

  setStatus('');
  btn.disabled = false;
}

// ─── Submit on Ctrl+Enter ─────────────────────────────────────────────────────
document.getElementById('message-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    submitRequest();
  }
});

// ─── Init ─────────────────────────────────────────────────────────────────────
loadArchetypes();
