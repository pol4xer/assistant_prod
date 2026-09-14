import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from app.core.config import Settings
from app.main import create_app as create_application


def create_app(*, db_path: str):
    """Existing provider contract tests opt into live mode with isolated, mocked clients."""
    root = Path(db_path).parent
    return create_application(
        db_path=db_path,
        config=Settings(
            _env_file=None,
            app_demo_mode=False,
            automation_polling_enabled=False,
            app_secrets_key_path=str(root / "test-secrets.key"),
            log_dir=str(root / "logs"),
            default_timezone="Europe/Istanbul",
            google_oauth_client_id=None,
            google_oauth_client_secret=None,
            oauth_client_id=None,
            oauth_client_secret=None,
        ),
    )


def test_outgoing_request_can_be_confirmed_and_reminded(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    assistant_email = app.state.service.require_workspace()["assistant_email"]

    create_response = client.post(
        "/api/requests",
        json={
            "subject": "Roadmap sync",
            "body": "Please schedule a 45 minute online meeting next Tuesday afternoon.",
            "participants": [
                {"email": "sam@example.com", "display_name": "Sam Partner", "required": True}
            ],
            "mode": "online",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["status"] == "negotiating"
    assert created["options"]

    reply_response = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "sam@example.com",
            "sender_name": "Sam Partner",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Roadmap sync",
            "body": "Option 1 works for me. Confirmed.",
            "thread_key": created["thread_key"],
        },
    )
    assert reply_response.status_code == 200
    confirmed = reply_response.json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["meeting"] is not None

    reminder_at = datetime.fromisoformat(confirmed["confirmation_due_at"]) + timedelta(minutes=1)
    automation_response = client.post(
        "/api/automation/run",
        json={"now": reminder_at.isoformat()},
    )
    assert automation_response.status_code == 200
    assert automation_response.json()["confirmations_sent"] == 1


def test_dashboard_stays_serializable_after_multiple_automation_runs(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)

    first = client.post("/api/automation/run", json={})
    second = client.post("/api/automation/run", json={})
    dashboard = client.get("/api/dashboard")

    assert first.status_code == 200
    assert second.status_code == 200
    assert dashboard.status_code == 200


def test_recent_logs_endpoint_and_request_id_header(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)

    dashboard = client.get("/api/dashboard")
    logs = client.get("/api/system/logs/recent?kind=app&limit=10")

    assert dashboard.status_code == 200
    assert "x-request-id" in dashboard.headers
    assert logs.status_code == 200
    payload = logs.json()
    assert payload["ok"] is True
    assert payload["kind"] == "app"
    assert "path" in payload
    assert isinstance(payload["lines"], list)


def test_due_queries_handle_timezones_via_sql(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service
    now = datetime(2026, 4, 15, 12, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    analysis = service.analyzer.analyze_new_request(
        subject="SQL due check",
        body="Please schedule a 30 minute call tomorrow afternoon.",
        reference_at=now,
        timezone_name="Europe/Istanbul",
        default_duration_minutes=30,
    )

    request_id = service.request_lifecycle.create_request_record(
        analysis=analysis,
        direction="incoming",
        source="email",
        subject="SQL due check",
        organizer_name="Client",
        organizer_email="client@example.com",
        requested_location=None,
        thread_key="sql-due-check",
        created_at=now,
    )

    service.db.update_request(
        request_id,
        {
            "status": "negotiating",
            "next_follow_up_at": "2026-04-15T09:00+00:00",
            "confirmation_due_at": "2026-04-15T09:00+00:00",
            "confirmation_sent_at": None,
        },
    )

    follow_ups = service.db.list_follow_ups_due("2026-04-15T12:00+03:00")
    assert [row["id"] for row in follow_ups] == [request_id]

    service.db.update_request(request_id, {"status": "confirmed"})
    confirmations = service.db.list_confirmations_due("2026-04-15T12:00+03:00")
    assert [row["id"] for row in confirmations] == [request_id]


def test_create_request_is_not_blocked_by_automation_lock(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service

    monkeypatch.setattr(
        service.request_lifecycle,
        "create_outgoing_request",
        lambda payload: {"ok": True, "subject": payload["subject"]},
    )

    ready = threading.Event()
    release = threading.Event()

    def hold_automation_lock():
        service._automation_lock.acquire()
        ready.set()
        release.wait(timeout=5)
        service._automation_lock.release()

    holder = threading.Thread(target=hold_automation_lock)
    holder.start()
    assert ready.wait(timeout=1)

    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "value", service.create_outgoing_request({"subject": "parallel"})
        )
    )
    worker.start()
    worker.join(timeout=0.5)
    release.set()
    holder.join(timeout=1)

    assert not worker.is_alive()
    assert result["value"]["subject"] == "parallel"


def test_request_can_be_deleted_from_current_work(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    assistant_email = service.require_workspace()["assistant_email"]

    monkeypatch.setattr(service, "_remove_google_calendar_event", lambda meeting: True)
    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_delete_me",
            "meeting_link": "https://meet.google.com/delete-me",
        },
    )

    created = client.post(
        "/api/requests",
        json={
            "subject": "Delete me",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "mira@example.com", "display_name": "Mira", "required": True}
            ],
            "mode": "online",
        },
    ).json()

    confirmed = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Delete me",
            "body": "Option 1 works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert confirmed["meeting"]["external_event_id"] == "evt_delete_me"

    deleted = client.delete(f"/api/requests/{created['id']}")

    assert deleted.status_code == 200
    assert deleted.json()["deleted_request_id"] == created["id"]
    assert deleted.json()["calendar_event_removed"] is True

    missing = client.get(f"/api/requests/{created['id']}")
    assert missing.status_code == 404

    dashboard = client.get("/api/dashboard").json()
    assert all(item["id"] != created["id"] for item in dashboard["requests"])


