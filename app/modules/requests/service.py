from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.models import (
    AnalysisResult,
    MeetingMode,
    MeetingPriority,
    MessageIntent,
    RequestStatus,
    display_datetime,
    isoformat_minute,
    parse_iso_datetime,
)
from app.modules.shared.constants import ASSISTANT_GMAIL_PROVIDER, GOOGLE_WORKSPACE_PROVIDER
from app.services.analysis import normalize_subject
from app.services.scheduler import option_to_dict

logger = logging.getLogger(__name__)


class RequestLifecycleService:
    """Application use-cases for request ingestion, negotiation, confirmation, and follow-ups."""

    def __init__(self, assistant: Any):
        self.assistant = assistant

    @property
    def db(self):
        return self.assistant.db

    @property
    def scheduler(self):
        return self.assistant.scheduler

    @property
    def state_lock(self):
        return self.assistant._state_lock

    def request_status(self, request_row: Dict[str, Any]) -> RequestStatus:
        try:
            return RequestStatus(str(request_row.get("status") or RequestStatus.DRAFT.value))
        except ValueError:
            return RequestStatus.DRAFT

    def policy_action_for_inbound(
        self,
        request_row: Dict[str, Any],
        analysis: AnalysisResult,
        selected_option: Optional[Dict[str, Any]],
    ) -> str:
        status = self.request_status(request_row)
        has_new_window = bool(analysis.availability_hints)

        if status == RequestStatus.CANCELLED:
            if analysis.intent == MessageIntent.CANCEL:
                return "already_cancelled"
            if (
                analysis.intent in (MessageIntent.PROPOSE_TIME, MessageIntent.RESCHEDULE)
                or has_new_window
            ):
                return "reopen"
            if analysis.intent == MessageIntent.ACCEPT or selected_option:
                return "cancelled_needs_new_window"
            return "noop_cancelled"

        if status == RequestStatus.CONFIRMED:
            if analysis.intent == MessageIntent.CANCEL:
                return "cancel"
            if (
                analysis.intent in (MessageIntent.PROPOSE_TIME, MessageIntent.RESCHEDULE)
                or has_new_window
            ):
                return "replan"
            if analysis.intent == MessageIntent.ACCEPT or selected_option:
                return "already_confirmed"
            return "noop_confirmed"

        if analysis.intent == MessageIntent.CANCEL:
            return "cancel"
        if analysis.intent in (MessageIntent.PROPOSE_TIME, MessageIntent.RESCHEDULE):
            return "replan"
        if analysis.intent == MessageIntent.NEW_REQUEST and has_new_window:
            return "replan"
        if analysis.intent == MessageIntent.ACCEPT or selected_option:
            return "accept"
        return "clarify"

    def create_outgoing_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.assistant.require_workspace()
        now = self.assistant._now()
        analysis = self.assistant._analyze_message(
            subject=payload["subject"],
            body=payload["body"],
            reference_at=now,
            timezone_name=workspace["timezone"],
            default_duration_minutes=int(workspace["default_duration_minutes"]),
            forced_intent=MessageIntent.NEW_REQUEST,
        )
        if payload.get("priority"):
            analysis.priority = MeetingPriority(payload["priority"])
        if payload.get("requested_location"):
            analysis.requested_location = payload["requested_location"]
        requested_mode = MeetingMode(payload["mode"]) if payload.get("mode") else None

        self.assistant._refresh_google_calendar_context(force=False, reference_time=now)

        participant_emails = [participant["email"] for participant in payload["participants"]]
        with self.state_lock:
            availability_rules = self.db.rows_to_dicts(self.db.list_availability_rules())
            busy_slots = self.db.rows_to_dicts(self.db.list_busy_slots())
            locations = self.db.rows_to_dicts(self.db.list_locations())
            resolved_mode = self.scheduler.resolve_mode(analysis, locations, requested_mode).value
            thread_key = (
                payload["thread_key"]
                if payload.get("thread_key")
                else self.assistant._new_thread_key(payload["subject"])
            )
            request_id = self.create_request_record(
                analysis=analysis,
                direction="outgoing",
                source="dashboard",
                subject=payload["subject"],
                organizer_name=workspace["owner_name"],
                organizer_email=workspace["owner_email"],
                requested_location=payload.get("requested_location"),
                thread_key=thread_key,
                created_at=now,
            )
            for participant in payload["participants"]:
                email = participant["email"]
                contact_id = self.db.upsert_contact(
                    email=email,
                    display_name=participant.get("display_name"),
                    now=isoformat_minute(now),
                )
                self.db.add_participant(
                    request_id=request_id,
                    email=email,
                    display_name=participant.get("display_name"),
                    contact_id=contact_id,
                    required=bool(participant.get("required", True)),
                )

            options = self.scheduler.propose_options(
                workspace=workspace,
                availability_rules=availability_rules,
                busy_slots=busy_slots,
                locations=locations,
                analysis=analysis,
                now=now,
                requested_mode=requested_mode,
            )
            self.persist_options(request_id, options, now)

            request_snapshot = self.db.row_to_dict(self.db.get_request(request_id))
            option_rows = self.db.rows_to_dicts(self.db.list_options(request_id))
            if options:
                body = self.assistant._compose_negotiation_email(
                    request=request_snapshot,
                    options=option_rows,
                    workspace=workspace,
                    revision=False,
                )
                next_follow_up = isoformat_minute(
                    self.scheduler.next_follow_up_at(analysis.priority, now)
                )
                status = RequestStatus.NEGOTIATING.value
            else:
                body = self.assistant._compose_needs_input_email(
                    request=request_snapshot,
                    workspace=workspace,
                )
                next_follow_up = None
                status = RequestStatus.NEEDS_INPUT.value

            self.db.update_request(
                request_id,
                {
                    "status": status,
                    "last_outbound_at": isoformat_minute(now),
                    "next_follow_up_at": next_follow_up,
                    "updated_at": isoformat_minute(now),
                    "mode": resolved_mode,
                },
            )

        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=participant_emails,
            cc=[workspace["owner_email"]],
            subject=payload["subject"],
            body=body,
            sent_at=isoformat_minute(now),
            intent=MessageIntent.NEW_REQUEST.value,
        )
        return self.assistant.get_request_detail(request_id)

    def ingest_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        email_metadata = self.assistant._email_metadata_from_payload(payload)
        external_message_id = str(email_metadata.get("external_message_id") or "").strip()
        if external_message_id:
            existing_email = self.db.get_email_by_external_message_id(external_message_id)
            if existing_email is not None:
                logger.info(
                    "Idempotent email ingest hit external_message_id=%s request_id=%s",
                    external_message_id,
                    existing_email["request_id"],
                )
                return self.assistant.get_request_detail(int(existing_email["request_id"]))

        workspace = self.assistant.require_workspace()
        sent_at = payload.get("sent_at") or self.assistant._now()
        if isinstance(sent_at, str):
            sent_at = parse_iso_datetime(sent_at)
        explicit_thread_key = str(payload.get("thread_key") or "").strip()
        if explicit_thread_key:
            request_row = self.db.get_request_by_thread(explicit_thread_key)
            if request_row is None:
                return self.create_incoming_request(
                    payload, workspace, sent_at, explicit_thread_key
                )
            return self.handle_existing_thread(dict(request_row), payload, workspace, sent_at)

        normalized_subject_thread = self.assistant._thread_key(payload["subject"])
        if self.assistant._looks_like_reply_subject(payload["subject"]):
            request_row = self.db.get_request_by_thread(normalized_subject_thread)
            if request_row is not None:
                return self.handle_existing_thread(dict(request_row), payload, workspace, sent_at)

        return self.create_incoming_request(
            payload,
            workspace,
            sent_at,
            self.assistant._new_thread_key(payload["subject"]),
        )

    def run_automation(self, current_time: Optional[datetime] = None) -> Dict[str, Any]:
        now = current_time or self.assistant._now()
        if isinstance(now, str):
            now = parse_iso_datetime(now)

        acquired = self.assistant._automation_lock.acquire(blocking=False)
        if not acquired:
            logger.info("Automation run skipped because another cycle is already running")
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_running",
                "run_at": isoformat_minute(now),
                "imported_inbox_messages": 0,
                "follow_ups_sent": 0,
                "confirmations_sent": 0,
                "repaired_events": 0,
                "snapshot": self.assistant.dashboard_snapshot(),
            }

        try:
            imported_inbox_messages = 0
            assistant_bundle = self.assistant._load_provider_bundle(ASSISTANT_GMAIL_PROVIDER)
            if assistant_bundle.get("tokens"):
                try:
                    inbox_sync = self.assistant._sync_gmail_inbox(ASSISTANT_GMAIL_PROVIDER)
                    imported_inbox_messages = int(inbox_sync.get("imported_messages") or 0)
                except Exception as exc:
                    warning = self.assistant._describe_google_api_issue("Assistant Gmail sync", exc)
                    logger.exception("Assistant Gmail automation sync failed reason=%s", warning)
                    self.assistant._append_provider_warning(ASSISTANT_GMAIL_PROVIDER, warning)

            follow_up_count = 0
            confirmation_count = 0
            repaired_events = 0

            for request_summary in self.follow_ups_due(now):
                self.send_follow_up(request_summary["id"], now)
                follow_up_count += 1

            for request_summary in self.confirmations_due(now):
                self.send_confirmation_reminder(request_summary["id"], now)
                confirmation_count += 1

            google_bundle = self.assistant._load_provider_bundle(GOOGLE_WORKSPACE_PROVIDER)
            if google_bundle.get("tokens") and google_bundle.get("selected_calendar_id"):
                repair_result = self.assistant._repair_missing_google_calendar_events(
                    reference_time=now
                )
                repaired_events = int(repair_result.get("created") or 0)

            return {
                "ok": True,
                "run_at": isoformat_minute(now),
                "imported_inbox_messages": imported_inbox_messages,
                "follow_ups_sent": follow_up_count,
                "confirmations_sent": confirmation_count,
                "repaired_events": repaired_events,
                "snapshot": self.assistant.dashboard_snapshot(),
            }
        finally:
            self.assistant._automation_lock.release()

    def reset_demo_data(self) -> Dict[str, Any]:
        with self.assistant._demo_reset_lock:
            self.db.reset()
            self.assistant._bootstrap_defaults()
            workspace = self.assistant.require_workspace()
            now = self.assistant._now()

            primary_calendar = self.db.rows_to_dicts(self.db.list_calendars())[0]
            tomorrow = now + timedelta(days=1)
            self.db.add_busy_slot(
                calendar_id=int(primary_calendar["id"]),
                title="Leadership sync",
                starts_at=isoformat_minute(tomorrow.replace(hour=11, minute=0)),
                ends_at=isoformat_minute(tomorrow.replace(hour=12, minute=0)),
                source="seed",
            )
            self.db.add_busy_slot(
                calendar_id=int(primary_calendar["id"]),
                title="Product review",
                starts_at=isoformat_minute(
                    (tomorrow + timedelta(days=1)).replace(hour=15, minute=0)
                ),
                ends_at=isoformat_minute((tomorrow + timedelta(days=1)).replace(hour=16, minute=0)),
                source="seed",
            )

            outgoing = self.create_outgoing_request(
                {
                    "subject": "Q2 partnership planning",
                    "body": "Could you help me schedule a 45 minute online meeting next Tuesday afternoon to review the partnership launch plan?",
                    "participants": [
                        {
                            "email": "sam@example.com",
                            "display_name": "Sam Partner",
                            "required": True,
                        }
                    ],
                    "mode": MeetingMode.ONLINE.value,
                }
            )
            self.ingest_email(
                {
                    "sender_email": "sam@example.com",
                    "sender_name": "Sam Partner",
                    "to": [workspace["assistant_email"]],
                    "cc": [],
                    "subject": "Re: Q2 partnership planning",
                    "body": "Option 1 works for me. Confirmed.",
                    "thread_key": outgoing["thread_key"],
                    "sent_at": now + timedelta(hours=1),
                }
            )

            self.ingest_email(
                {
                    "sender_email": "investor@example.com",
                    "sender_name": "Nina Investor",
                    "to": [workspace["owner_email"]],
                    "cc": [workspace["assistant_email"]],
                    "subject": "Board prep review",
                    "body": "Can we meet tomorrow afternoon in person to review the investor deck? This is urgent.",
                    "sent_at": now,
                }
            )
            return self.assistant.dashboard_snapshot()

    def delete_request(self, request_id: int) -> Dict[str, Any]:
        with self.state_lock:
            request_row = self.db.row_to_dict(self.db.get_request(request_id))
            if request_row is None:
                raise ValueError("Request %s not found" % request_id)
            emails = self.db.rows_to_dicts(self.db.list_emails(request_id))
            meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))
            had_calendar_event = bool(
                meeting
                and str(meeting.get("external_event_id") or "").strip()
                and str(meeting.get("external_calendar_id") or "").strip()
            )

        calendar_event_removed = self.assistant._remove_google_calendar_event(meeting)

        with self.state_lock:
            request_row = self.db.row_to_dict(self.db.get_request(request_id))
            if request_row is None:
                raise ValueError("Request %s not found" % request_id)
            created_at = isoformat_minute(self.assistant._now())
            for email in emails:
                provider = str(email.get("provider") or "").strip()
                if not provider or provider == "local":
                    continue
                self.db.add_ignored_inbox_message(
                    provider=provider,
                    external_message_id=str(email.get("external_message_id") or "").strip(),
                    created_at=created_at,
                    reason="deleted-from-dashboard",
                )
                self.db.add_ignored_inbox_thread(
                    provider=provider,
                    external_thread_id=str(email.get("external_thread_id") or "").strip(),
                    created_at=created_at,
                    reason="deleted-from-dashboard",
                )
            self.db.delete_request(request_id)

        logger.info(
            "Request deleted request_id=%s subject=%s had_calendar_event=%s calendar_event_removed=%s",
            request_id,
            request_row.get("subject"),
            had_calendar_event,
            calendar_event_removed,
        )
        return {
            "ok": True,
            "deleted_request_id": request_id,
            "deleted_subject": request_row.get("subject"),
            "had_calendar_event": had_calendar_event,
            "calendar_event_removed": calendar_event_removed,
        }

    def create_incoming_request(
        self,
        payload: Dict[str, Any],
        workspace: Dict[str, Any],
        sent_at: datetime,
        thread_key: str,
    ) -> Dict[str, Any]:
        email_metadata = self.assistant._email_metadata_from_payload(payload)
        analysis = self.assistant._analyze_message(
            subject=payload["subject"],
            body=payload["body"],
            reference_at=sent_at,
            timezone_name=workspace["timezone"],
            default_duration_minutes=int(workspace["default_duration_minutes"]),
            forced_intent=MessageIntent.NEW_REQUEST,
        )

        participant_emails = self.derive_external_participants(payload, workspace)

        self.assistant._refresh_google_calendar_context(force=False, reference_time=sent_at)

        with self.state_lock:
            external_message_id = str(email_metadata.get("external_message_id") or "").strip()
            if external_message_id:
                existing_email = self.db.get_email_by_external_message_id(external_message_id)
                if existing_email is not None:
                    return self.assistant.get_request_detail(int(existing_email["request_id"]))
            availability_rules = self.db.rows_to_dicts(self.db.list_availability_rules())
            busy_slots = self.db.rows_to_dicts(self.db.list_busy_slots())
            locations = self.db.rows_to_dicts(self.db.list_locations())
            resolved_mode = self.scheduler.resolve_mode(analysis, locations, None).value
            request_id = self.create_request_record(
                analysis=analysis,
                direction="incoming",
                source="email",
                subject=payload["subject"],
                organizer_name=payload.get("sender_name"),
                organizer_email=payload["sender_email"],
                requested_location=analysis.requested_location,
                thread_key=thread_key,
                created_at=sent_at,
            )
            self.db.add_email(
                request_id=request_id,
                direction="inbound",
                sender_email=payload["sender_email"],
                recipients=payload["to"],
                cc=payload.get("cc", []),
                subject=payload["subject"],
                body=payload["body"],
                sent_at=isoformat_minute(sent_at),
                intent=analysis.intent.value,
                extracted_windows=self.assistant._serialize_hints(analysis.availability_hints),
                provider=email_metadata["provider"] or "local",
                external_message_id=email_metadata["external_message_id"],
                external_thread_id=email_metadata["external_thread_id"],
                internet_message_id=email_metadata["internet_message_id"],
            )
            for email in participant_emails:
                contact_id = self.db.upsert_contact(
                    email=email, display_name=None, now=isoformat_minute(sent_at)
                )
                self.db.add_participant(request_id=request_id, email=email, contact_id=contact_id)

            options = self.scheduler.propose_options(
                workspace=workspace,
                availability_rules=availability_rules,
                busy_slots=busy_slots,
                locations=locations,
                analysis=analysis,
                now=sent_at,
            )
            self.persist_options(request_id, options, sent_at)

            request_snapshot = self.db.row_to_dict(self.db.get_request(request_id))
            option_rows = self.db.rows_to_dicts(self.db.list_options(request_id))
            if options:
                outbound_body = self.assistant._compose_negotiation_email(
                    request=request_snapshot,
                    options=option_rows,
                    workspace=workspace,
                    revision=False,
                )
                outbound_intent = MessageIntent.NEW_REQUEST.value
                status = RequestStatus.NEGOTIATING.value
                next_follow_up = isoformat_minute(
                    self.scheduler.next_follow_up_at(analysis.priority, sent_at)
                )
            else:
                outbound_body = self.assistant._compose_needs_input_email(
                    request=request_snapshot,
                    workspace=workspace,
                )
                outbound_intent = MessageIntent.GENERAL.value
                status = RequestStatus.NEEDS_INPUT.value
                next_follow_up = None

            self.db.update_request(
                request_id,
                {
                    "status": status,
                    "last_inbound_at": isoformat_minute(sent_at),
                    "last_outbound_at": isoformat_minute(sent_at),
                    "next_follow_up_at": next_follow_up,
                    "updated_at": isoformat_minute(sent_at),
                    "mode": resolved_mode,
                },
            )

        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=participant_emails or [payload["sender_email"]],
            cc=[workspace["owner_email"]],
            subject=payload["subject"],
            body=outbound_body,
            sent_at=isoformat_minute(sent_at),
            intent=outbound_intent,
        )
        return self.assistant.get_request_detail(request_id)

    def handle_existing_thread(
        self,
        request_row: Dict[str, Any],
        payload: Dict[str, Any],
        workspace: Dict[str, Any],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        request_id = int(request_row["id"])
        email_metadata = self.assistant._email_metadata_from_payload(payload)
        analysis = self.assistant._analyze_message(
            subject=payload["subject"],
            body=payload["body"],
            reference_at=sent_at,
            timezone_name=workspace["timezone"],
            default_duration_minutes=int(request_row["duration_minutes"]),
            forced_intent=None,
        )
        with self.state_lock:
            current_request = self.db.row_to_dict(self.db.get_request(request_id))
            if current_request is None:
                raise ValueError("Request %s not found" % request_id)
            external_message_id = str(email_metadata.get("external_message_id") or "").strip()
            if external_message_id:
                existing_email = self.db.get_email_by_external_message_id(external_message_id)
                if existing_email is not None:
                    return self.assistant.get_request_detail(int(existing_email["request_id"]))
            self.db.add_email(
                request_id=request_id,
                direction="inbound",
                sender_email=payload["sender_email"],
                recipients=payload["to"],
                cc=payload.get("cc", []),
                subject=payload["subject"],
                body=payload["body"],
                sent_at=isoformat_minute(sent_at),
                intent=analysis.intent.value,
                extracted_windows=self.assistant._serialize_hints(analysis.availability_hints),
                provider=email_metadata["provider"] or "local",
                external_message_id=email_metadata["external_message_id"],
                external_thread_id=email_metadata["external_thread_id"],
                internet_message_id=email_metadata["internet_message_id"],
            )
            self.db.update_request(
                request_id,
                {
                    "last_inbound_at": isoformat_minute(sent_at),
                    "updated_at": isoformat_minute(sent_at),
                },
            )
            options = self.db.rows_to_dicts(self.db.list_options(request_id))
            selected_option = self.scheduler.match_existing_option(
                payload["body"], options, analysis
            )
            action = self.policy_action_for_inbound(current_request, analysis, selected_option)

        logger.info(
            "Inbound policy decision request_id=%s status=%s intent=%s action=%s sender=%s",
            request_id,
            current_request.get("status"),
            analysis.intent.value,
            action,
            payload.get("sender_email"),
        )

        if action == "cancel":
            return self.cancel_request(current_request, workspace, sent_at)

        if action in {"replan", "reopen"}:
            return self.replan_request(current_request, workspace, analysis, sent_at)

        if action == "accept":
            return self.register_acceptance(
                request_row=current_request,
                workspace=workspace,
                analysis=analysis,
                sender_email=payload["sender_email"],
                selected_option=selected_option,
                sent_at=sent_at,
            )

        if action == "already_confirmed":
            with self.state_lock:
                meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))
            body = self.assistant._compose_already_confirmed_email(
                current_request, meeting, workspace
            )
            self.assistant._record_outbound_email(
                request_id=request_id,
                sender_email=workspace["assistant_email"],
                recipients=[payload["sender_email"]],
                cc=[workspace["owner_email"]],
                subject=current_request["subject"],
                body=body,
                sent_at=isoformat_minute(sent_at),
                intent=MessageIntent.CONFIRMATION.value,
            )
            with self.state_lock:
                if self.db.get_request(request_id) is not None:
                    self.db.update_request(
                        request_id,
                        {
                            "last_outbound_at": isoformat_minute(sent_at),
                            "updated_at": isoformat_minute(sent_at),
                        },
                    )
            return self.assistant.get_request_detail(request_id)

        if action in {"already_cancelled", "cancelled_needs_new_window"}:
            body = self.assistant._compose_cancelled_thread_email(current_request, workspace)
            self.assistant._record_outbound_email(
                request_id=request_id,
                sender_email=workspace["assistant_email"],
                recipients=[payload["sender_email"]],
                cc=[workspace["owner_email"]],
                subject=current_request["subject"],
                body=body,
                sent_at=isoformat_minute(sent_at),
                intent=MessageIntent.GENERAL.value,
            )
            with self.state_lock:
                if self.db.get_request(request_id) is not None:
                    self.db.update_request(
                        request_id,
                        {
                            "last_outbound_at": isoformat_minute(sent_at),
                            "updated_at": isoformat_minute(sent_at),
                        },
                    )
            return self.assistant.get_request_detail(request_id)

        if action in {"noop_confirmed", "noop_cancelled"}:
            return self.assistant.get_request_detail(request_id)

        clarification_body = self.assistant._compose_clarification_email(
            current_request, options, workspace
        )
        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=[payload["sender_email"]],
            cc=[workspace["owner_email"]],
            subject=current_request["subject"],
            body=clarification_body,
            sent_at=isoformat_minute(sent_at),
            intent=MessageIntent.GENERAL.value,
        )
        with self.state_lock:
            if self.db.get_request(request_id) is not None:
                self.db.update_request(
                    request_id,
                    {
                        "last_outbound_at": isoformat_minute(sent_at),
                        "updated_at": isoformat_minute(sent_at),
                    },
                )
        return self.assistant.get_request_detail(request_id)

    def register_acceptance(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        analysis: AnalysisResult,
        sender_email: str,
        selected_option: Optional[Dict[str, Any]],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        request_id = int(request_row["id"])
        should_confirm = False
        with self.state_lock:
            current_request = self.db.row_to_dict(self.db.get_request(request_id))
            if current_request is None:
                raise ValueError("Request %s not found" % request_id)
            options = self.db.rows_to_dicts(self.db.list_options(request_id))
            chosen = selected_option
            if chosen is None and len(options) == 1:
                chosen = options[0]
            if chosen is None:
                clarification = self.assistant._compose_clarification_email(
                    current_request, options, workspace
                )
            else:
                participant_emails = {
                    item["email"]
                    for item in self.db.rows_to_dicts(self.db.list_participants(request_id))
                }
                if sender_email in participant_emails:
                    self.db.update_participant(
                        request_id,
                        sender_email,
                        {
                            "response_status": "accepted",
                            "accepted_option_id": int(chosen["id"]),
                            "notes": analysis.summary,
                            "last_response_at": isoformat_minute(sent_at),
                        },
                    )
                should_confirm = self.all_required_participants_accept(
                    request_id, int(chosen["id"])
                )
                if not should_confirm:
                    waiting_body = self.assistant._compose_waiting_email(
                        current_request, chosen, workspace
                    )
                    next_follow_up = isoformat_minute(
                        self.scheduler.next_follow_up_at(
                            MeetingPriority(current_request["priority"]), sent_at
                        )
                    )

        if chosen is None:
            self.assistant._record_outbound_email(
                request_id=request_id,
                sender_email=workspace["assistant_email"],
                recipients=[sender_email],
                cc=[workspace["owner_email"]],
                subject=current_request["subject"],
                body=clarification,
                sent_at=isoformat_minute(sent_at),
                intent=MessageIntent.GENERAL.value,
            )
            with self.state_lock:
                if self.db.get_request(request_id) is not None:
                    self.db.update_request(
                        request_id,
                        {
                            "last_outbound_at": isoformat_minute(sent_at),
                            "updated_at": isoformat_minute(sent_at),
                        },
                    )
            return self.assistant.get_request_detail(request_id)

        if should_confirm:
            return self.confirm_request(current_request, workspace, chosen, sent_at)

        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=[sender_email],
            cc=[workspace["owner_email"]],
            subject=current_request["subject"],
            body=waiting_body,
            sent_at=isoformat_minute(sent_at),
            intent=MessageIntent.ACCEPT.value,
        )
        with self.state_lock:
            if self.db.get_request(request_id) is not None:
                self.db.update_request(
                    request_id,
                    {
                        "last_outbound_at": isoformat_minute(sent_at),
                        "next_follow_up_at": next_follow_up,
                        "updated_at": isoformat_minute(sent_at),
                    },
                )
        return self.assistant.get_request_detail(request_id)

    def confirm_request(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        option_row: Dict[str, Any],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        request_id = int(request_row["id"])
        option_id = int(option_row["id"])
        with self.state_lock:
            current_request = self.db.row_to_dict(self.db.get_request(request_id))
            if current_request is None:
                raise ValueError("Request %s not found" % request_id)
            existing_meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))
            if (
                self.request_status(current_request) == RequestStatus.CONFIRMED
                and existing_meeting is not None
            ):
                logger.info(
                    "Confirm request is already finalized request_id=%s option_id=%s",
                    request_id,
                    option_id,
                )
                return self.assistant.get_request_detail(request_id)
            self.db.set_selected_option(request_id, option_id)
            participant_emails = [
                item["email"]
                for item in self.db.rows_to_dicts(self.db.list_participants(request_id))
            ]

        meeting_link = option_row.get("meeting_link")
        if option_row["mode"] == MeetingMode.ONLINE.value and not meeting_link:
            meeting_link = self.scheduler.build_meeting_link(
                provider=workspace["online_provider"],
                request_public_id=current_request["public_id"],
            )
        google_event = self.assistant._create_google_calendar_event(
            request_row=current_request,
            workspace=workspace,
            option_row=option_row,
            participant_emails=participant_emails,
            fallback_meeting_link=meeting_link,
        )
        if google_event and google_event.get("meeting_link"):
            meeting_link = google_event["meeting_link"]

        with self.state_lock:
            current_request = self.db.row_to_dict(self.db.get_request(request_id))
            if current_request is None:
                raise ValueError("Request %s not found" % request_id)
            self.db.create_meeting(
                {
                    "request_id": request_id,
                    "title": current_request["subject"],
                    "agenda": current_request.get("agenda"),
                    "starts_at": option_row["starts_at"],
                    "ends_at": option_row["ends_at"],
                    "mode": option_row["mode"],
                    "location_label": option_row.get("location_label"),
                    "location_address": option_row.get("location_address"),
                    "meeting_link": meeting_link,
                    "external_calendar_id": google_event.get("external_calendar_id")
                    if google_event
                    else None,
                    "external_event_id": google_event.get("external_event_id")
                    if google_event
                    else None,
                    "confirmation_status": "pending",
                    "created_at": isoformat_minute(sent_at),
                    "updated_at": isoformat_minute(sent_at),
                }
            )
            self.db.update_request(
                request_id,
                {
                    "status": RequestStatus.CONFIRMED.value,
                    "scheduled_starts_at": option_row["starts_at"],
                    "scheduled_ends_at": option_row["ends_at"],
                    "location_label": option_row.get("location_label"),
                    "location_address": option_row.get("location_address"),
                    "meeting_link": meeting_link,
                    "next_follow_up_at": None,
                    "confirmation_due_at": isoformat_minute(
                        self.scheduler.confirmation_due_at(
                            parse_iso_datetime(str(option_row["starts_at"])),
                            int(workspace["confirmation_lead_hours"]),
                        )
                    ),
                    "updated_at": isoformat_minute(sent_at),
                    "last_outbound_at": isoformat_minute(sent_at),
                },
            )
            meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))
            confirmation_body = self.assistant._compose_confirmation_email(
                request=self.db.row_to_dict(self.db.get_request(request_id)),
                meeting=meeting,
                workspace=workspace,
            )

        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=participant_emails + [workspace["owner_email"]],
            cc=[],
            subject=current_request["subject"],
            body=confirmation_body,
            sent_at=isoformat_minute(sent_at),
            intent=MessageIntent.CONFIRMATION.value,
        )
        return self.assistant.get_request_detail(request_id)

    def replan_request(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        analysis: AnalysisResult,
        sent_at: datetime,
    ) -> Dict[str, Any]:
        request_id = int(request_row["id"])
        with self.state_lock:
            existing_meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))
        self.assistant._remove_google_calendar_event(existing_meeting)
        self.assistant._refresh_google_calendar_context(force=False, reference_time=sent_at)

        with self.state_lock:
            current_request = self.db.row_to_dict(self.db.get_request(request_id))
            if current_request is None:
                raise ValueError("Request %s not found" % request_id)
            self.db.delete_meeting(request_id)
            self.db.clear_participant_acceptances(request_id)
            availability_rules = self.db.rows_to_dicts(self.db.list_availability_rules())
            busy_slots = self.db.rows_to_dicts(self.db.list_busy_slots())
            locations = self.db.rows_to_dicts(self.db.list_locations())
            options = self.scheduler.propose_options(
                workspace=workspace,
                availability_rules=availability_rules,
                busy_slots=busy_slots,
                locations=locations,
                analysis=analysis,
                now=sent_at,
                requested_mode=MeetingMode(current_request["mode"]),
            )
            self.persist_options(request_id, options, sent_at)
            option_rows = self.db.rows_to_dicts(self.db.list_options(request_id))
            body = self.assistant._compose_negotiation_email(
                request=current_request,
                options=option_rows,
                workspace=workspace,
                revision=True,
            )
            participant_emails = [
                item["email"]
                for item in self.db.rows_to_dicts(self.db.list_participants(request_id))
            ]
            self.db.update_request(
                request_id,
                {
                    "status": RequestStatus.NEGOTIATING.value
                    if options
                    else RequestStatus.NEEDS_INPUT.value,
                    "scheduled_starts_at": None,
                    "scheduled_ends_at": None,
                    "location_label": None,
                    "location_address": None,
                    "meeting_link": None,
                    "confirmation_due_at": None,
                    "confirmation_sent_at": None,
                    "last_outbound_at": isoformat_minute(sent_at),
                    "next_follow_up_at": isoformat_minute(
                        self.scheduler.next_follow_up_at(
                            MeetingPriority(current_request["priority"]), sent_at
                        )
                    )
                    if options
                    else None,
                    "updated_at": isoformat_minute(sent_at),
                },
            )

        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=participant_emails,
            cc=[workspace["owner_email"]],
            subject=current_request["subject"],
            body=body,
            sent_at=isoformat_minute(sent_at),
            intent=MessageIntent.RESCHEDULE.value,
        )
        return self.assistant.get_request_detail(request_id)

    def cancel_request(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        request_id = int(request_row["id"])
        with self.state_lock:
            existing_meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))
        self.assistant._remove_google_calendar_event(existing_meeting)

        with self.state_lock:
            current_request = self.db.row_to_dict(self.db.get_request(request_id))
            if current_request is None:
                raise ValueError("Request %s not found" % request_id)
            self.db.delete_meeting(request_id)
            self.db.update_request(
                request_id,
                {
                    "status": RequestStatus.CANCELLED.value,
                    "next_follow_up_at": None,
                    "scheduled_starts_at": None,
                    "scheduled_ends_at": None,
                    "location_label": None,
                    "location_address": None,
                    "meeting_link": None,
                    "confirmation_due_at": None,
                    "confirmation_sent_at": None,
                    "updated_at": isoformat_minute(sent_at),
                },
            )
            participant_emails = [
                item["email"]
                for item in self.db.rows_to_dicts(self.db.list_participants(request_id))
            ]

        body = (
            "Hi everyone,\n\n"
            "I have marked this meeting thread as cancelled. If you'd like, reply with a new window and I can reopen the scheduling process.\n\n"
            "Best,\n%s"
        ) % workspace["assistant_name"]
        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=participant_emails + [workspace["owner_email"]],
            cc=[],
            subject=current_request["subject"],
            body=body,
            sent_at=isoformat_minute(sent_at),
            intent=MessageIntent.CANCEL.value,
        )
        return self.assistant.get_request_detail(request_id)

    def send_follow_up(self, request_id: int, sent_at: datetime) -> None:
        workspace = self.assistant.require_workspace()
        with self.state_lock:
            request_row = self.db.row_to_dict(self.db.get_request(request_id))
            if request_row is None:
                return
            options = self.db.rows_to_dicts(self.db.list_options(request_id))
            pending = [
                participant["email"]
                for participant in self.db.rows_to_dicts(self.db.list_participants(request_id))
                if participant["response_status"] != "accepted"
            ]
        if not pending:
            return
        body = self.assistant._compose_follow_up_email(request_row, options, workspace)
        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=pending,
            cc=[workspace["owner_email"]],
            subject=request_row["subject"],
            body=body,
            sent_at=isoformat_minute(sent_at),
            intent=MessageIntent.FOLLOW_UP.value,
        )
        with self.state_lock:
            if self.db.get_request(request_id) is not None:
                self.db.update_request(
                    request_id,
                    {
                        "last_outbound_at": isoformat_minute(sent_at),
                        "next_follow_up_at": isoformat_minute(
                            self.scheduler.next_follow_up_at(
                                MeetingPriority(request_row["priority"]), sent_at
                            )
                        ),
                        "updated_at": isoformat_minute(sent_at),
                    },
                )

    def send_confirmation_reminder(self, request_id: int, sent_at: datetime) -> None:
        workspace = self.assistant.require_workspace()
        with self.state_lock:
            request_row = self.db.row_to_dict(self.db.get_request(request_id))
            meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))
            if request_row is None or meeting is None:
                return
            participants = [
                item["email"]
                for item in self.db.rows_to_dicts(self.db.list_participants(request_id))
            ]
        body = (
            "Hi everyone,\n\n"
            "Quick confirmation for %s.\n\n"
            "%s\n\n"
            "If anything changed, reply here and I will reopen scheduling.\n\n"
            "Best,\n%s"
        ) % (
            display_datetime(meeting["starts_at"], workspace["timezone"]),
            self.assistant._meeting_detail_line(meeting, workspace),
            workspace["assistant_name"],
        )
        self.assistant._record_outbound_email(
            request_id=request_id,
            sender_email=workspace["assistant_email"],
            recipients=participants + [workspace["owner_email"]],
            cc=[],
            subject=request_row["subject"],
            body=body,
            sent_at=isoformat_minute(sent_at),
            intent=MessageIntent.CONFIRMATION.value,
        )
        with self.state_lock:
            if self.db.get_request(request_id) is not None:
                self.db.update_request(
                    request_id,
                    {
                        "confirmation_sent_at": isoformat_minute(sent_at),
                        "updated_at": isoformat_minute(sent_at),
                        "last_outbound_at": isoformat_minute(sent_at),
                    },
                )

    def create_request_record(
        self,
        analysis: AnalysisResult,
        direction: str,
        source: str,
        subject: str,
        organizer_name: Optional[str],
        organizer_email: str,
        requested_location: Optional[str],
        thread_key: str,
        created_at: datetime,
    ) -> int:
        return self.db.create_request(
            {
                "public_id": uuid.uuid4().hex[:12],
                "thread_key": thread_key,
                "direction": direction,
                "source": source,
                "subject": normalize_subject(subject) or subject,
                "objective": analysis.objective,
                "summary": analysis.summary,
                "mode": analysis.mode.value,
                "status": RequestStatus.DRAFT.value,
                "priority": analysis.priority.value,
                "duration_minutes": analysis.duration_minutes,
                "requested_location": requested_location,
                "organizer_name": organizer_name,
                "organizer_email": organizer_email,
                "agenda": analysis.agenda,
                "created_at": isoformat_minute(created_at),
                "updated_at": isoformat_minute(created_at),
                "last_inbound_at": isoformat_minute(created_at)
                if direction == "incoming"
                else None,
                "last_outbound_at": isoformat_minute(created_at)
                if direction == "outgoing"
                else None,
            }
        )

    def derive_external_participants(
        self, payload: Dict[str, Any], workspace: Dict[str, Any]
    ) -> List[str]:
        assistant_email = workspace["assistant_email"]
        owner_email = workspace["owner_email"]
        people = set()
        sender = payload["sender_email"]
        if sender != owner_email:
            people.add(sender)
        for email in payload.get("to", []) + payload.get("cc", []):
            if email not in {assistant_email, owner_email}:
                people.add(email)
        return sorted(people)

    def persist_options(self, request_id: int, options: List[Any], created_at: datetime) -> None:
        serialized = [option_to_dict(option, created_at) for option in options]
        self.db.replace_options(request_id, serialized)

    def all_required_participants_accept(self, request_id: int, option_id: int) -> bool:
        participants = self.db.rows_to_dicts(self.db.list_participants(request_id))
        required = [
            participant for participant in participants if int(participant["required"] or 0)
        ]
        if not required:
            return False
        return all(
            int(participant.get("accepted_option_id") or 0) == option_id for participant in required
        )

    def follow_ups_due(self, now: datetime) -> List[Dict[str, Any]]:
        now_iso = isoformat_minute(now)
        return [
            self.assistant._serialize_request_summary(request)
            for request in self.db.rows_to_dicts(self.db.list_follow_ups_due(now_iso))
        ]

    def confirmations_due(self, now: datetime) -> List[Dict[str, Any]]:
        now_iso = isoformat_minute(now)
        return [
            self.assistant._serialize_request_summary(request)
            for request in self.db.rows_to_dicts(self.db.list_confirmations_due(now_iso))
        ]
