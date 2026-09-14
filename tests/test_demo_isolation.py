"""Offline portfolio mode must stay local even when credentials already exist."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.core.config import LiveIntegrationsDisabled, Settings
from app.main import create_app
from app.services.llm import OpenAIAnalysisError, OpenAIEmailAnalyzer
from app.services.scheduler import Scheduler


def isolated_config(tmp_path: Path, *, demo_mode: bool = True, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        app_demo_mode=demo_mode,
        state_db_path=str(tmp_path / "state.db"),
        app_secrets_key_path=str(tmp_path / "secrets.key"),
        log_dir=str(tmp_path / "logs"),
        **overrides,
    )


def test_importing_app_does_not_create_runtime_files(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo), APP_DEMO_MODE="true")
    subprocess.run(
        [sys.executable, "-c", "import app.main; assert not hasattr(app.main, 'app')"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert list(tmp_path.iterdir()) == []


def test_default_configuration_is_local_demo(monkeypatch):
    for key in ["APP_DEMO_MODE", "AUTOMATION_POLLING_ENABLED", "APP_HOST"]:
        monkeypatch.delenv(key, raising=False)
    config = Settings(_env_file=None)
    assert config.app_demo_mode is True
    assert config.automation_polling_enabled is False
    assert config.app_host == "127.0.0.1"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/auth/google/start", None),
        ("get", "/auth/google/callback?state=demo-state&code=demo-code", None),
        ("post", "/api/integrations/openai", {"values": {"api_key": "demo-key"}}),
        ("post", "/api/integrations/google_workspace/disconnect", None),
        ("post", "/api/integrations/google_workspace/connect-calendar", None),
        ("post", "/api/integrations/assistant_gmail/sync", None),
        ("post", "/api/demo/live-gmail-cc-flow", None),
    ],
)
def test_live_endpoints_are_blocked_in_demo(tmp_path, method, path, payload):
    app = create_app(config=isolated_config(tmp_path))
    with TestClient(app) as client:
        response = client.request(method, path, json=payload, follow_redirects=False)
    assert response.status_code == 403
    assert "demo mode" in response.json()["detail"]
    assert not (tmp_path / "secrets.key").exists()


def test_demo_ignores_existing_credentials_and_never_decrypts_them(tmp_path, monkeypatch):
    live = create_app(config=isolated_config(tmp_path, demo_mode=False)).state.service
    for provider in ["assistant_gmail", "google_workspace", "openai"]:
        live._save_provider_bundle(
            provider,
            {
                "api_key": "demo-only-placeholder",
                "tokens": {"token": "demo-only-placeholder"},
                "account_connected": True,
                "selected_calendar_id": "primary",
            },
            "2026-04-10T10:00+00:00",
        )

    def forbidden(*args, **kwargs):
        pytest.fail("Demo must not initialize a provider client or decrypt credentials")

    monkeypatch.setattr("app.services.assistant.SecretVault", forbidden)
    monkeypatch.setattr("app.modules.integrations.service.build", forbidden)
    monkeypatch.setattr("app.services.llm.OpenAI", forbidden)
    app = create_app(config=isolated_config(tmp_path, automation_polling_enabled=True))
    service = app.state.service
    with TestClient(app) as client:
        assert client.get("/health").json()["mode"] == "demo"
        snapshot = client.get("/api/dashboard").json()
        assert snapshot["demo_mode"] is True
        assert not snapshot["automation"]["runtime"]["enabled"]
        assert not any(item["configured"] for item in snapshot["integrations"])
        result = client.post("/api/demo/one-click-pipeline")
        assert result.status_code == 200
        detail = result.json()["detail"]
        assert detail["status"] == "confirmed"
        assert detail["meeting"]["meeting_link"] is None
        assert all(email["delivery_status"] == "local" for email in detail["emails"])
        assert client.post("/api/automation/run", json={}).status_code == 200
    assert service._load_provider_bundle("openai") == {}
    assert (
        service._analyze_with_openai(
            subject="Meeting",
            body="Tomorrow",
            reference_at=datetime.now(ZoneInfo("UTC")),
            timezone_name="UTC",
            default_duration_minutes=30,
            forced_intent=None,
        )
        is None
    )
    with pytest.raises(LiveIntegrationsDisabled):
        service._gmail_service("assistant_gmail")
    with pytest.raises(LiveIntegrationsDisabled):
        service._google_calendar_service({"tokens": {"token": "demo-placeholder"}})


def test_llm_client_requires_explicit_network_opt_in(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("OpenAI client must not be created without explicit opt-in")

    monkeypatch.setattr("app.services.llm.OpenAI", forbidden)
    with pytest.raises(OpenAIAnalysisError, match="explicit opt-in"):
        OpenAIEmailAnalyzer(api_key="demo-placeholder")


@pytest.mark.parametrize("provider", ["zoom", "google_meet", "unsupported"])
def test_scheduler_never_fabricates_a_provider_meeting_url(provider):
    assert Scheduler().build_meeting_link(provider, "req_demo") is None


@pytest.mark.parametrize("recipient_field", ["to", "cc"])
def test_new_gmail_request_requires_assistant_in_to_or_cc(tmp_path, recipient_field):
    service = create_app(config=isolated_config(tmp_path)).state.service
    transport = service.gmail_transport
    parsed = {
        "sender_email": "client@example.com",
        "subject": "Schedule a meeting",
        "body": "Can we schedule a 30 minute call tomorrow?",
        recipient_field: ["ASSISTANT@example.com"],
    }
    assert transport.should_auto_ignore_message("assistant_gmail", {}, parsed, None) is None
    parsed[recipient_field] = ["owner@example.com"]
    parsed["bcc"] = ["assistant@example.com"]
    parsed["body"] += " assistant@example.com is mentioned here."
    assert (
        transport.should_auto_ignore_message("assistant_gmail", {}, parsed, None)
        == "assistant-not-addressed"
    )
    assert transport.should_auto_ignore_message("assistant_gmail", {}, parsed, {"id": 1}) is None
    assert transport.parse_email_header_list(
        '"Morgan, Alex" <owner@example.com>, Assistant <assistant@example.com>'
    ) == ["owner@example.com", "assistant@example.com"]


def test_failed_gmail_delivery_is_recorded_as_failed(tmp_path, monkeypatch):
    service = create_app(config=isolated_config(tmp_path, demo_mode=False)).state.service
    service._save_provider_bundle(
        "assistant_gmail", {"tokens": {"token": "demo-placeholder"}}, "2026-04-10T10:00+00:00"
    )

    def fail_send(**kwargs):
        raise RuntimeError("Simulated provider rejection")

    monkeypatch.setattr(service, "_send_gmail_message", fail_send)
    detail = service.create_outgoing_request(
        {
            "subject": "Project planning",
            "body": "Schedule a 30 minute online meeting next Tuesday",
            "participants": [{"email": "client@example.com", "required": True}],
            "mode": "online",
        }
    )
    outbound = [email for email in detail["emails"] if email["direction"] == "outbound"]
    assert len(outbound) == 1
    assert outbound[0]["delivery_status"] == "failed"
    assert "Simulated provider rejection" in outbound[0]["delivery_error"]
    assert outbound[0]["external_message_id"] is None


def test_secret_fields_are_redacted_and_blank_resubmission_preserves_them(tmp_path):
    service = create_app(config=isolated_config(tmp_path, demo_mode=False)).state.service
    first = service.save_integration_credentials(
        "openai", {"api_key": "demo-only-key", "model": "demo-model"}
    )
    assert first["values"]["api_key"] == ""
    assert "demo-only-key" not in json.dumps(service.dashboard_snapshot())
    second = service.save_integration_credentials(
        "openai", {"api_key": "", "model": "updated-demo-model"}
    )
    assert second["configured"] is True
    assert service._load_provider_bundle("openai")["api_key"] == "demo-only-key"


def test_successful_gmail_send_survives_metadata_lookup_failure(tmp_path, monkeypatch):
    service = create_app(config=isolated_config(tmp_path, demo_mode=False)).state.service
    service._save_provider_bundle(
        "assistant_gmail", {"tokens": {"token": "demo-placeholder"}}, "2026-04-10T10:00+00:00"
    )
    gmail = Mock()
    messages = gmail.users.return_value.messages.return_value
    messages.send.return_value.execute.return_value = {
        "id": "sent-demo-1",
        "threadId": "demo-thread",
    }
    messages.get.return_value.execute.side_effect = RuntimeError("Simulated metadata outage")
    monkeypatch.setattr(service, "_gmail_service", lambda provider: gmail)
    detail = service.create_outgoing_request(
        {
            "subject": "Project planning",
            "body": "Schedule a 30 minute online meeting next Tuesday",
            "participants": [{"email": "client@example.com", "required": True}],
            "mode": "online",
        }
    )
    outbound = [email for email in detail["emails"] if email["direction"] == "outbound"]
    assert outbound[0]["delivery_status"] == "sent"
    assert outbound[0]["external_message_id"] == "sent-demo-1"
    assert outbound[0]["delivery_error"] is None
    messages.send.assert_called_once()


def test_assistant_can_be_added_to_cc_later_in_a_previously_unaddressed_thread(
    tmp_path, monkeypatch
):
    service = create_app(config=isolated_config(tmp_path, demo_mode=False)).state.service
    gmail = Mock()
    messages = gmail.users.return_value.messages.return_value
    monkeypatch.setattr(service, "_gmail_service", lambda provider: gmail)
    ingested = []
    monkeypatch.setattr(service, "ingest_email", lambda payload: ingested.append(payload))

    def message(identifier, cc):
        return {
            "id": identifier,
            "threadId": "demo-thread",
            "internalDate": "1000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "client@example.com"},
                    {"name": "To", "value": "owner@example.com"},
                    {"name": "Cc", "value": cc},
                    {"name": "Subject", "value": "Schedule a planning meeting"},
                ]
            },
            "snippet": "Can we schedule a 30 minute call tomorrow?",
        }

    messages.list.return_value.execute.return_value = {
        "messages": [{"id": "first", "threadId": "demo-thread"}]
    }
    messages.get.return_value.execute.return_value = message("first", "")
    assert service._sync_gmail_inbox("assistant_gmail")["imported_messages"] == 0
    assert not service.db.is_ignored_inbox_thread("assistant_gmail", "demo-thread")
    messages.list.return_value.execute.return_value = {
        "messages": [
            {"id": "first", "threadId": "demo-thread"},
            {"id": "second", "threadId": "demo-thread"},
        ]
    }
    messages.get.return_value.execute.return_value = message("second", "assistant@example.com")
    assert service._sync_gmail_inbox("assistant_gmail")["imported_messages"] == 1
    assert ingested[0]["external_message_id"] == "second"