def test_deleted_request_marks_external_gmail_refs_as_ignored(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service

    created = service.ingest_email(
        {
            "sender_email": "client@example.com",
            "sender_name": "Client",
            "to": ["owner@example.com"],
            "cc": ["assistant@example.com"],
            "subject": "Scheduling request",
            "body": "Please schedule a 30 minute call tomorrow afternoon.",
            "provider": "assistant_gmail",
            "external_message_id": "gmail-msg-1",
            "external_thread_id": "gmail-thread-1",
            "internet_message_id": "<gmail-msg-1@example.com>",
        }
    )

    client = TestClient(app)
    deleted = client.delete(f"/api/requests/{created['id']}")

    assert deleted.status_code == 200
    assert service.db.is_ignored_inbox_message("assistant_gmail", "gmail-msg-1") is True
    assert service.db.is_ignored_inbox_thread("assistant_gmail", "gmail-thread-1") is True


def test_duplicate_external_message_ingest_is_idempotent(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service

    first = service.ingest_email(
        {
            "sender_email": "client@example.com",
            "sender_name": "Client",
            "to": ["owner@example.com"],
            "cc": ["assistant@example.com"],
            "subject": "Kitchen remodel",
            "body": "Please schedule a 30 minute call next Tuesday at 16:30.",
            "provider": "assistant_gmail",
            "external_message_id": "gmail-msg-idempotent-1",
            "external_thread_id": "gmail-thread-idempotent-1",
            "internet_message_id": "<gmail-msg-idempotent-1@example.com>",
        }
    )

    second = service.ingest_email(
        {
            "sender_email": "client@example.com",
            "sender_name": "Client",
            "to": ["owner@example.com"],
            "cc": ["assistant@example.com"],
            "subject": "Kitchen remodel",
            "body": "Please schedule a 30 minute call next Tuesday at 16:30.",
            "provider": "assistant_gmail",
            "external_message_id": "gmail-msg-idempotent-1",
            "external_thread_id": "gmail-thread-idempotent-1",
            "internet_message_id": "<gmail-msg-idempotent-1@example.com>",
        }
    )

    assert first["id"] == second["id"]
    assert len(service.db.rows_to_dicts(service.db.list_emails(first["id"]))) == 2

    inbound_messages = [
        item
        for item in service.db.rows_to_dicts(service.db.list_emails(first["id"]))
        if item["direction"] == "inbound"
    ]
    assert len(inbound_messages) == 1


def test_reschedule_clears_confirmed_meeting(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    assistant_email = app.state.service.require_workspace()["assistant_email"]

    created = client.post(
        "/api/requests",
        json={
            "subject": "Commercial review",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "mira@example.com", "display_name": "Mira", "required": True}
            ],
            "mode": "online",
        },
    ).json()

    confirmed = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Commercial review",
            "body": "Option 1 works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert confirmed["status"] == "confirmed"

    replanned = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Commercial review",
            "body": "Can we reschedule to tomorrow at 16:00 instead?",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert replanned["status"] == "negotiating"
    assert replanned["meeting"] is None
    assert replanned["scheduled_starts_at"] is None


def test_confirmed_thread_accept_reply_does_not_duplicate_meeting(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    assistant_email = app.state.service.require_workspace()["assistant_email"]

    created = client.post(
        "/api/requests",
        json={
            "subject": "Vendor sync",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "mira@example.com", "display_name": "Mira", "required": True}
            ],
            "mode": "online",
        },
    ).json()

    first_confirmation = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Vendor sync",
            "body": "Option 1 works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    second_confirmation = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Vendor sync",
            "body": "Yes, option 1 still works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert first_confirmation["status"] == "confirmed"
    assert second_confirmation["status"] == "confirmed"
    assert second_confirmation["meeting"] is not None
    assert second_confirmation["meeting"]["id"] == first_confirmation["meeting"]["id"]
    assert "already confirmed" in second_confirmation["emails"][-1]["body"].lower()


