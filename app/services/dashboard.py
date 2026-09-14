from __future__ import annotations

from html import escape


def render_dashboard_page(app_name: str, auto_refresh_seconds: int = 10) -> str:
    safe_app_name = escape(app_name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dashboard · {safe_app_name}</title>
  <link rel="stylesheet" href="/static/dashboard.css" />
</head>
<body data-app-name="{safe_app_name}" data-auto-refresh-seconds="{int(auto_refresh_seconds)}">
  <div class="app-shell">
    <div id="modeBanner" class="mode-banner" role="status">Loading workspace mode…</div>
    <header class="masthead">
      <section class="masthead__intro panel">
        <div class="eyebrow">Scheduling assistant</div>
        <h1>{safe_app_name}</h1>
        <p class="masthead__lead">
          Explore a scheduling assistant in one place: working hours, meeting requests,
          conversation history, and follow-up automation.
        </p>
        <div class="masthead__chips">
          <span class="chip">1. Workspace</span>
          <span class="chip">2. Scenarios</span>
          <span class="chip">3. Activity</span>
        </div>
      </section>

      <aside class="masthead__actions panel">
        <div class="panel__header">
          <div>
            <h2>Try a scenario</h2>
            <p>Explore the scheduling flow with fictional example contacts.</p>
          </div>
        </div>
        <div class="quick-actions">
          <button id="loadDemo">Reset sample data</button>
          <button id="runOneClickPipeline">Book a sample meeting</button>
          <button id="runLiveGmailCcFlow" class="secondary" disabled>Live Gmail scenario</button>
          <button id="runAutomation" class="ghost">Run follow-ups</button>
          <button id="refresh" class="ghost">Refresh dashboard</button>
        </div>
        <div id="scenarioHint" class="helper-note">
          Start with <strong>Book a sample meeting</strong>, then inspect the conversation in Activity.
          Demo mode uses local message records and fictional contacts. No Google account is required.
        </div>
        <div id="statusBanner" class="status-banner" hidden></div>
      </aside>
    </header>

    <section class="overview">
      <div id="summaryCards" class="summary-grid"></div>
      <div class="overview__side">
        <article class="panel panel--compact">
          <div class="panel__header">
            <div>
              <h2>Integration status</h2>
              <p>Available adapters and the current execution mode.</p>
            </div>
          </div>
          <div id="integrationHealth" class="prose small"></div>
          <div id="readinessPills" class="pill-row" style="margin-top: 14px;"></div>
        </article>

        <article class="panel panel--compact">
          <div class="panel__header">
            <div>
              <h2>Automation queue</h2>
              <p>Pending follow-ups and meeting reminders.</p>
            </div>
          </div>
          <div id="automationSummary" class="prose small"></div>
        </article>

        <article class="panel panel--compact">
          <div class="panel__header">
            <div>
              <h2>System logs</h2>
              <p>Recent application errors, available on request.</p>
            </div>
            <button id="showLogs" class="ghost" type="button">Show logs</button>
          </div>
          <div id="systemLogs" class="prose small">
            <div class="muted">Logs load only when you select <strong>Show logs</strong>.</div>
          </div>
        </article>
      </div>
    </section>

    <nav class="section-tabs" aria-label="Dashboard sections">
      <button class="tab-button active" data-tab="setup">1. Workspace</button>
      <button class="tab-button" data-tab="test">2. Scenarios</button>
      <button class="tab-button" data-tab="activity">3. Activity</button>
    </nav>

    <main class="workspace-pages">
      <section id="tab-setup" class="tab-page active">
        <div class="page-head">
          <div>
            <h2>Workspace and scheduling rules</h2>
            <p>Profile, availability, adapters, and calendar sources.</p>
          </div>
        </div>

        <div class="content-grid content-grid--primary">
          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>Assistant profile</h2>
                <p>Set the assistant identity, calendar owner, and scheduling preferences.</p>
              </div>
            </div>
            <form id="workspaceForm" class="panel__body">
              <div class="form-grid">
                <label>Assistant name<input name="assistant_name" /></label>
                <label>Assistant email<input name="assistant_email" /></label>
                <label>Owner name<input name="owner_name" /></label>
                <label>Owner email<input name="owner_email" /></label>
                <label>Timezone<input name="timezone" /></label>
                <label>Online meeting provider
                  <select name="online_provider">
                    <option value="google_meet">Google Meet</option>
                    <option value="zoom" disabled>Zoom — not implemented</option>
                  </select>
                </label>
                <label>Default duration (minutes)<input name="default_duration_minutes" type="number" min="15" max="240" /></label>
                <label>Meeting buffer (minutes)<input name="meeting_buffer_minutes" type="number" min="0" max="120" /></label>
                <label>Minimum notice (hours)<input name="min_notice_hours" type="number" min="0" max="168" /></label>
                <label>Reminder lead time (hours)<input name="confirmation_lead_hours" type="number" min="1" max="168" /></label>
                <label>Workday start<input name="workday_start" /></label>
                <label>Workday end<input name="workday_end" /></label>
                <label>Base location<input name="base_location_label" /></label>
                <label>Base latitude<input name="base_lat" type="number" step="any" /></label>
                <label>Base longitude<input name="base_lng" type="number" step="any" /></label>
              </div>
              <div class="form-actions">
                <button type="submit">Save profile</button>
              </div>
            </form>
          </article>

          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>External adapters</h2>
                <p>Google Calendar, Gmail, and optional AI parsing.</p>
              </div>
            </div>
            <div class="panel__body">
              <div id="integrationModeNote" class="callout">
                Loading adapter availability…
              </div>
              <div id="integrations" class="integration-grid"></div>
            </div>
          </article>
        </div>

        <div class="content-grid content-grid--secondary">
          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>Availability</h2>
                <p>Choose when the assistant may propose a meeting.</p>
              </div>
            </div>
            <form id="availabilityForm" class="panel__body">
              <div class="checkbox-grid">
                <label class="checkbox-pill"><input type="checkbox" name="weekday" value="0" />Monday</label>
                <label class="checkbox-pill"><input type="checkbox" name="weekday" value="1" />Tuesday</label>
                <label class="checkbox-pill"><input type="checkbox" name="weekday" value="2" />Wednesday</label>
                <label class="checkbox-pill"><input type="checkbox" name="weekday" value="3" />Thursday</label>
                <label class="checkbox-pill"><input type="checkbox" name="weekday" value="4" />Friday</label>
                <label class="checkbox-pill"><input type="checkbox" name="weekday" value="5" />Saturday</label>
                <label class="checkbox-pill"><input type="checkbox" name="weekday" value="6" />Sunday</label>
              </div>
              <div class="form-grid">
                <label>Start time<input name="start_time" type="time" value="09:00" /></label>
                <label>End time<input name="end_time" type="time" value="18:00" /></label>
              </div>
              <div class="form-actions">
                <button type="submit">Save availability</button>
              </div>
              <div id="availability" class="stack-list"></div>
            </form>
          </article>

          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>Calendars and busy slots</h2>
                <p>Calendar sources and time blocks used by the scheduler.</p>
              </div>
            </div>
            <div class="panel__body panel__body--split">
              <div>
                <h3 class="subheading">Calendars</h3>
                <div id="calendars" class="stack-list"></div>
              </div>
              <div>
                <h3 class="subheading">Busy slots</h3>
                <div id="busySlots" class="stack-list"></div>
              </div>
            </div>
          </article>

          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>In-person meeting locations</h2>
                <p>Sample locations for the distance-based travel estimate. Maps routing is not implemented.</p>
              </div>
            </div>
            <div class="panel__body">
              <div id="locations" class="stack-list"></div>
            </div>
          </article>
        </div>
      </section>

      <section id="tab-test" class="tab-page">
        <div class="page-head">
          <div>
            <h2>Scheduling scenarios</h2>
            <p>Explore prepared scenarios or enter a meeting request yourself.</p>
          </div>
        </div>

        <div class="content-grid content-grid--primary">
          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>A quick walkthrough</h2>
                <p>Follow the complete scheduling flow in a few steps.</p>
              </div>
            </div>
            <div class="panel__body">
              <div class="checklist">
                <div class="checklist__item">
                  <strong>1. Explore the sample workspace</strong>
                  <span>Review the fictional contacts, working hours, and calendar blocks.</span>
                </div>
                <div class="checklist__item">
                  <strong>2. Book a sample meeting</strong>
                  <span>The scenario creates a request, proposes a slot, and records an acceptance.</span>
                </div>
                <div class="checklist__item">
                  <strong>3. Open Activity</strong>
                  <span>Inspect the request, message records, time options, and confirmed meeting.</span>
                </div>
              </div>
              <div class="callout callout--soft">
                Use the forms below to explore your own scheduling cases:
                create a request or simulate a reply by reusing its thread key.
              </div>
            </div>
          </article>

          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>What you can explore</h2>
                <p>Each action demonstrates a different part of the workflow.</p>
              </div>
            </div>
            <div class="panel__body prose small">
              <p><strong>Book a sample meeting</strong> creates a request, proposes a time, and records confirmation.</p>
              <p><strong>Simulate an incoming message</strong> lets a fictional client contact the owner with the assistant in CC.</p>
              <p><strong>Live Gmail scenario</strong> is available only in explicitly configured live mode; it can send real email.</p>
              <p><strong>Run follow-ups</strong> processes due reminders and follow-ups. In demo mode, messages stay local.</p>
            </div>
          </article>
        </div>

        <div class="content-grid content-grid--primary">
          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>Create a meeting request</h2>
                <p>Describe the meeting and let the assistant propose suitable times.</p>
              </div>
            </div>
            <form id="requestForm" class="panel__body">
              <label>Subject<input name="subject" value="Product roadmap sync" /></label>
              <label>Scheduling request<textarea name="body">Please schedule a 45 minute online meeting next Tuesday afternoon to align on the roadmap.</textarea></label>
              <div class="form-grid">
                <label>Participants (one per line: email|name)<textarea name="participants">client@example.com|Jordan Lee</textarea></label>
                <div>
                  <label>Meeting format
                    <select name="mode">
                      <option value="">Flexible</option>
                      <option value="online" selected>Online</option>
                      <option value="offline">In person</option>
                    </select>
                  </label>
                  <label>Preferred location<input name="requested_location" placeholder="For an in-person meeting" /></label>
                </div>
              </div>
              <div class="form-actions">
                <button type="submit">Create request</button>
              </div>
            </form>
          </article>

          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>Simulate an incoming message</h2>
                <p>Create a message record or reply to an existing conversation.</p>
              </div>
            </div>
            <form id="ingestForm" class="panel__body">
              <div class="form-grid">
                <label>Sender email<input name="sender_email" value="client@example.com" /></label>
                <label>Sender name<input name="sender_name" value="Jordan Lee" /></label>
                <label>To (comma separated)<input name="to" value="owner@example.com" /></label>
                <label>CC (comma separated)<input name="cc" value="assistant@example.com" /></label>
              </div>
              <label>Subject<input name="subject" value="Strategy review" /></label>
              <label>Message<textarea name="body">Hi Alex, could we schedule a 30 minute online meeting next Tuesday afternoon? I am cc'ing Marlow so the assistant can handle the scheduling.</textarea></label>
              <label>Thread key (for a reply)<input name="thread_key" placeholder="Leave blank to start a new thread" /></label>
              <div class="form-actions">
                <button type="submit" class="secondary">Process message</button>
              </div>
            </form>
          </article>
        </div>
      </section>

      <section id="tab-activity" class="tab-page">
        <div class="page-head">
          <div>
            <h2>Activity</h2>
            <p>Current requests, confirmed meetings, and conversation records.</p>
          </div>
        </div>

        <div class="content-grid content-grid--primary">
          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>Requests and conversations</h2>
                <p>Select a request to explore its messages and scheduling state.</p>
              </div>
            </div>
            <div id="requests" class="panel__body panel__body--table"></div>
          </article>

          <article class="panel">
            <div class="panel__header">
              <div>
                <h2>Confirmed meetings</h2>
                <p>Meetings confirmed in the current workspace.</p>
              </div>
            </div>
            <div id="meetings" class="panel__body panel__body--table"></div>
          </article>
        </div>

        <article class="panel">
          <div class="panel__header">
            <div>
              <h2>Request details</h2>
              <p>Message delivery states and the full request record.</p>
            </div>
          </div>
          <div class="panel__body">
            <div id="requestDetailActions" class="detail-actions" hidden></div>
            <div id="requestMessages" class="stack-list" hidden></div>
            <details>
              <summary>View technical record</summary>
              <pre id="requestDetail">Select a request above to see its full record.</pre>
            </details>
          </div>
        </article>
      </section>
    </main>
  </div>

  <script type="module" src="/static/dashboard.js"></script>
</body>
</html>"""
