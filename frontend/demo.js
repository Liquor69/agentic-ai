/* demo.js — appointment assistant chat demo */

// ── Phase labels ──────────────────────────────────────────────────────────────
const PHASE_LABELS = {
  interpretation: "Request understood",
  selection:      "Action selected",
  safety:         "Safety check",
  form:           "Details needed",
  execution:      "Action taken",
  log:            "Logged",
  error:          "Error",
};

// ── Demo scenarios ─────────────────────────────────────────────────────────────
const SCENARIOS = [
  {
    id:         "client-reschedule-ok",
    clientName: "Sophie Bernard",
    title: "Free reschedule — 3 days out",
    sub:   "Sophie has a session booked in 3 days. Rescheduling is free (24h+ notice).",
    tags: [
      { label: "Reschedule", cls: "ok" },
      { label: "No fee",     cls: "ok" },
    ],
    examples: [
      "Can I move my appointment to next Monday?",
      "I need to reschedule — do you have anything on Wednesday?",
      "Can we shift my session to next week?",
    ],
  },
  {
    id:         "client-cancel-sameday",
    clientName: "Marco Rossi",
    title: "Same-day cancellation — €30 fee",
    sub:   "Marco has an appointment today. Cancelling now triggers the late-cancellation fee.",
    tags: [
      { label: "Cancel today", cls: "fee" },
      { label: "€30 fee",     cls: "fee" },
    ],
    examples: [
      "I need to cancel my appointment today.",
      "Something came up — please cancel my session.",
      "Can you cancel my booking for today?",
    ],
  },
  {
    id:         "client-new",
    clientName: "Amelia Chen",
    title: "New client — wants to book",
    sub:   "Amelia has no existing booking and wants to schedule her first session.",
    tags: [
      { label: "New booking", cls: "pending" },
    ],
    examples: [
      "Hi, I’d like to book an appointment.",
      "Do you have anything available next Thursday?",
      "Can I schedule a session for next week?",
    ],
  },
  {
    id:         "client-cancel-ok",
    clientName: "Thomas Müller",
    title: "Free cancellation — later this week",
    sub:   "Thomas has a consultation later this week. Cancelling or rescheduling is free.",
    tags: [
      { label: "Cancel", cls: "ok" },
      { label: "No fee", cls: "ok" },
    ],
    examples: [
      "I’d like to cancel my appointment.",
      "Can I reschedule my session to next week?",
      "Please cancel my booking — something came up.",
    ],
  },
];

// ── App state ──────────────────────────────────────────────────────────────────
const state = {
  sessionId:      crypto.randomUUID(),
  activeClientId: null,
  isLoading:      false,
  lastMessage:    null,
  loadingEl:      null,
};

// calendarState: Map<'YYYY-MM-DD', {type: 'manual'|'system', label: string}>
const calendarState = new Map();

// ── DOM refs ───────────────────────────────────────────────────────────────────
const scenariosEl  = document.getElementById("scenarios");
const examplesEl   = document.getElementById("examples");
const messageInput = document.getElementById("messageInput");
const runBtn       = document.getElementById("runBtn");
const spinner      = document.getElementById("spinner");
const chatThread   = document.getElementById("chatThread");
const calBtn       = document.getElementById("calBtn");
const calPanel     = document.getElementById("calPanel");
const calGrid      = document.getElementById("calGrid");
const resetBtn     = document.getElementById("resetBtn");


// ── Scenario rendering ─────────────────────────────────────────────────────────

function renderScenarios() {
  scenariosEl.innerHTML = "";
  SCENARIOS.forEach(sc => {
    const card = document.createElement("button");
    card.className  = "scenario-card";
    card.dataset.id = sc.id;
    card.innerHTML  = `
      <div class="scenario-radio"></div>
      <div class="scenario-body">
        <div class="scenario-title">${esc(sc.title)}</div>
        <div class="scenario-sub">${esc(sc.sub)}</div>
        <div class="scenario-tags">
          ${sc.tags.map(t => `<span class="scenario-tag ${t.cls}">${esc(t.label)}</span>`).join("")}
        </div>
      </div>
    `;
    card.addEventListener("click", () => selectScenario(sc.id));
    scenariosEl.appendChild(card);
  });
}