def test_cancelled_thread_reopens_when_new_window_arrives(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    assistant_email = app.state.service.require_workspace()["assistant_email"]

    created = client.post(
        "/api/requests",
        json={
            "subject": "Site walk",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "mira@example.com", "display_name": "Mira", "required": True}
            ],
            "mode": "online",
        },
    ).json()

    cancelled = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Site walk",
            "body": "Please cancel this one.",
            "thread_key": created["thread_key"],
        },
    ).json()

    reopened = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Site walk",
            "body": "Actually next Tuesday at 16:30 works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert cancelled["status"] == "cancelled"
    assert reopened["status"] == "negotiating"
    assert reopened["meeting"] is None
    assert reopened["options"]


def test_integration_credentials_can_be_saved_from_cabinet(tmp_path: Path):
    client = TestClient(create_app(db_path=str(tmp_path / "assistant.db")))

    save_response = client.post(
        "/api/integrations/openai",
        json={
            "values": {
                "api_key": "sk-local-test",
                "model": "gpt-4.1-mini",
                "system_prompt": "Be calm and precise.",
            }
        },
    )
    assert save_response.status_code == 200
    saved = save_response.json()
    assert saved["configured"] is True
    assert saved["values"]["model"] == "gpt-4.1-mini"

    dashboard = client.get("/api/dashboard").json()
    openai = next(item for item in dashboard["integrations"] if item["provider"] == "openai")
    assert openai["configured"] is True
    assert openai["values"]["api_key"] == ""
    assert openai["secret_fields_configured"] == ["api_key"]

    root = client.get("/")
    assert root.status_code == 200
    assert "Scheduling Assistant" in root.text
    assert "/static/dashboard.css" in root.text
    assert "/static/dashboard.js" in root.text

    dashboard_js = client.get("/static/dashboard.js")
    assert dashboard_js.status_code == 200
    assert "refreshDashboard" in dashboard_js.text


