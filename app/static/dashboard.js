const STORAGE_KEYS = {
  activeTab: "scheduling-assistant.active-tab",
  currentRequestId: "scheduling-assistant.current-request-id",
  integrationDrafts: "scheduling-assistant.integration-drafts",
  integrationResults: "scheduling-assistant.integration-results",
  requestDraft: "scheduling-assistant.request-draft",
  ingestDraft: "scheduling-assistant.ingest-draft",
};

const WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const elements = {
  modeBanner: document.getElementById("modeBanner"),
  scenarioHint: document.getElementById("scenarioHint"),
  integrationModeNote: document.getElementById("integrationModeNote"),
  statusBanner: document.getElementById("statusBanner"),
  summaryCards: document.getElementById("summaryCards"),
  integrationHealth: document.getElementById("integrationHealth"),
  readinessPills: document.getElementById("readinessPills"),
  automationSummary: document.getElementById("automationSummary"),
  systemLogs: document.getElementById("systemLogs"),
  showLogs: document.getElementById("showLogs"),
  workspaceForm: document.getElementById("workspaceForm"),
  availabilityForm: document.getElementById("availabilityForm"),
  integrations: document.getElementById("integrations"),
  availability: document.getElementById("availability"),
  calendars: document.getElementById("calendars"),
  busySlots: document.getElementById("busySlots"),
  locations: document.getElementById("locations"),
  requests: document.getElementById("requests"),
  meetings: document.getElementById("meetings"),
  requestDetailActions: document.getElementById("requestDetailActions"),
  requestDetail: document.getElementById("requestDetail"),
  requestMessages: document.getElementById("requestMessages"),
  requestForm: document.getElementById("requestForm"),
  ingestForm: document.getElementById("ingestForm"),
  loadDemo: document.getElementById("loadDemo"),
  runOneClickPipeline: document.getElementById("runOneClickPipeline"),
  runLiveGmailCcFlow: document.getElementById("runLiveGmailCcFlow"),
  runAutomation: document.getElementById("runAutomation"),
  refresh: document.getElementById("refresh"),
  tabButtons: Array.from(document.querySelectorAll(".tab-button")),
  tabPages: Array.from(document.querySelectorAll(".tab-page")),
};

const state = {
  snapshot: null,
  activeTab: loadStoredText(STORAGE_KEYS.activeTab, "setup"),
  currentRequestId: loadStoredText(STORAGE_KEYS.currentRequestId, ""),
  integrationDrafts: loadStoredJson(STORAGE_KEYS.integrationDrafts, {}),
  integrationResults: loadStoredJson(STORAGE_KEYS.integrationResults, {}),
  statusTimer: null,
  refreshInFlight: null,
  refreshIntervalId: null,
  systemLogsLoaded: false,
  systemLogsLoading: false,
};

const REQUEST_DETAIL_PLACEHOLDER = "Select a request above to see its full record.";