function selectScenario(id) {
  state.activeClientId = id;
  document.querySelectorAll(".scenario-card").forEach(c => {
    c.classList.toggle("active", c.dataset.id === id);
  });
  const sc = SCENARIOS.find(s => s.id === id);
  renderExamples(sc?.examples ?? []);
  messageInput.focus();
}

function renderExamples(examples) {
  examplesEl.innerHTML = "";
  examples.forEach(ex => {
    const chip = document.createElement("button");
    chip.className   = "example-chip";
    chip.textContent = ex;
    chip.addEventListener("click", () => {
      messageInput.value = ex;
      messageInput.focus();
    });
    examplesEl.appendChild(chip);
  });
}


// ── Calendar ───────────────────────────────────────────────────────────────────

function buildCalendar() {
  calGrid.innerHTML = "";

  const today = new Date();
  today.setHours(0, 0, 0, 0);

  // ISO week starts Monday: Sun=0 becomes 6
  const todayDow = (today.getDay() + 6) % 7;

  for (let i = 0; i < todayDow; i++) {
    const empty = document.createElement("div");
    empty.className = "cal-day empty";
    calGrid.appendChild(empty);
  }

  for (let offset = 0; offset < 30; offset++) {
    const d = new Date(today);
    d.setDate(today.getDate() + offset);
    const dateStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
    const entry   = calendarState.get(dateStr);
    const isToday = offset === 0;

    const cell = document.createElement("div");
    cell.dataset.date = dateStr;

    if (d.getDay() === 0) {
      cell.className = `cal-day closed${isToday ? " today" : ""}`;
      cell.textContent = d.getDate();
      calGrid.appendChild(cell);
      continue;
    }

    if (entry?.type === "system") {
      cell.className = `cal-day busy-system${isToday ? " today" : ""}`;
      const firstName = entry.label.split(" ")[0];
      cell.innerHTML  = `<span class="cal-day-num">${d.getDate()}</span><span class="cal-day-name">${esc(firstName)}</span>`;
    } else if (entry?.type === "manual") {
      cell.className   = `cal-day busy-manual${isToday ? " today" : ""}`;
      cell.textContent = d.getDate();
    } else if (isToday) {
      cell.className   = "cal-day today";
      cell.textContent = d.getDate();
    } else {
      cell.className   = "cal-day free";
      cell.textContent = d.getDate();
    }

    cell.addEventListener("click", e => handleDayClick(e, cell, dateStr));
    calGrid.appendChild(cell);
  }
}

function handleDayClick(e, cell, dateStr) {
  e.stopPropagation();
  closeTips();

  const entry = calendarState.get(dateStr);

  if (!entry) {
    calendarState.set(dateStr, { type: "manual", label: "Booked" });
    buildCalendar();
    syncCalendarToBackend();
    return;
  }

  if (entry.type === "manual") {
    calendarState.delete(dateStr);
    buildCalendar();
    syncCalendarToBackend();
    return;
  }

  // System booking — select the linked account and flash a tooltip
  if (entry.type === "system" && entry.accountId) {
    selectScenario(entry.accountId);
    document.getElementById("scenarios")
      ?.closest(".scenarios-panel")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  const tip = document.createElement("div");
  tip.className   = "day-tip";
  tip.textContent = entry.label;
  cell.appendChild(tip);
  setTimeout(() => document.addEventListener("click", closeTips, { once: true }), 10);
}

function closeTips() {
  document.querySelectorAll(".day-tip").forEach(t => t.remove());
}

async function syncCalendarToBackend() {
  const dates = [...calendarState.entries()]
    .filter(([, v]) => v.type === "manual")
    .map(([k]) => k);
  try {
    await fetch("/db/busy-dates", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ dates }),
    });
  } catch (_) {}
}