def test_workspace_defaults_use_fictional_emails(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    workspace = app.state.service.require_workspace()

    assert workspace["assistant_email"] == "assistant@example.com"
    assert workspace["owner_email"] == "owner@example.com"


def test_google_workspace_oauth_start_redirects_to_google(tmp_path: Path):
    client = TestClient(create_app(db_path=str(tmp_path / "assistant.db")))

    save_response = client.post(
        "/api/integrations/google_workspace",
        json={
            "values": {
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "super-secret",
                "redirect_uri": "http://localhost:8000/auth/google/callback",
            }
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["app_ready"] is True
    assert save_response.json()["configured"] is False

    oauth_start = client.get("/auth/google/start?redirect_to=/", follow_redirects=False)
    assert oauth_start.status_code in (302, 307)
    assert "accounts.google.com" in oauth_start.headers["location"]


def test_legacy_google_token_file_is_not_imported(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tokens.json").write_text(
        '{"client_id": "legacy-demo-client", "client_secret": "legacy-demo-secret", '
        '"refresh_token": "legacy-demo-refresh"}'
    )
    service = create_app(db_path=str(tmp_path / "assistant.db")).state.service
    bundle = service._load_provider_bundle("google_workspace")
    assert not bundle.get("client_id")
    assert not bundle.get("client_secret")
    assert not bundle.get("tokens")


def test_legacy_openai_token_file_is_not_imported(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tokens.json").write_text('{"openai_apikey": "legacy-demo-key"}')
    service = create_app(db_path=str(tmp_path / "assistant.db")).state.service
    assert service._load_provider_bundle("openai") == {}


def test_google_workspace_legacy_callback_route_is_supported(tmp_path: Path):
    client = TestClient(create_app(db_path=str(tmp_path / "assistant.db")))

    save_response = client.post(
        "/api/integrations/google_workspace",
        json={
            "values": {
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "super-secret",
                "redirect_uri": "http://localhost:8000/oauth2/callback",
            }
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["app_ready"] is True

    oauth_start = client.get("/auth/google/start?redirect_to=/", follow_redirects=False)
    assert oauth_start.status_code in (302, 307)
    assert (
        "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Foauth2%2Fcallback"
        in oauth_start.headers["location"]
    )

    legacy_callback = client.get("/oauth2/callback?state=missing", follow_redirects=False)
    assert legacy_callback.status_code == 400
    assert legacy_callback.json()["detail"] == "Google OAuth state is missing or expired"


def test_google_credentials_use_granted_scopes_instead_of_current_defaults(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service
    bundle = {
        "tokens": {
            "token": "test-token",
            "refresh_token": "test-refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "secret",
            "scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/calendar",
            ],
        },
        "granted_scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/calendar",
        ],
    }

    credentials = service._google_credentials(bundle)

    assert credentials.scopes == [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/calendar",
    ]
    assert "https://www.googleapis.com/auth/gmail.modify" not in credentials.scopes


def test_duplicate_outgoing_subjects_get_unique_thread_keys(tmp_path: Path):
    client = TestClient(create_app(db_path=str(tmp_path / "assistant.db")))

    first = client.post(
        "/api/requests",
        json={
            "subject": "Board sync",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [{"email": "sam@example.com", "display_name": "Sam", "required": True}],
            "mode": "online",
        },
    )
    second = client.post(
        "/api/requests",
        json={
            "subject": "Board sync",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "partner@example.com", "display_name": "Nina", "required": True}
            ],
            "mode": "online",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["thread_key"] == "board-sync"
    assert second.json()["thread_key"] == "board-sync-2"


def test_duplicate_incoming_subjects_without_thread_key_create_new_threads(tmp_path: Path):
    client = TestClient(create_app(db_path=str(tmp_path / "assistant.db")))

    first = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "investor@example.com",
            "sender_name": "Nina",
            "to": ["owner@example.com"],
            "cc": ["assistant@example.com"],
            "subject": "Board prep review",
            "body": "Can we meet tomorrow afternoon in person to review the investor deck?",
        },
    )
    second = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "investor@example.com",
            "sender_name": "Nina",
            "to": ["owner@example.com"],
            "cc": ["assistant@example.com"],
            "subject": "Board prep review",
            "body": "Following up with the same ask, still looking for a slot.",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["thread_key"] == "board-prep-review"
    assert second.json()["thread_key"] == "board-prep-review-2"


def test_reply_subject_without_explicit_thread_key_still_joins_existing_thread(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    assistant_email = app.state.service.require_workspace()["assistant_email"]

    created = client.post(
        "/api/requests",
        json={
            "subject": "Roadmap sync",
            "body": "Please schedule a 45 minute online meeting next Tuesday afternoon.",
            "participants": [{"email": "sam@example.com", "display_name": "Sam", "required": True}],
            "mode": "online",
        },
    ).json()

    reply = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "sam@example.com",
            "sender_name": "Sam",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Roadmap sync",
            "body": "Option 1 works for me.",
        },
    )

    assert reply.status_code == 200
    assert reply.json()["id"] == created["id"]
    assert reply.json()["thread_key"] == created["thread_key"]


def test_localhost_redirect_temporarily_enables_insecure_oauth_transport(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service

    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ
    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in os.environ
    with service._oauth_transport_override("http://localhost:8000/oauth2/callback"):
        assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"
        assert os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "1"
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ
    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in os.environ


def test_google_profile_fetch_handles_calendar_api_disabled(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service

    class DummySession:
        def __init__(self, credentials):
            self.credentials = credentials

        def get(self, url, timeout):
            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"email": "alex@example.com"}

            return Response()

    class CalendarListCall:
        def list(self, maxResults):
            return self

        def execute(self):
            raise HttpError(
                SimpleNamespace(status=403, reason="Forbidden"),
                b'{"error":{"message":"Calendar API disabled","status":"PERMISSION_DENIED"}}',
                uri="https://www.googleapis.com/calendar/v3/users/me/calendarList",
            )

    class CalendarService:
        def calendarList(self):
            return CalendarListCall()

    def fake_build(service_name, version, credentials=None, cache_discovery=False):
        assert service_name == "calendar"
        return CalendarService()

    monkeypatch.setattr("app.services.assistant.AuthorizedSession", DummySession)
    monkeypatch.setattr("app.services.assistant.build", fake_build)

    profile = service._fetch_google_profile(SimpleNamespace(scopes=["openid"]))

    assert profile["connected_email"] == "alex@example.com"
    assert profile["calendar_summaries"] == []
    assert any("Google Calendar API" in warning for warning in profile["warnings"])


def test_connect_google_calendar_route_marks_primary_calendar_connected(
    tmp_path: Path, monkeypatch
):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service

    now_iso = datetime.now().astimezone().isoformat(timespec="minutes")
    service._save_provider_bundle(
        "google_workspace",
        {
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "secret",
            "redirect_uri": "http://localhost:8000/oauth2/callback",
            "tokens": {
                "token": "test-token",
                "refresh_token": "test-refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "secret",
                "scopes": ["openid"],
            },
            "account_connected": True,
            "connected_email": "owner@example.com",
        },
        now_iso,
    )

    def fake_sync(bundle, force, reference_time):
        bundle["selected_calendar_id"] = "primary"
        bundle["selected_calendar_summary"] = "Primary Google Calendar"
        bundle["last_calendar_sync_at"] = reference_time.isoformat(timespec="minutes")
        return {
            "ok": True,
            "warnings": [],
            "calendar_id": "primary",
            "calendar_summary": "Primary Google Calendar",
            "imported_busy_slots": 4,
            "last_calendar_sync_at": bundle["last_calendar_sync_at"],
        }

    monkeypatch.setattr(service, "_sync_google_calendar", fake_sync)

    response = client.post("/api/integrations/google_workspace/connect-calendar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["calendar_connected"] is True
    assert payload["selected_calendar_id"] == "primary"
    assert payload["selected_calendar_summary"] == "Primary Google Calendar"


def test_connect_google_calendar_route_handles_calendar_api_disabled(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service

    now_iso = datetime.now().astimezone().isoformat(timespec="minutes")
    service._save_provider_bundle(
        "google_workspace",
        {
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "secret",
            "redirect_uri": "http://localhost:8000/oauth2/callback",
            "tokens": {
                "token": "test-token",
                "refresh_token": "test-refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "secret",
                "scopes": ["openid"],
            },
            "account_connected": True,
            "connected_email": "owner@example.com",
        },
        now_iso,
    )

    def failing_sync(bundle, force, reference_time):
        raise HttpError(
            SimpleNamespace(status=403, reason="Forbidden"),
            b'{"error":{"message":"Google Calendar API has not been used in project yet","status":"PERMISSION_DENIED","details":[{"reason":"accessNotConfigured"}]}}',
            uri="https://www.googleapis.com/calendar/v3/calendars/primary/events",
        )

    monkeypatch.setattr(service, "_sync_google_calendar", failing_sync)

    response = client.post("/api/integrations/google_workspace/connect-calendar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_connected"] is True
    assert payload["calendar_connected"] is False
    assert payload["selected_calendar_id"] is None
    assert any("Google Cloud" in warning for warning in payload["warnings"])
    assert payload["calendar_sync"]["ok"] is False


def test_confirmed_meeting_is_written_to_google_calendar_when_connected(
    tmp_path: Path, monkeypatch
):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    assistant_email = service.require_workspace()["assistant_email"]

    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_123",
            "meeting_link": "https://meet.google.com/test-link",
        },
    )

    created = client.post(
        "/api/requests",
        json={
            "subject": "Launch review",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "mira@example.com", "display_name": "Mira", "required": True}
            ],
            "mode": "online",
        },
    ).json()

    confirmed = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Launch review",
            "body": "Option 1 works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert confirmed["meeting"]["meeting_link"] == "https://meet.google.com/test-link"
    assert confirmed["meeting"]["external_calendar_id"] == "primary"
    assert confirmed["meeting"]["external_event_id"] == "evt_123"


def test_automation_repairs_missing_google_calendar_event(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    assistant_email = service.require_workspace()["assistant_email"]

    now_iso = datetime.now().astimezone().isoformat(timespec="minutes")
    service._save_provider_bundle(
        "google_workspace",
        {
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "secret",
            "redirect_uri": "http://localhost:8000/oauth2/callback",
            "tokens": {
                "token": "test-token",
                "refresh_token": "test-refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "secret",
                "scopes": ["openid", "https://www.googleapis.com/auth/calendar"],
            },
            "granted_scopes": ["openid", "https://www.googleapis.com/auth/calendar"],
            "account_connected": True,
            "connected_email": "owner@example.com",
            "selected_calendar_id": "primary",
            "selected_calendar_summary": "Primary Google Calendar",
        },
        now_iso,
    )

    monkeypatch.setattr(service, "_create_google_calendar_event", lambda **kwargs: None)

    created = client.post(
        "/api/requests",
        json={
            "subject": "Repair booking",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "mira@example.com", "display_name": "Mira", "required": True}
            ],
            "mode": "online",
        },
    ).json()

    confirmed = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Repair booking",
            "body": "Option 1 works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert confirmed["meeting"]["external_event_id"] is None

    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_repaired",
            "meeting_link": "https://meet.google.com/repaired-link",
        },
    )

    automation_response = client.post("/api/automation/run", json={})

    assert automation_response.status_code == 200
    payload = automation_response.json()
    assert payload["repaired_events"] == 1

    repaired = client.get(f"/api/requests/{created['id']}").json()
    assert repaired["meeting"]["external_event_id"] == "evt_repaired"
    assert repaired["meeting"]["meeting_link"] == "https://meet.google.com/repaired-link"