function loadStoredText(key, fallback) {
  try {
    return window.sessionStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function loadStoredJson(key, fallback) {
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function saveStoredText(key, value) {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Ignore storage restrictions.
  }
}

function saveStoredJson(key, value) {
  try {
    window.sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Ignore storage restrictions.
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function asList(value) {
  return String(value ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function weekdayLabel(weekday) {
  return WEEKDAY_LABELS[Number(weekday)] || `Day ${weekday}`;
}

function setStatus(message, isWarning = false) {
  if (!elements.statusBanner) {
    return;
  }
  if (state.statusTimer) {
    window.clearTimeout(state.statusTimer);
  }
  elements.statusBanner.hidden = !message;
  elements.statusBanner.textContent = message;
  elements.statusBanner.classList.toggle("warn", isWarning);
  if (!message) {
    return;
  }
  state.statusTimer = window.setTimeout(() => {
    if (elements.statusBanner.textContent === message) {
      elements.statusBanner.hidden = true;
      elements.statusBanner.textContent = "";
      elements.statusBanner.classList.remove("warn");
    }
  }, 5000);
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail || response.statusText);
  }
  return response.json();
}

function switchTab(tabName) {
  state.activeTab = tabName;
  saveStoredText(STORAGE_KEYS.activeTab, tabName);
  elements.tabButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabName);
  });
  elements.tabPages.forEach((page) => {
    page.classList.toggle("active", page.id === `tab-${tabName}`);
  });
}

function googleIcon() {
  return `
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path fill="#EA4335" d="M12 10.2v3.9h5.4c-.23 1.24-.95 2.28-2.03 2.98l3.28 2.54c1.92-1.76 3.02-4.36 3.02-7.45 0-.69-.06-1.34-.18-1.98H12z"/>
      <path fill="#4285F4" d="M12 22c2.73 0 5.02-.9 6.69-2.44l-3.28-2.54c-.91.61-2.08.98-3.41.98-2.62 0-4.84-1.77-5.63-4.15H3v2.61A10 10 0 0 0 12 22z"/>
      <path fill="#FBBC05" d="M6.37 13.85A5.99 5.99 0 0 1 6.06 12c0-.64.11-1.26.31-1.85V7.54H3A10 10 0 0 0 2 12c0 1.61.39 3.13 1 4.46l3.37-2.61z"/>
      <path fill="#34A853" d="M12 5.98c1.48 0 2.8.5 3.84 1.48l2.88-2.88C17 2.98 14.71 2 12 2A10 10 0 0 0 3 7.54l3.37 2.61C7.16 7.75 9.38 5.98 12 5.98z"/>
    </svg>
  `;
}

function googleCalendarIcon() {
  return `
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <rect x="3" y="4" width="18" height="17" rx="4" fill="#fff" stroke="#DADCE0"/>
      <path d="M7 2.8a.8.8 0 0 1 .8.8V6H6.2V3.6a.8.8 0 0 1 .8-.8zm10 0a.8.8 0 0 1 .8.8V6h-1.6V3.6a.8.8 0 0 1 .8-.8z" fill="#4285F4"/>
      <path d="M3 8h18v4H3z" fill="#34A853"/>
      <path d="M8 13h3v3H8z" fill="#4285F4"/>
      <path d="M13 13h3v3h-3z" fill="#FBBC05"/>
      <path d="M8 17h3v2H8z" fill="#EA4335"/>
      <path d="M13 17h3v2h-3z" fill="#34A853"/>
    </svg>
  `;
}

function setInputValue(form, name, value) {
  const field = form?.querySelector(`[name="${name}"]`);
  if (field) {
    field.value = value ?? "";
  }
}

function serializeForm(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function persistFormDraft(form, key) {
  saveStoredJson(key, serializeForm(form));
}

function restoreFormDraft(form, key) {
  const draft = loadStoredJson(key, null);
  if (!draft) {
    return;
  }
  Object.entries(draft).forEach(([name, value]) => setInputValue(form, name, value));
}

function bindDraftPersistence(form, key) {
  if (!form) {
    return;
  }
  form.addEventListener("input", () => persistFormDraft(form, key));
  form.addEventListener("change", () => persistFormDraft(form, key));
}

function integrationResult(provider) {
  return state.integrationResults[provider] || null;
}

function setIntegrationResult(provider, payload) {
  state.integrationResults[provider] = payload;
  saveStoredJson(STORAGE_KEYS.integrationResults, state.integrationResults);
}

function clearIntegrationDraft(provider) {
  delete state.integrationDrafts[provider];
  saveStoredJson(STORAGE_KEYS.integrationDrafts, state.integrationDrafts);
}

function providerFieldValue(item, field) {
  const draft = state.integrationDrafts[item.provider] || {};
  if (Object.prototype.hasOwnProperty.call(draft, field.key)) {
    return draft[field.key];
  }
  return item.values[field.key];
}

function persistIntegrationDraft(provider, form) {
  state.integrationDrafts[provider] = Object.fromEntries(new FormData(form).entries());
  saveStoredJson(STORAGE_KEYS.integrationDrafts, state.integrationDrafts);
}

function googleCalendarConsoleUrl(item) {
  const project = item.google_project_hint ? `?project=${encodeURIComponent(item.google_project_hint)}` : "";
  return `https://console.cloud.google.com/apis/library/calendar-json.googleapis.com${project}`;
}

function fieldControl(provider, field, value) {
  const fieldId = `${provider}-${field.key}`;
  if (field.multiline) {
    return `
      <label>${escapeHtml(field.label)}${field.required ? " *" : ""}
        <textarea id="${fieldId}" name="${field.key}">${escapeHtml(value || "")}</textarea>
      </label>
    `;
  }
  return `
    <label>${escapeHtml(field.label)}${field.required ? " *" : ""}
      <input id="${fieldId}" name="${field.key}" type="${field.secret ? "password" : "text"}" value="${escapeHtml(value || "")}" />
    </label>
  `;
}

function renderSummary(snapshot) {
  const summary = snapshot.summary;
  const cards = [
    snapshot.demo_mode
      ? ["Execution", "Local only"]
      : ["Connections", `${summary.integrations_configured} / ${summary.integrations_total}`],
    ["Requests", summary.requests_total],
    ["Negotiating", summary.negotiating],
    ["Confirmed", summary.confirmed],
    ["Follow-ups due", summary.follow_ups_due],
    ["Reminders due", summary.confirmations_due],
  ];

  elements.summaryCards.innerHTML = cards
    .map(
      ([label, value]) => `
        <article class="summary-card">
          <span class="summary-card__value">${escapeHtml(value)}</span>
          <span class="summary-card__label">${escapeHtml(label)}</span>
        </article>
      `,
    )
    .join("");
}

function renderMode(snapshot) {
  const demoMode = snapshot.demo_mode !== false;
  elements.modeBanner.textContent = demoMode
    ? "Offline demo · Fictional contacts · Messages stay local · No credentials required"
    : "Live mode · Configured integrations can send email and modify calendars";
  elements.modeBanner.classList.toggle("mode-banner--live", !demoMode);
  elements.runLiveGmailCcFlow.disabled = demoMode;
  elements.runLiveGmailCcFlow.title = demoMode
    ? "Unavailable in offline demo mode"
    : "Runs a live Gmail scenario using your configured accounts";
  elements.integrationModeNote.textContent = demoMode
    ? "Demo mode uses local scheduling heuristics and a local calendar. Credential entry, Google sign-in, inbox sync, and external API calls are disabled."
    : "Live adapters require your own configuration. Google Maps routing and Zoom meeting creation are not implemented.";
  elements.scenarioHint.textContent = demoMode
    ? "Start with Book a sample meeting, then inspect the conversation in Activity. All messages and meetings remain in this local workspace."
    : "Live mode is enabled. Scheduling scenarios can send real email and create calendar events when the corresponding adapters are configured.";
}

function renderWorkspace(snapshot) {
  const workspace = snapshot.workspace;
  Object.entries(workspace).forEach(([name, value]) => setInputValue(elements.workspaceForm, name, value));

  setInputValue(elements.availabilityForm, "start_time", workspace.workday_start || "09:00");
  setInputValue(elements.availabilityForm, "end_time", workspace.workday_end || "18:00");
  const activeWeekdays = new Set(snapshot.availability_rules.map((rule) => Number(rule.weekday)));
  elements.availabilityForm.querySelectorAll('input[name="weekday"]').forEach((input) => {
    input.checked = activeWeekdays.has(Number(input.value));
  });

  setInputValue(elements.ingestForm, "to", workspace.owner_email);
  setInputValue(elements.ingestForm, "cc", workspace.assistant_email);

  elements.availability.innerHTML = snapshot.availability_rules.length
    ? snapshot.availability_rules
        .map(
          (rule) => `
            <div class="list-item">
              <strong>${escapeHtml(weekdayLabel(rule.weekday))}</strong>
              <div class="muted">${escapeHtml(rule.start_time)} - ${escapeHtml(rule.end_time)}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="muted">Availability is not configured yet.</div>';

  elements.calendars.innerHTML = snapshot.calendars.length
    ? snapshot.calendars
        .map(
          (calendar) => `
            <div class="list-item">
              <strong>${escapeHtml(calendar.name)}</strong>
              <div class="muted">${escapeHtml(calendar.provider)}${calendar.is_primary ? " · primary" : ""}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="muted">No calendars added yet.</div>';

  elements.busySlots.innerHTML = snapshot.busy_slots.length
    ? snapshot.busy_slots
        .map(
          (slot) => `
            <div class="list-item">
              <strong>${escapeHtml(slot.title)}</strong>
              <div class="muted">${escapeHtml(slot.label)}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="muted">No busy slots yet.</div>';

  elements.locations.innerHTML = snapshot.locations.length
    ? snapshot.locations
        .map(
          (location) => `
            <div class="list-item">
              <strong>${escapeHtml(location.label)}</strong>
              <div class="muted">${escapeHtml(location.address)}</div>
            </div>
          `,
        )
        .join("")
    : '<div class="muted">No locations added yet.</div>';

  restoreFormDraft(elements.requestForm, STORAGE_KEYS.requestDraft);
  restoreFormDraft(elements.ingestForm, STORAGE_KEYS.ingestDraft);
}

function renderIntegrations(snapshot) {
  const integrations = snapshot.integrations.filter((item) => !["zoom", "google_maps"].includes(item.provider));
  if (snapshot.demo_mode !== false) {
    elements.integrationHealth.innerHTML = `
      <strong>Local simulation is ready.</strong>
      <div class="inline-note">No accounts are connected. Requests, replies, and meeting confirmations are recorded locally.</div>
    `;
    elements.readinessPills.innerHTML = '<span class="pill">Local parser</span><span class="pill">Local calendar</span><span class="pill">Local message records</span>';
    elements.integrations.innerHTML = integrations.map((item) => `
      <article class="integration-card">
        <div class="integration-top">
          <strong>${escapeHtml(item.title)}</strong>
          <span class="badge">Disabled in demo</span>
        </div>
        <div class="muted small">${item.provider === "openai"
          ? "Scheduling requests are parsed with local rules. The optional AI adapter is not called."
          : "Explore this workflow with fictional records. No Google authorization or external synchronization is used."}</div>
      </article>
    `).join("") + '<div class="inline-note">Zoom meeting creation and Google Maps routing are not implemented. In-person travel time uses a local distance estimate.</div>';
    return;
  }
  const missing = integrations.filter((item) => !item.configured);

  elements.integrationHealth.innerHTML = missing.length
    ? `
        <strong>Available to configure:</strong><br />
        ${missing.map((item) => escapeHtml(item.title)).join(", ")}
        <div class="inline-note">Credentials are encrypted locally in live mode. Keep the encryption key outside version control.</div>
      `
    : `
        <strong>All listed adapters are configured.</strong>
        <div class="inline-note">Configured adapters can use live Google authorization and calendar synchronization.</div>
      `;

  elements.readinessPills.innerHTML = integrations
    .map(
      (item) =>
        `<span class="pill">${escapeHtml(item.title)}: ${escapeHtml(item.status_label || (item.configured ? "OK" : "not configured"))}</span>`,
    )
    .join("");

  elements.integrations.innerHTML = integrations
    .map((item) => {
      const result = integrationResult(item.provider);
      return `
        <form class="integration-card" data-provider="${item.provider}">
          <div class="integration-top">
            <div>
              <strong>${escapeHtml(item.title)}</strong>
              <div class="muted small">${escapeHtml(item.description)}</div>
              <div class="inline-note">${(item.services || []).map((service) => escapeHtml(service)).join(" · ")}</div>
            </div>
            <span class="badge ${escapeHtml(item.status_tone || (item.configured ? "ok" : "warn"))}">
              ${escapeHtml(item.status_label || (item.configured ? "configured" : "not configured"))}
            </span>
          </div>

          <div class="form-grid">
            ${item.fields.map((field) => fieldControl(item.provider, field, providerFieldValue(item, field))).join("")}
          </div>

          ${
            item.oauth_login
              ? `
                <div class="callout" style="margin-top:12px;">
                  <strong>Google authorization:</strong><br />
                  ${
                    item.account_connected
                      ? `Connected account <strong>${escapeHtml(item.connected_email || "")}</strong>`
                      : item.app_ready
                        ? "OAuth is configured. Select Sign in with Google to connect."
                        : "Enter the OAuth Client ID, Client Secret, and Redirect URI first."
                  }
                  <div class="inline-note">
                    Supported redirect URIs: <code>http://localhost:8000/auth/google/callback</code> and
                    <code>http://localhost:8000/oauth2/callback</code>.
                  </div>
                  ${
                    item.account_connected
                      ? item.provider === "google_workspace"
                        ? `<div class="inline-note">Google account connected. Next: <strong>Connect Google Calendar</strong>.</div>`
                        : item.provider === "assistant_gmail"
                          ? `<div class="inline-note">Google account connected. Next: <strong>Sync Assistant Gmail</strong>.</div>`
                          : `<div class="inline-note">This account is available for live Gmail scenarios.</div>`
                      : ""
                  }
                  ${
                    item.calendar_connected
                      ? `<div class="inline-note">Connected calendar: <strong>${escapeHtml(item.selected_calendar_summary || "Primary Google Calendar")}</strong>${item.last_calendar_sync_at ? ` · last sync: ${escapeHtml(item.last_calendar_sync_at)}` : ""}</div>`
                      : ""
                  }
                  ${
                    item.warnings && item.warnings.length
                      ? `<div class="inline-note" style="color:#9a3412;">${item.warnings.map((message) => escapeHtml(message)).join("<br />")}</div>`
                      : ""
                  }
                  ${
                    item.has_calendar_api_warning
                      ? `
                        <div class="integration-alert">
                          <strong>Enable the Google Calendar API</strong>
                          <div>The account is connected, but the Calendar API is disabled for this project.</div>
                          <div class="inline-note" style="margin-top:8px;">
                            <a href="${googleCalendarConsoleUrl(item)}" target="_blank" rel="noreferrer">Open Google Cloud Console</a>
                          </div>
                        </div>
                      `
                      : ""
                  }
                  ${
                    item.calendar_summaries && item.calendar_summaries.length
                      ? `<div class="inline-note">Discovered calendars: ${item.calendar_summaries.map((name) => escapeHtml(name)).join(", ")}</div>`
                      : ""
                  }
                </div>
              `
              : ""
          }

          <div class="inline-note">
            ${item.updated_at ? `Last saved: ${escapeHtml(item.updated_at)}` : "Not saved yet"}
          </div>

          <div class="form-actions">
            <button type="submit">Save ${escapeHtml(item.title)}</button>
            ${
              item.oauth_login && item.app_ready && !item.account_connected
                ? `
                  <a href="/auth/google/start?provider=${encodeURIComponent(item.provider)}&redirect_to=/" class="provider-button">
                    <span class="provider-button__icon">${googleIcon()}</span>
                    <span>Sign in with Google</span>
                  </a>
                `
                : ""
            }
            ${
              item.oauth_login && item.account_connected
                ? `
                  <span class="provider-status">
                    <span class="provider-button__icon">${googleIcon()}</span>
                    <span>Google connected</span>
                  </span>
                  <a href="/auth/google/start?provider=${encodeURIComponent(item.provider)}&redirect_to=/" class="provider-link">Change Google account</a>
                `
                : ""
            }
            ${
              item.provider === "google_workspace" && item.account_connected
                ? `
                  <button type="button" class="provider-button google-calendar-button ${result && result.kind === "loading" ? "is-loading" : ""}" data-action="connect-google-calendar">
                    <span class="provider-button__icon">${googleCalendarIcon()}</span>
                    <span>${result && result.kind === "loading" ? "Connecting calendar..." : item.calendar_connected ? "Sync Google Calendar" : "Connect Google Calendar"}</span>
                  </button>
                `
                : ""
            }
            ${
              item.provider === "assistant_gmail" && item.account_connected
                ? `
                  <button type="button" class="provider-button google-calendar-button ${result && result.kind === "loading" ? "is-loading" : ""}" data-action="sync-assistant-gmail">
                    <span class="provider-button__icon">${googleIcon()}</span>
                    <span>${result && result.kind === "loading" ? "Sync Gmail..." : "Sync Assistant Gmail"}</span>
                  </button>
                `
                : ""
            }
            ${
              item.oauth_login && item.account_connected
                ? `<button type="button" class="ghost" data-action="disconnect-google" data-provider="${escapeHtml(item.provider)}">Disconnect Google</button>`
                : ""
            }
          </div>

          ${
            result
              ? `<div class="integration-result ${escapeHtml(result.kind || "")}">${escapeHtml(result.message || "")}</div>`
              : ""
          }
        </form>
      `;
    })
    .join("");
}

function renderAutomationSummary(snapshot) {
  if (snapshot.demo_mode !== false) {
    elements.automationSummary.innerHTML = `
      <strong>Local follow-up queue</strong><br />
      Follow-ups due: ${Number(snapshot.summary.follow_ups_due)}<br />
      Reminders due: ${Number(snapshot.summary.confirmations_due)}<br />
      <span class="muted">Run follow-ups to process the queue manually. Message records stay local; background inbox polling is disabled.</span>
    `;
    return;
  }
  const runtime = snapshot.automation.runtime || {};
  const lastResult = runtime.last_result || {};
  const runtimeState = runtime.enabled
    ? runtime.active_run
      ? "running"
      : runtime.task_running
        ? "enabled"
        : "waiting for a server restart"
    : "disabled";
  const lastRun = runtime.last_run_at || "not yet";
  const lastSuccess = runtime.last_success_at || "not yet";
  const imported = lastResult.imported_inbox_messages ?? 0;
  const followUps = lastResult.follow_ups_sent ?? 0;
  const confirmations = lastResult.confirmations_sent ?? 0;
  const repaired = lastResult.repaired_events ?? 0;
  const fallbackHint = runtime.enabled
    ? "New Gmail messages are checked in the background. Use the manual action to run a cycle now."
    : "Background processing is disabled. You can run a cycle manually.";
  elements.automationSummary.innerHTML = `
    <strong>Automation queue</strong><br />
    Follow-ups due: ${snapshot.summary.follow_ups_due}<br />
    Reminders due: ${snapshot.summary.confirmations_due}<br /><br />
    <strong>Background inbox polling:</strong> ${escapeHtml(runtimeState)}<br />
    <strong>Polling interval:</strong> ${Number(runtime.interval_seconds || 0)} seconds<br />
    <strong>Last run:</strong> ${escapeHtml(lastRun)}<br />
    <strong>Last successful cycle:</strong> ${escapeHtml(lastSuccess)}<br />
    <strong>Last result:</strong> imported ${imported}, follow-up ${followUps}, reminders ${confirmations}, repaired ${repaired}<br />
    ${runtime.last_error ? `<strong>Last error:</strong> ${escapeHtml(runtime.last_error)}<br />` : ""}
    <span class="muted">${escapeHtml(fallbackHint)}</span>
  `;
}

async function refreshSystemLogs() {
  if (!elements.systemLogs || state.systemLogsLoading) {
    return false;
  }
  state.systemLogsLoading = true;
  if (elements.showLogs) {
    elements.showLogs.disabled = true;
    elements.showLogs.textContent = state.systemLogsLoaded ? "Refreshing logs..." : "Loading logs...";
  }
  elements.systemLogs.innerHTML = '<div class="muted">Loading system logs...</div>';
  try {
    const errors = await fetchJson("/api/system/logs/recent?kind=error&limit=20");
    const appLogs = await fetchJson("/api/system/logs/recent?kind=app&limit=5");
    const errorLines = (errors.lines || []).slice(-6);
    elements.systemLogs.innerHTML = `
      <p><strong>Application logs</strong>: ${Number((appLogs.lines || []).length)} recent entries loaded.</p>
      <p>The most recent application errors appear below.</p>
      ${
        errorLines.length
          ? `<pre class="log-preview">${escapeHtml(errorLines.join("\n"))}</pre>`
          : '<div class="muted">No errors have been logged.</div>'
      }
    `;
    state.systemLogsLoaded = true;
    return true;
  } catch (error) {
    elements.systemLogs.innerHTML = `<div class="muted">Unable to load system logs: ${escapeHtml(error.message || error)}</div>`;
    return false;
  } finally {
    state.systemLogsLoading = false;
    if (elements.showLogs) {
      elements.showLogs.disabled = false;
      elements.showLogs.textContent = state.systemLogsLoaded ? "Refresh logs" : "Show logs";
    }
  }
}

function renderRequests(snapshot) {
  const rows = snapshot.requests
    .map(
      (request) => `
        <tr>
          <td>
            <a class="row-link" data-request-id="${request.id}">${escapeHtml(request.subject)}</a>
            <div class="muted small">${escapeHtml(request.thread_key)}</div>
          </td>
          <td>${escapeHtml(request.status)}</td>
          <td>${escapeHtml(request.priority)}</td>
          <td>${request.participant_count}</td>
          <td>${escapeHtml(request.scheduled_display || "Scheduling in progress")}</td>
          <td>
            <div class="table-actions">
              <button type="button" class="table-action danger" data-delete-request-id="${request.id}" data-delete-subject="${escapeHtml(request.subject)}">Delete</button>
            </div>
          </td>
        </tr>
      `,
    )
    .join("");

  elements.requests.innerHTML = rows
    ? `
        <table>
          <thead>
            <tr>
              <th>Thread</th>
              <th>Status</th>
              <th>Priority</th>
              <th>Participants</th>
              <th>Scheduled for</th>
              <th></th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `
    : '<div class="muted">No requests yet. Try booking a sample meeting.</div>';

  document.querySelectorAll("[data-request-id]").forEach((node) => {
    node.addEventListener("click", () => showRequest(node.dataset.requestId));
  });
  document.querySelectorAll("[data-delete-request-id]").forEach((node) => {
    node.addEventListener("click", async (event) => {
      event.stopPropagation();
      await deleteRequest(node.dataset.deleteRequestId, node.dataset.deleteSubject || "");
    });
  });
}

function renderMeetings(snapshot) {
  const rows = snapshot.meetings
    .map(
      (meeting) => `
        <tr>
          <td>${escapeHtml(meeting.title)}</td>
          <td>${escapeHtml(meeting.label)}</td>
          <td>${escapeHtml(meeting.confirmation_status)}</td>
          <td>
            <div class="table-actions">
              <button type="button" class="table-action danger" data-delete-request-id="${meeting.request_id}" data-delete-subject="${escapeHtml(meeting.title)}">Delete</button>
            </div>
          </td>
        </tr>
      `,
    )
    .join("");

  elements.meetings.innerHTML = rows
    ? `
        <table>
          <thead>
            <tr>
              <th>Meeting</th>
              <th>Details</th>
              <th>Reminder status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `
    : '<div class="muted">No confirmed meetings yet.</div>';

  document.querySelectorAll("#meetings [data-delete-request-id]").forEach((node) => {
    node.addEventListener("click", async () => {
      await deleteRequest(node.dataset.deleteRequestId, node.dataset.deleteSubject || "");
    });
  });
}

async function refreshDashboard() {
  if (state.refreshInFlight) {
    return state.refreshInFlight;
  }
  state.refreshInFlight = (async () => {
    const snapshot = await fetchJson("/api/dashboard");
    state.snapshot = snapshot;
    renderMode(snapshot);
    renderSummary(snapshot);
    renderWorkspace(snapshot);
    renderIntegrations(snapshot);
    renderAutomationSummary(snapshot);
    renderRequests(snapshot);
    renderMeetings(snapshot);
    if (state.currentRequestId) {
      await showRequest(state.currentRequestId, false);
    }
  })();
  try {
    await state.refreshInFlight;
  } finally {
    state.refreshInFlight = null;
  }
}

async function showRequest(requestId, switchToActivity = true) {
  const detail = await fetchJson(`/api/requests/${requestId}`);
  state.currentRequestId = String(requestId);
  saveStoredText(STORAGE_KEYS.currentRequestId, state.currentRequestId);
  elements.requestDetail.textContent = JSON.stringify(detail, null, 2);
  renderMessages(detail);
  renderRequestDetailActions(detail);
  if (switchToActivity) {
    switchTab("activity");
  }
}

function renderMessages(detail) {
  const emails = detail?.emails || [];
  elements.requestMessages.hidden = emails.length === 0;
  const deliveryLabels = { local: "Local only", sent: "Sent", failed: "Failed", pending: "Pending" };
  elements.requestMessages.innerHTML = emails.map((email) => {
    const delivery = state.snapshot?.demo_mode !== false
      ? "local"
      : email.delivery_status || (email.provider === "local" ? "local" : "pending");
    return `
      <article class="list-item">
        <div class="message-heading">
          <strong>${escapeHtml(email.subject || "Message")}</strong>
          <span class="badge ${delivery === "failed" ? "warn" : ""}">${escapeHtml(deliveryLabels[delivery] || delivery)}</span>
        </div>
        <div class="muted small">${escapeHtml(email.sender_email || "")} · ${escapeHtml(email.direction || "")}</div>
        <p class="message-body">${escapeHtml(email.body || "")}</p>
      </article>
    `;
  }).join("");
}

function renderRequestDetailActions(detail) {
  if (!elements.requestDetailActions) {
    return;
  }
  if (!detail || !detail.id) {
    elements.requestDetailActions.hidden = true;
    elements.requestDetailActions.innerHTML = "";
    return;
  }
  elements.requestDetailActions.hidden = false;
  elements.requestDetailActions.innerHTML = `
    <button type="button" class="table-action danger" data-delete-request-id="${detail.id}" data-delete-subject="${escapeHtml(detail.subject || "")}">
      Delete request
    </button>
  `;
  const button = elements.requestDetailActions.querySelector("[data-delete-request-id]");
  if (button) {
    button.addEventListener("click", async () => {
      await deleteRequest(button.dataset.deleteRequestId, button.dataset.deleteSubject || "");
    });
  }
}

async function deleteRequest(requestId, subjectHint = "") {
  const label = subjectHint || `request #${requestId}`;
  const confirmed = window.confirm(`Delete "${label}" from this workspace?`);
  if (!confirmed) {
    return;
  }
  try {
    const result = await fetchJson(`/api/requests/${requestId}`, { method: "DELETE" });
    if (state.currentRequestId === String(requestId)) {
      state.currentRequestId = "";
      saveStoredText(STORAGE_KEYS.currentRequestId, "");
      elements.requestDetail.textContent = REQUEST_DETAIL_PLACEHOLDER;
      renderMessages(null);
      renderRequestDetailActions(null);
    }
    await refreshDashboard();
    const calendarHint = result.had_calendar_event
      ? result.calendar_event_removed
        ? " The Google Calendar event was also removed."
        : " The local record was deleted, but the Google Calendar event could not be removed."
      : "";
    setStatus(`"${result.deleted_subject || label}" deleted.${calendarHint}`, !result.calendar_event_removed && result.had_calendar_event);
  } catch (error) {
    setStatus(error.message, true);
  }
}

function bindTabs() {
  elements.tabButtons.forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
}

function bindWorkspaceForm() {
  elements.workspaceForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = serializeForm(event.currentTarget);
    ["default_duration_minutes", "meeting_buffer_minutes", "min_notice_hours", "confirmation_lead_hours"].forEach((key) => {
      payload[key] = Number(payload[key] || 0);
    });
    ["base_lat", "base_lng"].forEach((key) => {
      payload[key] = payload[key] ? Number(payload[key]) : null;
    });
    try {
      await fetchJson("/api/workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setStatus("Assistant profile saved.");
      await refreshDashboard();
    } catch (error) {
      setStatus(error.message, true);
    }
  });
}

function bindAvailabilityForm() {
  elements.availabilityForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.snapshot?.workspace) {
      setStatus("Wait for the dashboard to load first.", true);
      return;
    }
    const form = event.currentTarget;
    const startTime = String(form.querySelector('[name="start_time"]').value || "").trim();
    const endTime = String(form.querySelector('[name="end_time"]').value || "").trim();
    const weekdays = Array.from(form.querySelectorAll('input[name="weekday"]:checked'))
      .map((input) => Number(input.value))
      .sort((left, right) => left - right);

    if (!startTime || !endTime) {
      setStatus("Enter the start and end of the availability window.", true);
      return;
    }
    if (!weekdays.length) {
      setStatus("Select at least one working day.", true);
      return;
    }

    const workspacePayload = {
      ...state.snapshot.workspace,
      workday_start: startTime,
      workday_end: endTime,
    };
    const availabilityPayload = weekdays.map((weekday) => ({
      weekday,
      start_time: startTime,
      end_time: endTime,
      priority: "normal",
    }));

    try {
      await fetchJson("/api/workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(workspacePayload),
      });
      await fetchJson("/api/availability", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(availabilityPayload),
      });
      setStatus("Working days and availability saved.");
      await refreshDashboard();
    } catch (error) {
      setStatus(error.message, true);
    }
  });
}