async function loadAppointments() {
  try {
    const resp = await fetch("/appointments");
    if (!resp.ok) return;
    const appts = await resp.json();
    // Remove any existing system entries before re-populating
    for (const [k, v] of calendarState) {
      if (v.type === "system") calendarState.delete(k);
    }
    appts.forEach(a => {
      if (!a.appointment_date) return;
      calendarState.set(a.appointment_date, {
        type:      "system",
        label:     a.client_name,
        accountId: a.account_id,
      });
    });
    if (calPanel.classList.contains("open")) buildCalendar();
  } catch (_) {}
}

calBtn.addEventListener("click", e => {
  e.stopPropagation();
  const open = calPanel.classList.toggle("open");
  calBtn.classList.toggle("open", open);
  if (open) {
    buildCalendar();
    loadAppointments();
  }
});

function updateCalendarFromTrace(trace) {
  const execStep = trace.find(s => s.phase === "execution");
  if (!execStep) return;

  const results    = execStep.data?.results ?? [];
  const scenario   = SCENARIOS.find(s => s.id === state.activeClientId);
  const clientName = scenario?.clientName ?? "Client";
  const accountId  = state.activeClientId;
  let   changed    = false;

  results.forEach(r => {
    if (r.status !== "success") return;
    const res = r.result ?? {};

    if (r.tool === "book_appointment" && res.appointment_date) {
      calendarState.set(res.appointment_date, { type: "system", label: clientName, accountId });
      changed = true;
    }
    if (r.tool === "reschedule_appointment") {
      if (res.old_date) { calendarState.delete(res.old_date); changed = true; }
      if (res.new_date) {
        calendarState.set(res.new_date, { type: "system", label: clientName, accountId });
        changed = true;
      }
    }
    if (r.tool === "cancel_appointment" && res.appointment_date) {
      calendarState.delete(res.appointment_date);
      changed = true;
    }
  });

  if (changed && calPanel.classList.contains("open")) buildCalendar();
}


// ── API ────────────────────────────────────────────────────────────────────────

async function postRun(message, confirmed) {
  const body = {
    message,
    session_id: state.sessionId,
    domain:     "appointments",
  };
  if (state.activeClientId)    body.account_id = state.activeClientId;
  if (confirmed !== undefined) body.confirmed   = confirmed;

  const resp = await fetch("/run", {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(body),
  });

  if (!resp.ok) {
    const text = await resp.text().catch(() => `HTTP ${resp.status}`);
    throw new Error(text);
  }
  return resp.json();
}


// ── Chat UI ────────────────────────────────────────────────────────────────────

function appendUserMessage(text) {
  const el = document.createElement("div");
  el.className = "msg msg-user";
  el.innerHTML = `<div class="msg-bubble">${esc(text)}</div>`;
  chatThread.appendChild(el);
  scrollToBottom();
}

function appendLoadingBubble() {
  const el = document.createElement("div");
  el.className = "msg msg-assistant";
  el.innerHTML = `
    <div class="msg-bubble">
      <div class="typing-dots"><span></span><span></span><span></span></div>
    </div>
  `;
  chatThread.appendChild(el);
  state.loadingEl = el;
  scrollToBottom();
}

function replaceLoadingWithResponse(data) {
  const el = state.loadingEl;
  state.loadingEl = null;
  if (!el) return;

  const status = haltStatus(data);
  el.className  = `msg msg-assistant ${status}`;

  let actionsHtml = "";
  if (status === "pending") {
    actionsHtml = `
      <div class="confirm-actions">
        <button class="confirm-btn yes" onclick="sendConfirm(true)">YES</button>
        <button class="confirm-btn no"  onclick="sendConfirm(false)">NO</button>
      </div>
    `;
  }

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  const badgeText = STATUS_BADGE[status];
  const badgeHtml = badgeText ? `<span class="result-badge ${status}">${esc(badgeText)}</span>` : "";
  bubble.innerHTML =
    badgeHtml +
    (status === "pending" ? "" : `<div class="msg-text">${esc(data.result ?? "")}</div>`) +
    actionsHtml;

  el.innerHTML = "";
  el.appendChild(bubble);

  const trace = data.trace ?? [];
  if (trace.length) {
    const auditEl = document.createElement("div");
    auditEl.className = "msg-audit";
    renderAuditTrailInto(auditEl, trace);
    el.appendChild(auditEl);
  }

  scrollToBottom();
}

