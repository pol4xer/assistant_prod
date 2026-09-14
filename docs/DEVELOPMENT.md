# Development

The project uses Python 3.12 or 3.13 with Poetry 2. A lockfile records the
resolved dependencies. Node.js is used only for the JavaScript syntax check;
there is no frontend package installation or build step.

```bash
poetry install
make check
make run
```

## Commands

| Command | Purpose |
| --- | --- |
| `make install` | Install the Poetry environment |
| `make run` | Start the local application factory on port 8000 |
| `make dev` | Run the same factory with reload enabled |
| `make lint` | Check Ruff rules and formatting |
| `make test` | Run pytest |
| `make check` | Validate metadata/lockfile, lint, tests, and dashboard JavaScript |
| `make format` | Apply Ruff fixes and formatting |
| `make docker-up` | Build and start the local Docker demo |
| `make docker-down` | Stop the Docker demo while retaining its volume |

## Verification

The suite uses temporary SQLite databases and fake provider boundaries. It covers
intent extraction, availability conflicts, natural-language option selection,
request confirmation, rescheduling, cancellation, duplicate-message handling,
manual automation, and provider-related transformations. Demo-mode checks verify
that provider actions are blocked and local records do not appear as sent mail.

Mocked Google/OpenAI responses exercise application behavior without granting a
real account or making live calls. A passing suite does not establish external
provider availability, permission configuration, or production reliability.

Use the FastAPI factory with explicit `Settings` and a temporary database path
when adding tests. Keep new fixtures fictional and avoid importing personal
runtime files from outside the checkout.

## Docker demo

```bash
docker compose up --build
```

The Compose service publishes only `127.0.0.1:8000` and forces demo mode with
polling disabled. It uses the `assistant-demo-data` named volume for runtime
state. The service does not pass a host `.env` file into the application.

```bash
docker compose down
```

Stopping the stack preserves the volume, so the next start retains local demo
state. Use the dashboard's **Reset sample data** action when a fresh example is
needed.

## Scope for changes

Keep scheduling rules in `app/services/scheduler.py`, request transitions in
`app/modules/requests/`, and provider-specific behavior in the appropriate
module. The service facade coordinates these pieces and owns the shared local
state.

Add infrastructure only for a concrete requirement. Multiple workers,
authentication, durable dispatch, retries, and full provider pagination require
further design; the current dashboard and polling loop do not supply those
capabilities implicitly.
