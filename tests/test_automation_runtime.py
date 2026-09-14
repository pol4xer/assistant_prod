import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import create_app
from app.modules.automation.service import AutomationRuntimeService


def test_automation_runtime_runs_cycle_and_tracks_result():
    calls = []

    assistant = SimpleNamespace(
        settings=SimpleNamespace(
            app_demo_mode=False,
            automation_polling_enabled=True,
            automation_polling_interval_seconds=60,
        ),
        run_automation=lambda: (
            calls.append("run")
            or {
                "ok": True,
                "run_at": "2026-04-11T15:45+03:00",
                "imported_inbox_messages": 2,
                "follow_ups_sent": 1,
                "confirmations_sent": 0,
            }
        ),
        _now=lambda: datetime(2026, 4, 11, 15, 45, tzinfo=ZoneInfo("Europe/Istanbul")),
    )

    runtime = AutomationRuntimeService(assistant, disable_during_tests=False)

    async def exercise():
        await runtime.start()
        await asyncio.sleep(0.05)
        await runtime.stop()

    asyncio.run(exercise())

    snapshot = runtime.snapshot()
    assert calls == ["run"]
    assert snapshot["enabled"] is True
    assert snapshot["run_count"] == 1
    assert snapshot["last_result"]["imported_inbox_messages"] == 2
    assert snapshot["last_error"] is None
    assert "snapshot" not in snapshot["last_result"]


def test_automation_runtime_is_disabled_in_pytest_by_default():
    assistant = SimpleNamespace(
        settings=SimpleNamespace(
            app_demo_mode=False,
            automation_polling_enabled=True,
            automation_polling_interval_seconds=10,
        ),
        run_automation=lambda: {"ok": True, "run_at": "2026-04-11T15:45+03:00"},
        _now=lambda: datetime(2026, 4, 11, 15, 45, tzinfo=ZoneInfo("Europe/Istanbul")),
    )

    runtime = AutomationRuntimeService(assistant)

    assert runtime.enabled is False


def test_delivery_status_notification_is_auto_ignored_by_gmail_transport(tmp_path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    transport = app.state.service.gmail_transport

    message = {
        "payload": {
            "headers": [
                {"name": "From", "value": "Mailer-Daemon <mailer-daemon@example.com>"},
                {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
                {"name": "Auto-Submitted", "value": "auto-generated"},
            ]
        }
    }
    parsed = {
        "sender_email": "mailer-daemon@example.com",
        "subject": "Delivery Status Notification (Failure)",
        "body": "Message blocked",
    }

    reason = transport.should_auto_ignore_message(
        "assistant_gmail",
        message,
        parsed,
        existing_request=None,
    )

    assert reason == "delivery-status-or-bounce"


def test_marketing_message_is_auto_ignored_by_relevance_gate(tmp_path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    transport = app.state.service.gmail_transport

    message = {
        "payload": {
            "headers": [
                {"name": "From", "value": "Calendly <newsletter@example.com>"},
                {"name": "Subject", "value": "Calendly Developer Newsletter: April 2026"},
            ]
        }
    }
    parsed = {
        "sender_email": "newsletter@example.com",
        "subject": "Calendly Developer Newsletter: April 2026",
        "body": "Free trial updates, unsubscribe any time, manage preferences here.",
    }

    reason = transport.should_auto_ignore_message(
        "assistant_gmail",
        message,
        parsed,
        existing_request=None,
    )

    assert reason == "not-scheduling-related"


def test_new_thread_with_scheduling_body_passes_relevance_gate(tmp_path):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    transport = app.state.service.gmail_transport

    message = {
        "payload": {
            "headers": [
                {"name": "From", "value": "Alex <client@example.com>"},
                {"name": "Subject", "value": "House fixing"},
            ]
        }
    }
    parsed = {
        "sender_email": "client@example.com",
        "subject": "House fixing",
        "body": "Hi! Adding the assistant for schedule purposes for next week for 30 mins call.",
        "cc": ["assistant@example.com"],
    }

    reason = transport.should_auto_ignore_message(
        "assistant_gmail",
        message,
        parsed,
        existing_request=None,
    )

    assert reason is None