function appendWelcome() {
  const el = document.createElement("div");
  el.className = "msg msg-assistant";
  el.innerHTML = `<div class="msg-bubble msg-welcome">Choose a scenario above, then type what the client would say — or click an example.</div>`;
  chatThread.appendChild(el);
}

function scrollToBottom() {
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}


// ── Run flow ───────────────────────────────────────────────────────────────────

async function handleRun() {
  const message = messageInput.value.trim();
  if (!message || state.isLoading) return;

  state.lastMessage = message;
  messageInput.value = "";

  appendUserMessage(message);
  appendLoadingBubble();
  setLoading(true);

  try {
    const data = await postRun(message);
    replaceLoadingWithResponse(data);
    if (data.halt_reason === "success") updateCalendarFromTrace(data.trace ?? []);
  } catch (err) {
    replaceLoadingWithResponse({ result: "Sorry, I couldn't reach the server. Please try again in a moment.", halt_reason: "error", trace: [] });
  } finally {
    setLoading(false);
  }
}

// Called from inline onclick in confirmation bubble
async function sendConfirm(confirmed) {
  if (!state.lastMessage || state.isLoading) return;

  // Remove confirm buttons immediately so they can't be double-clicked
  document.querySelectorAll(".confirm-actions").forEach(el => el.remove());

  appendUserMessage(confirmed ? "YES" : "NO");
  appendLoadingBubble();
  setLoading(true);

  try {
    const data = await postRun(state.lastMessage, confirmed);
    replaceLoadingWithResponse(data);
    if (data.halt_reason === "success") updateCalendarFromTrace(data.trace ?? []);
  } catch (err) {
    replaceLoadingWithResponse({ result: "Sorry, I couldn't reach the server. Please try again in a moment.", halt_reason: "error", trace: [] });
  } finally {
    setLoading(false);
  }
}

async function resetDemo() {
  try {
    await fetch("/db/reset", { method: "POST" });
    await fetch("/db/busy-dates", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ dates: [] }),
    });
  } catch (_) {}

  state.sessionId      = crypto.randomUUID();
  state.activeClientId = null;
  state.isLoading      = false;
  state.lastMessage    = null;
  state.loadingEl      = null;

  calendarState.clear();
  await loadAppointments();  // reload system bookings from fresh fixture state

  document.querySelectorAll(".scenario-card").forEach(c => c.classList.remove("active"));
  examplesEl.innerHTML = "";
  messageInput.value   = "";
  chatThread.innerHTML = "";
  appendWelcome();
}


// ── UI helpers ─────────────────────────────────────────────────────────────────

function setLoading(val) {
  state.isLoading = val;
  runBtn.disabled = val;
  spinner.classList.toggle("active", val);
}

const _SCHEDULING_CODES = new Set([
  "DATE_NOT_AVAILABLE","CLOSED_DAY","DATE_IN_PAST","INVALID_DATE",
  "SLOT_NOT_AVAILABLE","INSUFFICIENT_NOTICE","APPOINTMENT_NOT_FOUND",
  "ALREADY_CANCELLED_APPT","DUPLICATE_BOOKING","NO_ACTIVE_APPOINTMENT",
  "MAX_ADVANCE_BOOKING_EXCEEDED","PENDING_RESCHEDULE_ACTIVE",
]);

function _wasBooked(data) {
  const exec = (data.trace ?? []).find(s => s.phase === "execution");
  return (exec?.data?.results ?? []).some(
    r => r.tool === "book_appointment" && r.status === "success"
  );
}

