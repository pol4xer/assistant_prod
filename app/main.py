from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import LiveIntegrationsDisabled, Settings
from app.core.config import settings as default_settings
from app.core.db import Database
from app.core.log_reader import tail_lines
from app.core.logging import reset_request_id, set_request_id, setup_logging
from app.schemas import (
    AutomationRunInput,
    AvailabilityRuleInput,
    BusySlotInput,
    CalendarInput,
    CreateRequestInput,
    IngestEmailInput,
    IntegrationCredentialsInput,
    LocationInput,
    WorkspaceUpdateInput,
)
from app.services.assistant import SchedulingAssistantService
from app.services.dashboard import render_dashboard_page

logger = logging.getLogger(__name__)


def create_app(db_path: str | None = None, config: Settings | None = None) -> FastAPI:
    settings = config or default_settings
    setup_logging(
        log_level=settings.log_level,
        app_env=settings.app_env,
        log_dir=settings.log_dir,
        json_logs=bool(settings.log_json),
        max_bytes=int(settings.log_max_bytes),
        backup_count=int(settings.log_backup_count),
    )
    database = Database(db_path or settings.state_db_path)
    service = SchedulingAssistantService(database, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.automation_runtime.start()
        try:
            yield
        finally:
            await service.automation_runtime.stop()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.exception_handler(LiveIntegrationsDisabled)
    async def live_integrations_disabled(request: Request, exc: LiveIntegrationsDisabled):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    app.state.service = service
    app.state.log_dir = settings.log_dir
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(
        "Scheduling Assistant app ready db_path=%s env=%s timezone=%s",
        database.path,
        settings.app_env,
        settings.default_timezone,
    )

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "app": settings.app_name,
            "env": settings.app_env,
            "demo_mode": service.demo_mode,
            "mode": "demo" if service.demo_mode else "live",
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return render_dashboard_page(settings.app_name, settings.dashboard_auto_refresh_seconds)

    @app.get("/api/dashboard")
    def api_dashboard() -> dict:
        return service.dashboard_snapshot()

    @app.get("/api/system/logs/recent")
    def api_recent_logs(
        kind: str = Query("app", pattern="^(app|error)$"),
        limit: int = Query(settings.log_recent_default_lines, ge=1, le=1000),
    ) -> dict:
        filename = "error.log" if kind == "error" else "app.log"
        path = Path(app.state.log_dir) / filename
        return {
            "ok": True,
            "kind": kind,
            "path": str(path),
            "lines": tail_lines(path, limit=limit),
        }

    @app.get("/auth/google/start")
    def auth_google_start(
        provider: str = Query("google_workspace", description="Which Google provider to connect"),
        redirect_to: str = Query("/", description="Where to return after login"),
    ):
        try:
            return RedirectResponse(
                service.start_google_oauth(provider=provider, redirect_to=redirect_to)
            )
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def _handle_google_callback(request: Request, state: str) -> HTMLResponse:
        try:
            result = service.complete_google_oauth(str(request.url), state)
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        redirect_to = result.get("redirect_to") or "/"
        connected_email = result.get("connected_email") or "your Google account"
        provider = result.get("provider") or "google_workspace"
        if provider == "google_workspace":
            next_step = (
                "<p>Next step: open the dashboard and click <strong>Connect My Calendar</strong>.</p>"
                if result.get("account_connected") or result.get("connected_email")
                else ""
            )
            title = "Google account connected"
        elif provider == "assistant_gmail":
            next_step = "<p>Next step: open the dashboard and click <strong>Sync Assistant Gmail</strong>.</p>"
            title = "Assistant Gmail connected"
        else:
            next_step = "<p>Next step: you can now use the live Gmail demo button.</p>"
            title = "Test Client Gmail connected"
        return HTMLResponse(
            (
                "<html><body style='font-family:sans-serif;padding:24px;'>"
                "<h2>%s</h2>"
                "<p>Connected as <strong>%s</strong>.</p>"
                "%s"
                "<p><a href='%s'>Back to dashboard</a></p>"
                "</body></html>"
            )
            % (title, connected_email, next_step, redirect_to)
        )

    @app.get("/auth/google/callback", response_class=HTMLResponse)
    def auth_google_callback(request: Request, state: str = Query(...)) -> HTMLResponse:
        return _handle_google_callback(request, state)

    @app.get("/oauth2/callback", response_class=HTMLResponse)
    def auth_google_callback_legacy(request: Request, state: str = Query(...)) -> HTMLResponse:
        return _handle_google_callback(request, state)

    @app.post("/api/demo/reset")
    def api_demo_reset() -> dict:
        return service.reset_demo_data()

    @app.post("/api/demo/one-click-pipeline")
    def api_demo_one_click_pipeline() -> dict:
        try:
            return service.run_one_click_test_pipeline()
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/demo/live-gmail-cc-flow")
    def api_demo_live_gmail_cc_flow() -> dict:
        try:
            return service.run_live_gmail_cc_demo()
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/demo/cc-agent-pipeline")
    def api_demo_cc_agent_pipeline() -> dict:
        try:
            return service.run_cc_assistant_test_pipeline()
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/requests/{request_id}")
    def api_request_detail(request_id: int) -> dict:
        try:
            return service.get_request_detail(request_id)
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.delete("/api/requests/{request_id}")
    def api_delete_request(request_id: int) -> dict:
        try:
            return service.delete_request(request_id)
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/workspace")
    def api_update_workspace(payload: WorkspaceUpdateInput) -> dict:
        return service.update_workspace(payload.model_dump())

    @app.put("/api/availability")
    def api_replace_availability(payload: list[AvailabilityRuleInput]) -> list[dict]:
        return service.replace_availability([item.model_dump() for item in payload])

    @app.post("/api/calendars")
    def api_add_calendar(payload: CalendarInput) -> dict:
        return service.add_calendar(payload.model_dump())

    @app.post("/api/calendars/{calendar_id}/busy-slots")
    def api_add_busy_slot(calendar_id: int, payload: BusySlotInput) -> dict:
        return service.add_busy_slot(calendar_id, payload.model_dump())

    @app.post("/api/locations")
    def api_add_location(payload: LocationInput) -> dict:
        return service.add_location(payload.model_dump())

    @app.post("/api/requests")
    def api_create_request(payload: CreateRequestInput) -> dict:
        return service.create_outgoing_request(payload.model_dump())

    @app.post("/api/emails/ingest")
    def api_ingest_email(payload: IngestEmailInput) -> dict:
        return service.ingest_email(payload.model_dump())

    @app.post("/api/automation/run")
    def api_run_automation(payload: AutomationRunInput) -> dict:
        return service.run_automation(payload.now)

    @app.post("/api/integrations/{provider}")
    def api_save_integration_credentials(
        provider: str, payload: IntegrationCredentialsInput
    ) -> dict:
        try:
            return service.save_integration_credentials(provider, payload.values)
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/integrations/{provider}/disconnect")
    def api_disconnect_google_provider(provider: str) -> dict:
        try:
            return service.disconnect_google_provider(provider)
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/integrations/google_workspace/connect-calendar")
    def api_connect_google_calendar() -> dict:
        try:
            return service.connect_google_calendar()
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/integrations/assistant_gmail/sync")
    def api_sync_assistant_gmail() -> dict:
        try:
            return service.sync_assistant_gmail()
        except LiveIntegrationsDisabled:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return app
