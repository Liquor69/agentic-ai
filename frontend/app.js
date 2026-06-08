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
  safety:         'phase-plan',
  form:           'phase-plan',
  execution:      'phase-act',
  log:            'phase-log',
  error:          'phase-observe',
};

const PHASE_LABELS = {
  interpretation: 'Read your request',
  selection:      'Chose an action',
  safety:         'Safety check',
  form:           'Needs more info',
  execution:      'Took action',
  log:            'Saved the record',
  error:          'Something went wrong',
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

// ─── Phase summary extractors (collapsed header line) ────────────────────────
function phaseSummary(phase, data) {
  switch (phase) {
    case 'interpretation':
      return [
        humanizeName(data.classification || 'input received'),
        data.account_id ? data.account_id : '',
      ].filter(Boolean).join(' · ');

    case 'selection': {
      if (data.intent === 'ambiguous') return 'needs clarification';
      if (data.halt)                   return `halted: ${humanizeName(data.halt)}`;
      const tools = data.selected_tools || (data.selected_tool ? [data.selected_tool] : []);
      const tool  = humanizeName(tools[0] || '—');
      const miss  = data.missing_params?.length
        ? ` · needs ${data.missing_params.length} more detail${data.missing_params.length !== 1 ? 's' : ''}`
        : '';
      return `using "${tool}"${miss}`;
    }

    case 'safety':
      return data.passed ? 'all checks passed' : 'check failed';

    case 'form': {
      const n = (data.missing_fields || []).length;
      return `${humanizeName(data.tool_name || '—')} — needs ${n} more detail${n !== 1 ? 's' : ''}`;
    }

    case 'execution': {
      const attempted = data.tools_attempted || (data.tool_name ? [data.tool_name] : []);
      const label     = attempted.map(humanizeName).join(', ') || '—';
      const failed    = (data.results || []).some(r => r.status === 'tool_failure');
      if (data.confirmation_required) return `${label} — awaiting confirmation`;
      if (failed || data.has_error)   return `${label} — error`;
      return `${label} — done`;
    }

    case 'log': {
      const n = data.verified_changes?.length || 0;
      return `log #${data.log_id || '?'} · ${n} change${n !== 1 ? 's' : ''} recorded`;
    }

    case 'error':
      return 'see details below';

    default:
      return '';
  }
}

// ─── Trace body helpers ───────────────────────────────────────────────────────
function humanizeName(name) {
  return (name || '').replace(/_/g, ' ');
}

function narrative(html) {
  return `<div class="trace-narrative">${html}</div>`;
}

function sectionLabel(text) {
  return `<div class="trace-section-label">${escapeHtml(text)}</div>`;
}

function _resultKvs(r) {
  const kvs = [];
  if (r.message)               kvs.push(['Message',      r.message]);
  if (r.description)           kvs.push(['Details',      r.description]);
  if (r.action)                kvs.push(['Action',       humanizeName(r.action)]);
  if (r.refund_amount != null) kvs.push(['Refund',       `€${r.refund_amount}`]);
  if (r.new_status)            kvs.push(['New status',   humanizeName(r.new_status)]);
  if (r.pause_start)           kvs.push(['Pause start',  r.pause_start]);
  if (r.pause_end)             kvs.push(['Pause end',    r.pause_end]);
  if (r.next_billing_date)     kvs.push(['Next billing', r.next_billing_date]);
  if (r.fee != null)           kvs.push(['Fee charged',  `€${r.fee}`]);
  if (r.reason)                kvs.push(['Reason',       r.reason]);
  return kvs;
}

function kvList(pairs) {
  const rows = pairs.map(([k, v]) =>
    `<div class="trace-kv"><span class="trace-kv-key">${escapeHtml(k)}</span><span class="trace-kv-val">${escapeHtml(String(v))}</span></div>`
  ).join('');
  return `<div class="trace-kv-list">${rows}</div>`;
}

function renderTraceBody(phase, data) {
  const parts = [];

  switch (phase) {
    case 'interpretation': {
      const intent = humanizeName(data.classification || data.intent || 'input received');
      parts.push(narrative(`Understood your request as: <strong>${escapeHtml(intent)}</strong>`));
      const kvs = [];
      if (data.account_id)     kvs.push(['Account',     data.account_id]);
      if (data.history_length) kvs.push(['Prior turns', String(data.history_length)]);
      if (kvs.length)          parts.push(kvList(kvs));
      break;
    }

    case 'selection': {
      const selTools = data.selected_tools || (data.selected_tool ? [data.selected_tool] : []);
      if (data.intent === 'ambiguous' || data.halt || !selTools.length) {
        parts.push(narrative(`Could not determine a clear action — asked a clarifying question.`));
        if (data.halt) parts.push(kvList([['Reason', humanizeName(data.halt)]]));
      } else {
        const toolNames = selTools.map(humanizeName).join(', ');
        parts.push(narrative(`Decided to use <strong>${escapeHtml(toolNames)}</strong> to handle this.`));

        // Old structure: justification + expected_result
        const kvs = [];
        if (data.justification)    kvs.push(['Why',      data.justification]);
        if (data.expected_result)  kvs.push(['Expected', data.expected_result]);
        if (kvs.length) parts.push(kvList(kvs));

        // New structure: extracted_params_per_tool
        if (data.extracted_params_per_tool) {
          const entries = Object.entries(data.extracted_params_per_tool);
          if (entries.length) {
            parts.push(sectionLabel('Parameters detected'));
            parts.push(kvList(entries.map(([t, p]) => [
              humanizeName(t),
              p.length ? p.join(', ') : 'None — no extra details needed',
            ])));
          }
        }

        // Old structure: constraint_validation
        if (data.constraint_validation && Object.keys(data.constraint_validation).length) {
          parts.push(sectionLabel('Policy checks'));
          const chips = Object.entries(data.constraint_validation).map(([key, val]) => {
            const ok  = val === true || val === 'pass' || val === 'ok' || val === 'clear';
            const cls = ok ? 'trace-chip-ok' : 'trace-chip-warn';
            return `<span class="trace-chip ${cls}">${ok ? '✓' : '⚠'} ${escapeHtml(humanizeName(key))}</span>`;
          });
          parts.push(`<div class="trace-chips">${chips.join('')}</div>`);
        }

        if (data.missing_params?.length) {
          parts.push(sectionLabel('Still needed'));
          const chips = data.missing_params.map(p =>
            `<span class="trace-chip trace-chip-warn">⚠ ${escapeHtml(humanizeName(p))}</span>`
          );
          parts.push(`<div class="trace-chips">${chips.join('')}</div>`);
        }
      }
      break;
    }

    case 'form': {
      const tool = humanizeName(data.tool_name || '—');
      parts.push(narrative(`A few more details are needed before <strong>${escapeHtml(tool)}</strong> can run.`));
      if (data.missing_fields?.length) {
        parts.push(sectionLabel('Missing information'));
        const chips = data.missing_fields.map(f =>
          `<span class="trace-chip trace-chip-warn">${escapeHtml(humanizeName(f))}</span>`
        );
        parts.push(`<div class="trace-chips">${chips.join('')}</div>`);
      }
      break;
    }

    case 'execution': {
      if (data.results && Array.isArray(data.results)) {
        const failed  = data.results.filter(r => r.status === 'tool_failure');
        const success = data.results.filter(r => r.status !== 'tool_failure');

        if (data.confirmation_required) {
          const names = (data.tools_attempted || []).map(humanizeName).join(', ');
          parts.push(narrative(`<strong>${escapeHtml(names)}</strong> is ready — waiting for your confirmation before making changes.`));
        } else if (failed.length) {
          // Any failure → lead with the failure; suppress unrelated successes
          parts.push(narrative(`The action could not be completed.`));
          for (const r of failed) {
            if (failed.length > 1) parts.push(sectionLabel(humanizeName(r.tool)));
            if (r.error) {
              const kvs = [];
              if (r.error.code)    kvs.push(['Error code', r.error.code]);
              if (r.error.message) kvs.push(['Details',    r.error.message]);
              if (kvs.length) parts.push(kvList(kvs));
            }
          }
        } else {
          // All succeeded — only surface results that have meaningful output
          const meaningful = success.filter(r => r.result && _resultKvs(r.result).length > 0);
          if (meaningful.length) {
            const names = meaningful.map(r => humanizeName(r.tool)).join(', ');
            parts.push(narrative(`<strong>${escapeHtml(names)}</strong> completed successfully.`));
            for (const r of meaningful) {
              if (meaningful.length > 1) parts.push(sectionLabel(humanizeName(r.tool)));
              parts.push(kvList(_resultKvs(r.result)));
            }
          } else {
            const names = (data.tools_attempted || success.map(r => r.tool)).map(humanizeName).join(', ');
            parts.push(narrative(`<strong>${escapeHtml(names || '—')}</strong> completed.`));
          }
        }
      } else {
        // Old structure: tool_name, has_error, result
        const tool = humanizeName(data.tool_name || '—');
        if (data.confirmation_required) {
          parts.push(narrative(`<strong>${escapeHtml(tool)}</strong> is ready — waiting for your confirmation before making changes.`));
        } else if (data.has_error) {
          parts.push(narrative(`<strong>${escapeHtml(tool)}</strong> encountered an error.`));
        } else {
          parts.push(narrative(`<strong>${escapeHtml(tool)}</strong> completed successfully.`));
        }
        if (data.result && typeof data.result === 'object') {
          const kvs = _resultKvs(data.result);
          if (kvs.length) parts.push(kvList(kvs));
        }
      }
      break;
    }

    case 'safety': {
      const passed      = data.passed !== false;
      const toolChecked = (data.tools_checked || []).map(humanizeName);
      parts.push(passed
        ? narrative(`All safety checks passed — the request is safe to execute.`)
        : narrative(`A safety check failed — this request was blocked.`));
      if (toolChecked.length) {
        const chips = toolChecked.map(t =>
          `<span class="trace-chip ${passed ? 'trace-chip-ok' : 'trace-chip-error'}">${passed ? '✓' : '✗'} ${escapeHtml(t)}</span>`
        );
        parts.push(sectionLabel('Tools checked'));
        parts.push(`<div class="trace-chips">${chips.join('')}</div>`);
      }
      break;
    }

    case 'log': {
      parts.push(narrative(`This interaction was recorded (log <strong>#${escapeHtml(String(data.log_id || '—'))}</strong>).`));
      if (data.verified_changes?.length) {
        parts.push(sectionLabel('Changes made'));
        const items = data.verified_changes.map(c => `<li>${escapeHtml(c)}</li>`).join('');
        parts.push(`<ul class="trace-changes-list">${items}</ul>`);
      } else {
        parts.push(kvList([['Changes', 'None — read-only operation']]));
      }
      if (data.form_required) {
        parts.push(`<div class="trace-chips"><span class="trace-chip trace-chip-warn">Form required: ${escapeHtml(humanizeName(data.form_tool || ''))}</span></div>`);
      }
      break;
    }

    case 'error': {
      parts.push(narrative(`An error occurred while processing your request.`));
      const kvs = [];
      if (data.code)      kvs.push(['Error code', data.code]);
      if (data.exception) kvs.push(['Details',    data.exception]);
      if (kvs.length) parts.push(kvList(kvs));
      break;
    }

    default: {
      const kvs = Object.entries(data)
        .filter(([, v]) => v != null && typeof v !== 'object')
        .map(([k, v]) => [humanizeName(k), String(v)]);
      if (kvs.length) parts.push(kvList(kvs));
      break;
    }
  }

  parts.push(`
    <details class="trace-raw-toggle">
      <summary></summary>
      <div class="trace-raw-body">${escapeHtml(JSON.stringify(data, null, 2))}</div>
    </details>
  `);

  return parts.join('');
}

// ─── Render helpers ───────────────────────────────────────────────────────────
function renderTrace(steps) {
  const container = document.getElementById('trace-container');
  container.innerHTML = '';

  steps.forEach(({ phase, data }, idx) => {
    const phaseClass = PHASE_CLASSES[phase] || '';
    const label      = PHASE_LABELS[phase] || phase;
    const summary    = phaseSummary(phase, data);

    const details = document.createElement('details');
    details.className = `trace-step ${phaseClass}`;
    if (idx === 0) details.open = true;

    details.innerHTML = `
      <summary>
        <span class="step-num">${idx + 1}</span>
        <span class="phase-dot"></span>
        <span class="phase-name">${escapeHtml(label)}</span>
        <span class="phase-summary">${escapeHtml(summary)}</span>
      </summary>
      <div class="trace-step-body">${renderTraceBody(phase, data)}</div>
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

function cleanResponseText(text) {
  if (!text) return '';
  // Extract "answer" field from ```json blocks; strip everything else
  return text.replace(/```json\n([\s\S]*?)```/g, (_, json) => {
    try {
      const obj = JSON.parse(json);
      return obj.answer ? '\n' + obj.answer : '';
    } catch {
      return '';
    }
  }).trim();
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

  document.getElementById('response-text').textContent = cleanResponseText(data.result || '');
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