function haltStatus(data) {
  const h = data.halt_reason;
  if (h === "confirmation_pending")           return "pending";
  if (h === "tool_failure" || h === "error") {
    if (_SCHEDULING_CODES.has(data.error?.code)) return "neutral";
    return "error";
  }
  if (h === "max_iterations")                return "error";
  if (data.error && h !== "success")         return "error";
  if (h === "success" && _wasBooked(data))   return "success";
  return "neutral";
}

const STATUS_BADGE = { success: "Done", error: "Error", pending: "Confirm action" };


// ── Audit trail (per message) ─────────────────────────────────────────────────

function renderAuditTrailInto(container, trace) {
  if (!trace.length) return;

  const headEl = document.createElement("div");
  headEl.className = "audit-header";
  headEl.innerHTML = `<span class="audit-title">Audit Trail</span><span class="audit-toggle">▾</span>`;
  headEl.addEventListener("click", () => toggleAudit(headEl));

  const stepsEl = document.createElement("div");
  stepsEl.className = "audit-steps";

  trace.forEach(step => {
    const phase   = step.phase ?? "unknown";
    const data    = step.data  ?? {};
    const label   = PHASE_LABELS[phase] ?? phase;
    const summary = buildSummary(phase, data);
    const details = buildDetails(phase, data);

    const stepEl = document.createElement("div");
    stepEl.className = "audit-step";

    const headerEl = document.createElement("div");
    headerEl.className = "audit-step-header";
    headerEl.innerHTML = `
      <div class="step-dot ${esc(phase)}"></div>
      <span class="step-label">${esc(label)}</span>
      ${summary ? `<span class="step-summary">${esc(summary)}</span>` : ""}
    `;

    const bodyEl = document.createElement("div");
    bodyEl.className = "audit-step-body";
    bodyEl.innerHTML = details;

    headerEl.addEventListener("click", () => bodyEl.classList.toggle("open"));

    stepEl.appendChild(headerEl);
    stepEl.appendChild(bodyEl);
    stepsEl.appendChild(stepEl);
  });

  container.appendChild(headEl);
  container.appendChild(stepsEl);
}

// Called from inline onclick on audit header — must be globally accessible
function toggleAudit(header) {
  const stepsEl = header.nextElementSibling;
  const toggle  = header.querySelector(".audit-toggle");
  const open    = toggle.classList.toggle("open");
  stepsEl.querySelectorAll(".audit-step-body").forEach(b => b.classList.toggle("open", open));
}


// ── Audit detail builders ─────────────────────────────────────────────────────

function buildSummary(phase, d) {
  if (phase === "interpretation") return d.classification ?? "";
  if (phase === "selection") {
    if (d.intent === "ambiguous") return "Ambiguous — clarifying";
    return d.selected_tools?.join(", ") ?? "";
  }
  if (phase === "form")      return d.tool_name ? `Needs info for ${d.tool_name}` : "";
  if (phase === "execution") return (d.tools_attempted ?? []).join(", ") || d.overall_halt_reason || "";
  if (phase === "log")       return d.log_id ? `Entry #${d.log_id}` : "Saved";
  if (phase === "error")     return String(d.exception ?? "").slice(0, 60);
  return "";
}