function bindIntegrationHandlers() {
  elements.integrations.addEventListener("input", (event) => {
    if (state.snapshot?.demo_mode !== false) return;
    const form = event.target.closest("form[data-provider]");
    if (!form) {
      return;
    }
    persistIntegrationDraft(form.dataset.provider, form);
  });

  elements.integrations.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.snapshot?.demo_mode !== false) return;
    const form = event.target.closest("form[data-provider]");
    if (!form) {
      return;
    }
    const provider = form.dataset.provider;
    const payload = { values: Object.fromEntries(new FormData(form).entries()) };
    try {
      await fetchJson(`/api/integrations/${provider}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      clearIntegrationDraft(provider);
      setStatus(`Credentials for ${provider} saved.`);
      await refreshDashboard();
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  elements.integrations.addEventListener("click", async (event) => {
    if (state.snapshot?.demo_mode !== false) return;
    const connectCalendarButton = event.target.closest("[data-action='connect-google-calendar']");
    if (connectCalendarButton) {
      setIntegrationResult("google_workspace", {
        kind: "loading",
        message: "Connecting and synchronizing Google Calendar...",
      });
      renderIntegrations(state.snapshot);
      try {
        const snapshot = await fetchJson("/api/integrations/google_workspace/connect-calendar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        const sync = snapshot.calendar_sync || {};
        const warning = (sync.warnings && sync.warnings[0]) || (snapshot.warnings && snapshot.warnings[0]);
        if (sync.ok) {
          const imported = Number(sync.imported_busy_slots || 0);
          const repaired = Number(sync.repaired_events || 0);
          let message = imported
            ? `Google Calendar synchronized. Busy slots imported: ${imported}.`
            : "Google Calendar synchronized. No new busy slots found.";
          if (repaired) {
            message += ` Calendar events restored: ${repaired}.`;
          }
          setIntegrationResult("google_workspace", { kind: "ok", message });
          setStatus(message);
        } else {
          const message = warning || "Google Calendar could not be synchronized. Check the API settings in Google Cloud.";
          setIntegrationResult("google_workspace", { kind: "warn", message });
          setStatus(message, true);
        }
        await refreshDashboard();
      } catch (error) {
        setIntegrationResult("google_workspace", { kind: "warn", message: error.message });
        setStatus(error.message, true);
        renderIntegrations(state.snapshot);
      }
      return;
    }

    const syncAssistantButton = event.target.closest("[data-action='sync-assistant-gmail']");
    if (syncAssistantButton) {
      setIntegrationResult("assistant_gmail", {
        kind: "loading",
        message: "Synchronizing the assistant inbox...",
      });
      renderIntegrations(state.snapshot);
      try {
        const snapshot = await fetchJson("/api/integrations/assistant_gmail/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        const sync = snapshot.gmail_sync || {};
        const imported = Number(sync.imported_messages || 0);
        const skipped = Number(sync.skipped_messages || 0);
        const message = `Assistant Gmail synchronized. Imported messages: ${imported}, skipped: ${skipped}.`;
        setIntegrationResult("assistant_gmail", { kind: "ok", message });
        setStatus(message);
        await refreshDashboard();
      } catch (error) {
        setIntegrationResult("assistant_gmail", { kind: "warn", message: error.message });
        setStatus(error.message, true);
        renderIntegrations(state.snapshot);
      }
      return;
    }

    const disconnectButton = event.target.closest("[data-action='disconnect-google']");
    if (!disconnectButton) {
      return;
    }
    const provider = disconnectButton.dataset.provider || "google_workspace";
    try {
      await fetchJson(`/api/integrations/${provider}/disconnect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      setIntegrationResult(provider, {
        kind: "warn",
        message: "Google integration disconnected.",
      });
      setStatus(`Google integration ${provider} disconnected.`);
      await refreshDashboard();
    } catch (error) {
      setStatus(error.message, true);
    }
  });
}

