from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.models import MeetingMode, RequestStatus, combine_local, isoformat_minute, parse_clock
from app.modules.shared.constants import (
    GOOGLE_WORKSPACE_PROVIDER,
    TEST_CLIENT_EMAIL,
    TEST_CLIENT_GMAIL_PROVIDER,
    TEST_CLIENT_NAME,
)


class DemoScenarioService:
    """Application-layer helpers for repeatable demos and QA scenarios."""

    def __init__(self, assistant: Any):
        self.assistant = assistant

    @property
    def db(self):
        return self.assistant.db

    @property
    def scheduler(self):
        return self.assistant.scheduler

    @property
    def demo_lock(self):
        return self.assistant._demo_reset_lock

    def run_one_click_test_pipeline(self) -> Dict[str, Any]:
        with self.demo_lock:
            workspace = self.assistant.require_workspace()
            now = self.assistant._now()
            target_start = self.one_click_test_start(now, workspace)
            target_end = target_start + timedelta(minutes=30)
            target_prompt = target_start.strftime("%Y-%m-%d at %I:%M %p")
            subject = "One-click test meeting"
            created = self.assistant.create_outgoing_request(
                {
                    "subject": subject,
                    "body": (
                        f"Please schedule a 30 minute online meeting on {target_prompt}. "
                        "This is a one-click end-to-end test."
                    ),
                    "participants": [
                        {
                            "email": TEST_CLIENT_EMAIL,
                            "display_name": TEST_CLIENT_NAME,
                            "required": True,
                        }
                    ],
                    "mode": MeetingMode.ONLINE.value,
                }
            )
            target_option_index = self.find_one_click_test_option_index(
                created["options"],
                target_start=target_start,
                target_end=target_end,
            )
            if target_option_index is None:
                raise ValueError(
                    "Unable to prepare the sample meeting for the next available day at 12:30. "
                    "The slot was not proposed. Check availability, busy slots, and calendar synchronization."
                )
            confirmed = self.assistant.ingest_email(
                {
                    "sender_email": TEST_CLIENT_EMAIL,
                    "sender_name": TEST_CLIENT_NAME,
                    "to": [workspace["owner_email"]],
                    "cc": [workspace["assistant_email"]],
                    "subject": f"Re: {subject}",
                    "body": f"Option {target_option_index} works for me.",
                    "thread_key": created["thread_key"],
                    "sent_at": now + timedelta(minutes=2),
                }
            )
            meeting = confirmed.get("meeting") or {}
            return {
                "ok": True,
                "request_id": confirmed["id"],
                "thread_key": confirmed["thread_key"],
                "scheduled_display": confirmed.get("scheduled_display"),
                "calendar_event_created": bool(meeting.get("external_event_id")),
                "detail": confirmed,
            }

    def run_cc_assistant_test_pipeline(self) -> Dict[str, Any]:
        with self.demo_lock:
            workspace = self.assistant.require_workspace()
            now = self.assistant._now()
            target_start = self.one_click_test_start(now, workspace)
            target_end = target_start + timedelta(minutes=30)
            target_prompt = target_start.strftime("%Y-%m-%d at %I:%M %p")
            subject = "Client email with assistant in CC"
            created = self.assistant.ingest_email(
                {
                    "sender_email": TEST_CLIENT_EMAIL,
                    "sender_name": TEST_CLIENT_NAME,
                    "to": [workspace["owner_email"]],
                    "cc": [workspace["assistant_email"]],
                    "subject": subject,
                    "body": (
                        f"Hi {workspace['owner_name']}, please schedule a 30 minute online meeting on {target_prompt}. "
                        f"I am cc'ing {workspace['assistant_email']} so the assistant can take it from here."
                    ),
                    "sent_at": now,
                }
            )
            target_option_index = self.find_one_click_test_option_index(
                created["options"],
                target_start=target_start,
                target_end=target_end,
            )
            if target_option_index is None:
                raise ValueError(
                    "Unable to prepare the CC scenario for the next available day at 12:30. "
                    "The slot was not proposed. Check availability, busy slots, and calendar synchronization."
                )
            confirmed = self.assistant.ingest_email(
                {
                    "sender_email": TEST_CLIENT_EMAIL,
                    "sender_name": TEST_CLIENT_NAME,
                    "to": [workspace["owner_email"]],
                    "cc": [workspace["assistant_email"]],
                    "subject": f"Re: {subject}",
                    "body": f"Option {target_option_index} works for me.",
                    "thread_key": created["thread_key"],
                    "sent_at": now + timedelta(minutes=2),
                }
            )
            meeting = confirmed.get("meeting") or {}
            return {
                "ok": True,
                "request_id": confirmed["id"],
                "thread_key": confirmed["thread_key"],
                "scheduled_display": confirmed.get("scheduled_display"),
                "calendar_event_created": bool(meeting.get("external_event_id")),
                "detail": confirmed,
            }

    def run_live_gmail_cc_demo(self) -> Dict[str, Any]:
        with self.demo_lock:
            workspace = self.assistant.require_workspace()
            now = self.assistant._now()
            target_start = self.one_click_test_start(now, workspace)
            target_end = target_start + timedelta(minutes=30)
            target_prompt = target_start.strftime("%Y-%m-%d at %I:%M %p")
            subject = f"Live Gmail CC demo {now.strftime('%Y-%m-%d %H:%M:%S')}"

            initial_body = (
                f"Hi {TEST_CLIENT_NAME}, would love to chat. "
                f"Adding {workspace['assistant_name']} to share some options for a 30 minute video call on {target_prompt}."
            )
            owner_send = self.assistant._send_gmail_message(
                provider=GOOGLE_WORKSPACE_PROVIDER,
                sender_email=workspace["owner_email"],
                recipients=[TEST_CLIENT_EMAIL],
                cc=[workspace["assistant_email"]],
                subject=subject,
                body=initial_body,
            )
            created = self.assistant.ingest_email(
                {
                    "sender_email": workspace["owner_email"],
                    "sender_name": workspace["owner_name"],
                    "to": [TEST_CLIENT_EMAIL],
                    "cc": [workspace["assistant_email"]],
                    "subject": subject,
                    "body": initial_body,
                    "sent_at": now,
                    **owner_send,
                }
            )
            target_option_index = self.find_one_click_test_option_index(
                created["options"],
                target_start=target_start,
                target_end=target_end,
            )
            if target_option_index is None:
                raise ValueError(
                    "Unable to prepare the live Gmail scenario for the next available day at 12:30. "
                    "The slot was not proposed. Check availability, busy slots, and calendar synchronization."
                )

            acceptance_body = "The earliest time works for me!"
            client_accept_send = self.assistant._send_gmail_message(
                provider=TEST_CLIENT_GMAIL_PROVIDER,
                sender_email=TEST_CLIENT_EMAIL,
                recipients=[workspace["assistant_email"]],
                cc=[workspace["owner_email"]],
                subject=f"Re: {subject}",
                body=acceptance_body,
                reply_context=self.assistant._gmail_reply_context(
                    created["id"], TEST_CLIENT_GMAIL_PROVIDER
                ),
            )
            confirmed = self.assistant.ingest_email(
                {
                    "sender_email": TEST_CLIENT_EMAIL,
                    "sender_name": TEST_CLIENT_NAME,
                    "to": [workspace["assistant_email"]],
                    "cc": [workspace["owner_email"]],
                    "subject": f"Re: {subject}",
                    "body": acceptance_body,
                    "thread_key": created["thread_key"],
                    "sent_at": now + timedelta(minutes=2),
                    **client_accept_send,
                }
            )

            reschedule_body = (
                "So sorry, could we actually reschedule this to next month? "
                "Something unmovable just came up for me."
            )
            client_reschedule_send = self.assistant._send_gmail_message(
                provider=TEST_CLIENT_GMAIL_PROVIDER,
                sender_email=TEST_CLIENT_EMAIL,
                recipients=[workspace["assistant_email"]],
                cc=[workspace["owner_email"]],
                subject=f"Re: {subject}",
                body=reschedule_body,
                reply_context=self.assistant._gmail_reply_context(
                    created["id"], TEST_CLIENT_GMAIL_PROVIDER
                ),
            )
            replanned = self.assistant.ingest_email(
                {
                    "sender_email": TEST_CLIENT_EMAIL,
                    "sender_name": TEST_CLIENT_NAME,
                    "to": [workspace["assistant_email"]],
                    "cc": [workspace["owner_email"]],
                    "subject": f"Re: {subject}",
                    "body": reschedule_body,
                    "thread_key": created["thread_key"],
                    "sent_at": now + timedelta(minutes=4),
                    **client_reschedule_send,
                }
            )

            cancel_body = (
                "Actually we probably don't need to have this meeting anymore. Please cancel it."
            )
            client_cancel_send = self.assistant._send_gmail_message(
                provider=TEST_CLIENT_GMAIL_PROVIDER,
                sender_email=TEST_CLIENT_EMAIL,
                recipients=[workspace["assistant_email"]],
                cc=[workspace["owner_email"]],
                subject=f"Re: {subject}",
                body=cancel_body,
                reply_context=self.assistant._gmail_reply_context(
                    created["id"], TEST_CLIENT_GMAIL_PROVIDER
                ),
            )
            cancelled = self.assistant.ingest_email(
                {
                    "sender_email": TEST_CLIENT_EMAIL,
                    "sender_name": TEST_CLIENT_NAME,
                    "to": [workspace["assistant_email"]],
                    "cc": [workspace["owner_email"]],
                    "subject": f"Re: {subject}",
                    "body": cancel_body,
                    "thread_key": created["thread_key"],
                    "sent_at": now + timedelta(minutes=6),
                    **client_cancel_send,
                }
            )
            return {
                "ok": True,
                "request_id": cancelled["id"],
                "thread_key": cancelled["thread_key"],
                "scheduled_display": confirmed.get("scheduled_display"),
                "calendar_event_created": bool(
                    (confirmed.get("meeting") or {}).get("external_event_id")
                ),
                "cancelled": cancelled["status"] == RequestStatus.CANCELLED.value,
                "detail": cancelled,
                "replanned_detail": replanned,
                "confirmed_detail": confirmed,
            }

    def one_click_test_start(self, now: datetime, workspace: Dict[str, Any]) -> datetime:
        timezone_name = str(workspace["timezone"])
        local_now = (
            now.astimezone(ZoneInfo(timezone_name))
            if now.tzinfo
            else now.replace(tzinfo=ZoneInfo(timezone_name))
        )
        availability_rules = self.db.rows_to_dicts(self.db.list_availability_rules())
        busy_slots = self.db.rows_to_dicts(self.db.list_busy_slots())
        for day_offset in range(1, 31):
            candidate_day = local_now.date() + timedelta(days=day_offset)
            candidate_start = combine_local(candidate_day, parse_clock("12:30"), timezone_name)
            candidate_end = candidate_start + timedelta(minutes=30)
            if self.one_click_slot_is_available(
                workspace=workspace,
                availability_rules=availability_rules,
                busy_slots=busy_slots,
                start=candidate_start,
                end=candidate_end,
            ):
                return candidate_start
        raise ValueError(
            "No available day at 12:30 was found. "
            "Check the working days, availability window, and calendar busy slots."
        )

    def one_click_slot_is_available(
        self,
        *,
        workspace: Dict[str, Any],
        availability_rules: List[Dict[str, Any]],
        busy_slots: List[Dict[str, Any]],
        start: datetime,
        end: datetime,
    ) -> bool:
        buffer_minutes = int(workspace["meeting_buffer_minutes"])
        padded_start = start - timedelta(minutes=buffer_minutes)
        padded_end = end + timedelta(minutes=buffer_minutes)
        if not self.scheduler._within_availability(
            availability_rules, workspace, padded_start, padded_end
        ):
            return False
        if self.scheduler._overlaps_busy_slot(busy_slots, padded_start, padded_end):
            return False
        return True

    def find_one_click_test_option_index(
        self,
        options: List[Dict[str, Any]],
        target_start: datetime,
        target_end: datetime,
    ) -> Optional[int]:
        target_start_iso = isoformat_minute(target_start)
        target_end_iso = isoformat_minute(target_end)
        for index, option in enumerate(options, start=1):
            if (
                option.get("starts_at") == target_start_iso
                and option.get("ends_at") == target_end_iso
            ):
                return index
        return None