function buildDetails(phase, d) {
  const rows = [];

  if (phase === "interpretation") {
    if (d.classification)         rows.push(["Intent",        d.classification]);
    if (d.account_id)             rows.push(["Client",        d.account_id]);
    if (d.confirmed != null)      rows.push(["Confirmed",     String(d.confirmed)]);
    if (d.history_length != null) rows.push(["History turns", String(d.history_length)]);
  }

  if (phase === "selection") {
    if (d.selected_tools?.length)  rows.push(["Tool(s)",       d.selected_tools.join(", ")]);
    if (d.clarifying_question)     rows.push(["Clarification", d.clarifying_question]);
    if (d.extracted_params_per_tool) {
      const params = Object.entries(d.extracted_params_per_tool)
        .map(([t, p]) => `${t}: [${p.join(", ")}]`).join("; ");
      if (params) rows.push(["Extracted params", params]);
    }
    if (d.iterations != null) rows.push(["Iteration", String(d.iterations)]);
  }

  if (phase === "form") {
    if (d.tool_name)                rows.push(["Tool",          d.tool_name]);
    if (d.missing_fields?.length)   rows.push(["Missing fields", d.missing_fields.join(", ")]);
    if (d.prefilled_fields?.length) rows.push(["Pre-filled",    d.prefilled_fields.join(", ")]);
  }

  if (phase === "execution") {
    (d.results ?? []).forEach(r => {
      const tool = r.tool ?? "unknown";
      if (r.status === "confirmation_pending") {
        rows.push([tool, "Awaiting confirmation"]);
        if (r.result?.confirmation_payload?.message)
          rows.push(["Confirmation message", r.result.confirmation_payload.message]);
      } else if (r.status === "success") {
        const changes = r.verified_changes ?? [];
        changes.forEach(c => rows.push([tool, c]));
        if (!changes.length) rows.push([tool, "Completed"]);
      } else if (r.status === "tool_failure") {
        const msg = r.error?.message || r.error?.code || JSON.stringify(r.error ?? "");
        rows.push([tool, `Error: ${msg}`]);
      } else if (r.status === "blocked") {
        rows.push([tool, `Blocked — conflicts with ${r.blocker}`]);
      }
    });
    if (d.overall_halt_reason && !(d.results ?? []).length)
      rows.push(["Status", d.overall_halt_reason]);
  }

  if (phase === "log") {
    if (d.log_id != null)              rows.push(["Log entry",  `#${d.log_id}`]);
    if (d.halt_reason)                 rows.push(["Halt reason", d.halt_reason]);
    (d.verified_changes ?? []).forEach(c => rows.push(["Change", c]));
    if (d.confirmation_value != null)  rows.push(["Confirmed", String(d.confirmation_value)]);
  }

  if (phase === "error") {
    if (d.exception) rows.push(["Exception", d.exception]);
  }

  const gridHtml = rows.length
    ? `<div class="data-grid">${rows.map(([k, v]) =>
        `<span class="data-key">${esc(k)}</span><span class="data-val">${esc(String(v))}</span>`
      ).join("")}</div>`
    : `<span class="data-val" style="color:var(--text-light)">No additional detail.</span>`;

  const rawId = `raw-${Math.random().toString(36).slice(2)}`;
  return `
    ${gridHtml}
    <div class="raw-row">
      <button class="raw-toggle" onclick="toggleRaw('${rawId}')">Show raw JSON</button>
      <button class="raw-copy" id="${rawId}-copy" onclick="copyRaw('${rawId}')" title="Copy JSON"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
    </div>
    <pre class="raw-json" id="${rawId}">${esc(JSON.stringify(d, null, 2))}</pre>
  `;
}

function toggleRaw(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle("open");
  const toggle = el.previousElementSibling?.querySelector(".raw-toggle");
  if (toggle) toggle.textContent = el.classList.contains("open") ? "Hide raw JSON" : "Show raw JSON";
}

const _ICON_COPY  = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
const _ICON_CHECK = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;

async function copyRaw(id) {
  const el  = document.getElementById(id);
  const btn = document.getElementById(id + "-copy");
  if (!el || !btn) return;
  try {
    await navigator.clipboard.writeText(el.textContent);
    btn.innerHTML = _ICON_CHECK;
    setTimeout(() => { btn.innerHTML = _ICON_COPY; }, 1500);
  } catch (_) {}
}


// ── Escape helper ──────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}


// ── Event wiring ───────────────────────────────────────────────────────────────
runBtn.addEventListener("click", handleRun);
messageInput.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") handleRun();
});
resetBtn.addEventListener("click", resetDemo);


// ── Init ───────────────────────────────────────────────────────────────────────
renderScenarios();
appendWelcome();
loadAppointments();