def test_one_click_pipeline_confirms_meeting_for_next_day_1230(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    fixed_now = datetime(2026, 4, 9, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    monkeypatch.setattr(service, "_now", lambda: fixed_now)
    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_one_click",
            "meeting_link": "https://meet.google.com/one-click-real",
        },
    )

    response = client.post("/api/demo/one-click-pipeline")

    assert response.status_code == 200
    payload = response.json()
    detail = payload["detail"]
    assert payload["ok"] is True
    assert payload["calendar_event_created"] is True
    assert detail["status"] == "confirmed"
    assert detail["scheduled_starts_at"] == "2026-04-10T12:30+03:00"
    assert detail["scheduled_ends_at"] == "2026-04-10T13:00+03:00"
    assert detail["meeting"]["meeting_link"] == "https://meet.google.com/one-click-real"


def test_one_click_pipeline_skips_weekend_by_default(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    fixed_now = datetime(2026, 4, 10, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    monkeypatch.setattr(service, "_now", lambda: fixed_now)
    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_weekday",
            "meeting_link": "https://meet.google.com/default-weekday",
        },
    )

    response = client.post("/api/demo/one-click-pipeline")

    assert response.status_code == 200
    detail = response.json()["detail"]
    assert detail["scheduled_starts_at"] == "2026-04-13T12:30+03:00"
    assert detail["scheduled_ends_at"] == "2026-04-13T13:00+03:00"