function bindActionButtons() {
  elements.loadDemo.addEventListener("click", async () => {
    try {
      await fetchJson("/api/demo/reset", { method: "POST" });
      state.currentRequestId = "";
      saveStoredText(STORAGE_KEYS.currentRequestId, "");
      elements.requestDetail.textContent = REQUEST_DETAIL_PLACEHOLDER;
      renderMessages(null);
      renderRequestDetailActions(null);
      setStatus("Sample data reset.");
      await refreshDashboard();
      switchTab("test");
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  elements.runOneClickPipeline.addEventListener("click", async () => {
    try {
      const result = await fetchJson("/api/demo/one-click-pipeline", { method: "POST" });
      const detail = result.detail;
      state.currentRequestId = String(detail.id);
      saveStoredText(STORAGE_KEYS.currentRequestId, state.currentRequestId);
      elements.requestDetail.textContent = JSON.stringify(detail, null, 2);
      const message = state.snapshot?.demo_mode !== false
        ? `Demo complete. Meeting recorded for ${result.scheduled_display}. No email was sent or external event created.`
        : result.calendar_event_created
        ? `Scenario complete. Meeting confirmed for ${result.scheduled_display} and added to Google Calendar.`
        : `Scenario complete. Meeting confirmed for ${result.scheduled_display}.`;
      setStatus(message);
      await refreshDashboard();
      switchTab("activity");
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  elements.runLiveGmailCcFlow.addEventListener("click", async () => {
    if (state.snapshot?.demo_mode !== false) return;
    try {
      const result = await fetchJson("/api/demo/live-gmail-cc-flow", { method: "POST" });
      const detail = result.detail;
      state.currentRequestId = String(detail.id);
      saveStoredText(STORAGE_KEYS.currentRequestId, state.currentRequestId);
      elements.requestDetail.textContent = JSON.stringify(detail, null, 2);
      const message = result.cancelled
        ? "Live Gmail scenario complete. Check the message delivery states below; the meeting was confirmed, rescheduled, and cancelled."
        : "Live Gmail scenario complete.";
      setStatus(message);
      await refreshDashboard();
      switchTab("activity");
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  elements.runAutomation.addEventListener("click", async () => {
    try {
      await fetchJson("/api/automation/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      setStatus(state.snapshot?.demo_mode !== false
        ? "Local follow-up cycle complete. No email was sent."
        : "Follow-up cycle complete.");
      await refreshDashboard();
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  elements.refresh.addEventListener("click", async () => {
    try {
      await refreshDashboard();
      setStatus("Dashboard refreshed.");
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  if (elements.showLogs) {
    elements.showLogs.addEventListener("click", async () => {
      try {
        const hadLogs = state.systemLogsLoaded;
        const ok = await refreshSystemLogs();
        if (ok) {
          setStatus(hadLogs ? "System logs refreshed." : "System logs loaded.");
        } else {
          setStatus("Unable to load system logs.", true);
        }
      } catch (error) {
        setStatus(error.message, true);
      }
    });
  }
}

function bindRequestForms() {
  elements.requestForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const participants = String(form.get("participants") || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [email, displayName] = line.split("|").map((item) => item.trim());
        return { email, display_name: displayName || null, required: true };
      });

    const payload = {
      subject: form.get("subject"),
      body: form.get("body"),
      participants,
      mode: form.get("mode") || null,
      requested_location: form.get("requested_location") || null,
    };

    try {
      const detail = await fetchJson("/api/requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.currentRequestId = String(detail.id);
      saveStoredText(STORAGE_KEYS.currentRequestId, state.currentRequestId);
      elements.requestDetail.textContent = JSON.stringify(detail, null, 2);
      setStatus("Meeting request created.");
      await refreshDashboard();
      switchTab("activity");
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  elements.ingestForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = {
      sender_email: form.get("sender_email"),
      sender_name: form.get("sender_name"),
      to: asList(form.get("to")),
      cc: asList(form.get("cc")),
      subject: form.get("subject"),
      body: form.get("body"),
      thread_key: form.get("thread_key") || null,
    };

    try {
      const detail = await fetchJson("/api/emails/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.currentRequestId = String(detail.id);
      saveStoredText(STORAGE_KEYS.currentRequestId, state.currentRequestId);
      elements.requestDetail.textContent = JSON.stringify(detail, null, 2);
      setStatus("Message processed.");
      await refreshDashboard();
      switchTab("activity");
    } catch (error) {
      setStatus(error.message, true);
    }
  });
}

function bootstrap() {
  bindTabs();
  bindWorkspaceForm();
  bindAvailabilityForm();
  bindIntegrationHandlers();
  bindActionButtons();
  bindRequestForms();
  bindDraftPersistence(elements.requestForm, STORAGE_KEYS.requestDraft);
  bindDraftPersistence(elements.ingestForm, STORAGE_KEYS.ingestDraft);
  switchTab(state.activeTab);
  refreshDashboard().catch((error) => setStatus(error.message, true));
  const autoRefreshSeconds = Number(document.body.dataset.autoRefreshSeconds || 0);
  if (autoRefreshSeconds > 0) {
    state.refreshIntervalId = window.setInterval(() => {
      refreshDashboard().catch(() => {
        // Quiet background refresh failures; the next successful poll will recover the UI.
      });
    }, autoRefreshSeconds * 1000);
  }
}

bootstrap();
