# Setup

## Offline walkthrough

Use Python 3.12 or 3.13 and Poetry 2, then start from the source checkout:

```bash
poetry install
make run
```

The command runs the application factory on `127.0.0.1:8000`. Open the dashboard,
select **Book a sample meeting** under **2. Scenarios**, and inspect its history
under **3. Activity**. The fictional workspace uses example.com addresses; no
account, OAuth grant, or API key is required.

**Run follow-ups** processes reminders that are currently due. **Reset sample
data** replaces the local demo state with a fresh fictional workspace. It is a
demo reset, not a backup operation.

## Local settings

Settings are read from process environment variables and, when present, the
checkout's `.env` file. Copy the template only if you want overrides:

```bash
cp .env.example .env
```

The default factory configuration does not require this file. Environment
variables take precedence over `.env` values.

| Setting | Default | Purpose |
| --- | --- | --- |
| `APP_DEMO_MODE` | `true` | Block live integrations and keep messages local |
| `STATE_DB_PATH` | `./var/assistant_state.db` | SQLite workflow database |
| `APP_SECRETS_KEY_PATH` | `./var/assistant_secrets.key` | Key file used only for optional live provider storage |
| `LOG_DIR` | `./var/log` | Rotating application logs |
| `DEFAULT_TIMEZONE` | `UTC` | Initial workspace timezone |
| `DEFAULT_DURATION_MINUTES` | `45` | Default meeting duration |
| `MEETING_BUFFER_MINUTES` | `15` | Time reserved around meetings |
| `MIN_NOTICE_HOURS` | `2` | Earliest acceptable meeting notice |
| `CONFIRMATION_LEAD_HOURS` | `24` | Confirmation reminder lead time |
| `AUTOMATION_POLLING_ENABLED` | `false` | Optional live background automation |
| `DASHBOARD_AUTO_REFRESH_SECONDS` | `10` | Dashboard refresh interval |

`.env.example` uses shorter database/key filenames under the same ignored `var/`
directory. Workspace defaults apply when a workspace is created; edit an existing
workspace through the dashboard.

`make run` sets the Uvicorn bind address and port explicitly. For a different
local port, invoke the factory directly:

```bash
poetry run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001
```

## Optional provider experiments

The live integration code is included for inspection. It was not exercised
against real Google or OpenAI accounts during the portfolio refresh. Keep the
default demo for ordinary review.

A deliberate live experiment needs a separate local runtime database, explicit
`APP_DEMO_MODE=false`, and provider configuration. Merely adding credentials while
demo mode is enabled does not activate providers.

- Google OAuth configuration uses client ID, client secret, and a matching local
  callback URL. The default callback path is `/auth/google/callback`.
- The owner Google account supplies calendar access. A separate assistant Gmail
  connection handles the assistant mailbox; a test-client connection exists for
  the experimental live Gmail scenario.
- OpenAI configuration supplies an API key and selected model for the optional
  message-analysis overlay. The local rule-based analyzer remains available
  without it.
- Polling remains off unless `AUTOMATION_POLLING_ENABLED=true` is also set in live
  mode. Manual actions can still cause external activity when live providers are
  connected.

Configure provider values through the local integration forms or the supported
Google environment fields shown in `.env.example`. No token file is imported
implicitly. Keep credential files, the SQLite database, vault key, and logs in
ignored local storage.

Live actions can send emails, create or remove calendar events, and make paid
API calls. The dashboard has no user authentication and is intended for one
local operator. This repository does not supply a public hosting configuration.