def test_one_click_pipeline_can_use_saturday_when_availability_allows_it(
    tmp_path: Path, monkeypatch
):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    fixed_now = datetime(2026, 4, 10, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    service.replace_availability(
        [
            {
                "weekday": 5,
                "start_time": "09:00",
                "end_time": "18:00",
                "priority": "normal",
            }
        ]
    )
    monkeypatch.setattr(service, "_now", lambda: fixed_now)
    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_saturday",
            "meeting_link": "https://meet.google.com/saturday-ok",
        },
    )

    response = client.post("/api/demo/one-click-pipeline")

    assert response.status_code == 200
    detail = response.json()["detail"]
    assert detail["scheduled_starts_at"] == "2026-04-11T12:30+03:00"
    assert detail["scheduled_ends_at"] == "2026-04-11T13:00+03:00"


def test_cc_agent_pipeline_uses_fictional_test_emails(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    fixed_now = datetime(2026, 4, 10, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    monkeypatch.setattr(service, "_now", lambda: fixed_now)
    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_cc_agent",
            "meeting_link": "https://meet.google.com/cc-agent-real",
        },
    )

    response = client.post("/api/demo/cc-agent-pipeline")

    assert response.status_code == 200
    payload = response.json()
    detail = payload["detail"]
    assert payload["ok"] is True
    assert payload["calendar_event_created"] is True
    assert detail["status"] == "confirmed"
    assert detail["participants"][0]["email"] == "client@example.com"
    assert detail["meeting"]["meeting_link"] == "https://meet.google.com/cc-agent-real"
    assert detail["emails"][0]["direction"] == "inbound"
    assert detail["emails"][0]["sender_email"] == "client@example.com"
    assert detail["emails"][0]["recipients_json"] == '["owner@example.com"]'
    assert detail["emails"][0]["cc_json"] == '["assistant@example.com"]'


def test_google_calendar_booking_retries_without_meet_when_conference_data_fails(
    tmp_path: Path,
    monkeypatch,
):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service
    workspace = service.require_workspace()

    class FakeInsertRequest:
        def __init__(self, result=None, error=None):
            self._result = result
            self._error = error

        def execute(self):
            if self._error is not None:
                raise self._error
            return self._result

    class FakeEventsResource:
        def __init__(self):
            self.calls = []

        def insert(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return FakeInsertRequest(
                    error=HttpError(
                        SimpleNamespace(status=400, reason="Bad Request"),
                        b'{"error":{"message":"Invalid conference request"}}',
                        uri="https://www.googleapis.com/calendar/v3/calendars/primary/events",
                    )
                )
            return FakeInsertRequest(result={"id": "evt_retry_ok"})

    class FakeCalendarService:
        def __init__(self, events_resource):
            self._events_resource = events_resource

        def events(self):
            return self._events_resource

    fake_events = FakeEventsResource()
    monkeypatch.setattr(
        service,
        "_load_provider_bundle",
        lambda provider: {"selected_calendar_id": "primary", "tokens": {"token": "test-token"}},
    )
    monkeypatch.setattr(
        service, "_google_calendar_service", lambda bundle: FakeCalendarService(fake_events)
    )

    google_event = service._create_google_calendar_event(
        request_row={
            "subject": "Launch review",
            "agenda": "Agenda",
            "summary": "Summary",
        },
        workspace=workspace,
        option_row={
            "mode": "online",
            "starts_at": "2026-04-10T12:30+03:00",
            "ends_at": "2026-04-10T13:00+03:00",
        },
        participant_emails=["mira@example.com"],
        fallback_meeting_link="https://meet.google.com/fallback-link",
    )

    assert google_event == {
        "external_calendar_id": "primary",
        "external_event_id": "evt_retry_ok",
        "meeting_link": None,
    }
    assert len(fake_events.calls) == 2
    assert fake_events.calls[0]["conferenceDataVersion"] == 1
    assert "conferenceData" in fake_events.calls[0]["body"]
    assert "conferenceDataVersion" not in fake_events.calls[1]
    assert "conferenceData" not in fake_events.calls[1]["body"]


def test_google_event_body_uses_rfc3339_seconds(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service
    workspace = service.require_workspace()

    event_body = service._build_google_event_body(
        request_row={
            "subject": "Launch review",
            "agenda": "Agenda",
            "summary": "Summary",
        },
        workspace=workspace,
        option_row={
            "mode": "online",
            "starts_at": "2026-04-10T12:30+03:00",
            "ends_at": "2026-04-10T13:00+03:00",
        },
        participant_emails=["mira@example.com"],
        fallback_meeting_link=None,
        include_conference_data=True,
    )

    assert event_body["start"]["dateTime"] == "2026-04-10T12:30:00+03:00"
    assert event_body["end"]["dateTime"] == "2026-04-10T13:00:00+03:00"


def test_google_meet_link_is_written_into_calendar_event_metadata(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service
    workspace = service.require_workspace()

    class FakeRequest:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload

    class FakeEventsResource:
        def __init__(self):
            self.insert_calls = []
            self.patch_calls = []

        def insert(self, **kwargs):
            self.insert_calls.append(kwargs)
            return FakeRequest(
                {
                    "id": "evt_real_meet",
                    "description": "Agenda",
                    "hangoutLink": "https://meet.google.com/real-link",
                }
            )

        def patch(self, **kwargs):
            self.patch_calls.append(kwargs)
            return FakeRequest(
                {
                    "id": "evt_real_meet",
                    "description": kwargs["body"]["description"],
                    "location": kwargs["body"]["location"],
                    "hangoutLink": "https://meet.google.com/real-link",
                }
            )

    class FakeCalendarService:
        def __init__(self, events_resource):
            self._events_resource = events_resource

        def events(self):
            return self._events_resource

    fake_events = FakeEventsResource()
    monkeypatch.setattr(
        service,
        "_load_provider_bundle",
        lambda provider: {"selected_calendar_id": "primary", "tokens": {"token": "test-token"}},
    )
    monkeypatch.setattr(
        service, "_google_calendar_service", lambda bundle: FakeCalendarService(fake_events)
    )

    google_event = service._create_google_calendar_event(
        request_row={
            "subject": "Launch review",
            "agenda": "Agenda",
            "summary": "Summary",
        },
        workspace=workspace,
        option_row={
            "mode": "online",
            "starts_at": "2026-04-10T12:30+03:00",
            "ends_at": "2026-04-10T13:00+03:00",
        },
        participant_emails=["mira@example.com"],
        fallback_meeting_link=None,
    )

    assert google_event == {
        "external_calendar_id": "primary",
        "external_event_id": "evt_real_meet",
        "meeting_link": "https://meet.google.com/real-link",
    }
    assert len(fake_events.insert_calls) == 1
    assert len(fake_events.patch_calls) == 1
    assert fake_events.patch_calls[0]["sendUpdates"] == "none"
    assert fake_events.patch_calls[0]["body"]["location"] == "https://meet.google.com/real-link"
    assert (
        "Google Meet: https://meet.google.com/real-link"
        in fake_events.patch_calls[0]["body"]["description"]
    )


def test_connect_google_calendar_repairs_confirmed_meetings_without_event_id(
    tmp_path: Path,
    monkeypatch,
):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service
    assistant_email = service.require_workspace()["assistant_email"]
    fixed_now = datetime(2026, 4, 10, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    monkeypatch.setattr(service, "_now", lambda: fixed_now)
    monkeypatch.setattr(service, "_create_google_calendar_event", lambda **kwargs: None)

    created = client.post(
        "/api/requests",
        json={
            "subject": "Repair me",
            "body": "Please schedule a 30 minute online meeting tomorrow afternoon.",
            "participants": [
                {"email": "mira@example.com", "display_name": "Mira", "required": True}
            ],
            "mode": "online",
        },
    ).json()

    confirmed = client.post(
        "/api/emails/ingest",
        json={
            "sender_email": "mira@example.com",
            "sender_name": "Mira",
            "to": [assistant_email],
            "cc": [],
            "subject": "Re: Repair me",
            "body": "Option 1 works for me.",
            "thread_key": created["thread_key"],
        },
    ).json()

    assert confirmed["status"] == "confirmed"
    assert confirmed["meeting"]["external_event_id"] is None

    bundle = service._load_provider_bundle("google_workspace")
    bundle.update(
        {
            "tokens": {"token": "test-token"},
            "account_connected": True,
            "connected_email": "alex@example.com",
        }
    )
    service._save_provider_bundle(
        "google_workspace", bundle, fixed_now.isoformat(timespec="minutes")
    )

    def fake_sync_google_calendar(sync_bundle, force, reference_time):
        sync_bundle["selected_calendar_id"] = "primary"
        sync_bundle["selected_calendar_summary"] = "alex@example.com"
        sync_bundle["last_calendar_sync_at"] = reference_time.isoformat(timespec="minutes")
        return {
            "ok": True,
            "calendar_id": "primary",
            "calendar_summary": "alex@example.com",
            "imported_busy_slots": 0,
            "warnings": [],
            "last_calendar_sync_at": sync_bundle["last_calendar_sync_at"],
        }

    monkeypatch.setattr(service, "_sync_google_calendar", fake_sync_google_calendar)
    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_repaired",
            "meeting_link": "https://meet.google.com/repaired",
        },
    )

    response = client.post("/api/integrations/google_workspace/connect-calendar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["calendar_sync"]["ok"] is True
    assert payload["calendar_sync"]["repaired_events"] == 1

    detail = client.get(f"/api/requests/{created['id']}").json()
    assert detail["meeting"]["external_event_id"] == "evt_repaired"
    assert detail["meeting"]["external_calendar_id"] == "primary"
    assert detail["meeting"]["meeting_link"] == "https://meet.google.com/repaired"


def test_sync_assistant_gmail_route_returns_sync_summary(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    client = TestClient(app)
    service = app.state.service

    monkeypatch.setattr(
        service,
        "_sync_gmail_inbox",
        lambda provider: {"ok": True, "imported_messages": 2, "skipped_messages": 1},
    )

    response = client.post("/api/integrations/assistant_gmail/sync")

    assert response.status_code == 200
    payload = response.json()
    assert payload["gmail_sync"]["ok"] is True
    assert payload["gmail_sync"]["imported_messages"] == 2
    assert payload["gmail_sync"]["skipped_messages"] == 1


def test_gmail_sync_skips_mark_read_when_modify_scope_is_missing(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service

    class FakeMessagesResource:
        def __init__(self):
            self.modify_called = False

        def list(self, **kwargs):
            class Request:
                def execute(self_nonlocal):
                    return {"messages": [{"id": "msg_1"}]}

            return Request()

        def get(self, **kwargs):
            class Request:
                def execute(self_nonlocal):
                    return {
                        "id": "msg_1",
                        "threadId": "thread_1",
                        "internalDate": "1000",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "Jamie Client <client@example.com>"},
                                {"name": "To", "value": "assistant@example.com"},
                                {"name": "Cc", "value": "owner@example.com"},
                                {"name": "Subject", "value": "Re: Manual live test"},
                                {"name": "Date", "value": "Sat, 11 Apr 2026 15:40:00 +0300"},
                                {"name": "Message-ID", "value": "<message-1@example.com>"},
                            ],
                            "body": {},
                        },
                        "snippet": "Option 1 works for me.",
                    }

            return Request()

        def modify(self, **kwargs):
            self.modify_called = True

            class Request:
                def execute(self_nonlocal):
                    return {}

            return Request()

    class FakeUsersResource:
        def __init__(self, messages_resource):
            self._messages_resource = messages_resource

        def messages(self):
            return self._messages_resource

    class FakeGmailService:
        def __init__(self, messages_resource):
            self._users_resource = FakeUsersResource(messages_resource)

        def users(self):
            return self._users_resource

    fake_messages = FakeMessagesResource()
    monkeypatch.setattr(service, "_gmail_service", lambda provider: FakeGmailService(fake_messages))
    monkeypatch.setattr(
        service,
        "_load_provider_bundle",
        lambda provider: {
            "tokens": {"token": "test-token"},
            "granted_scopes": [
                "openid",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            ],
        },
    )
    ingested = []
    monkeypatch.setattr(
        service, "ingest_email", lambda payload: ingested.append(payload) or {"ok": True}
    )

    result = service._sync_gmail_inbox("assistant_gmail")

    assert result["ok"] is True
    assert result["imported_messages"] == 1
    assert result["skipped_messages"] == 0
    assert len(ingested) == 1
    assert fake_messages.modify_called is False


def test_live_gmail_cc_demo_runs_full_lifecycle(tmp_path: Path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service
    fixed_now = datetime(2026, 4, 10, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    monkeypatch.setattr(service, "_now", lambda: fixed_now)
    monkeypatch.setattr(service, "_remove_google_calendar_event", lambda meeting: None)
    monkeypatch.setattr(
        service,
        "_create_google_calendar_event",
        lambda **kwargs: {
            "external_calendar_id": "primary",
            "external_event_id": "evt_live_demo",
            "meeting_link": "https://meet.google.com/live-demo",
        },
    )

    for provider, email in [
        ("google_workspace", "owner@example.com"),
        ("assistant_gmail", "assistant@example.com"),
        ("test_client_gmail", "client@example.com"),
    ]:
        bundle = service._load_provider_bundle(provider)
        bundle.update(
            {
                "tokens": {"token": f"token-{provider}"},
                "connected_email": email,
                "account_connected": True,
            }
        )
        service._save_provider_bundle(provider, bundle, fixed_now.isoformat(timespec="minutes"))

    counter = {"value": 0}

    def fake_send_gmail_message(**kwargs):
        counter["value"] += 1
        return {
            "provider": kwargs["provider"],
            "external_message_id": f"msg_{counter['value']}",
            "external_thread_id": f"thread_{kwargs['provider']}_{counter['value']}",
            "internet_message_id": f"<message-{counter['value']}@example.com>",
        }

    monkeypatch.setattr(service, "_send_gmail_message", fake_send_gmail_message)

    result = service.run_live_gmail_cc_demo()

    assert result["ok"] is True
    assert result["calendar_event_created"] is True
    assert result["cancelled"] is True
    assert result["confirmed_detail"]["status"] == "confirmed"
    assert result["confirmed_detail"]["meeting"]["external_event_id"] == "evt_live_demo"
    assert result["detail"]["status"] == "cancelled"
    assert len(result["detail"]["emails"]) >= 6
