from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build

from app.core.config import LiveIntegrationsDisabled
from app.core.db import Database
from app.core.secrets import SecretVault
from app.models import (
    AnalysisResult,
    AvailabilityHint,
    MeetingMode,
    MeetingPriority,
    MessageIntent,
    RequestStatus,
    display_datetime,
    isoformat_minute,
    parse_iso_datetime,
)
from app.modules.automation.service import AutomationRuntimeService
from app.modules.calendar.service import GoogleCalendarService
from app.modules.demo.service import DemoScenarioService
from app.modules.integrations.service import ProviderIntegrationService
from app.modules.messaging.service import GmailTransportService
from app.modules.requests.service import RequestLifecycleService
from app.modules.shared.constants import (
    ASSISTANT_GMAIL_PROVIDER,
    GOOGLE_OAUTH_PROVIDERS,
    GOOGLE_WORKSPACE_PROVIDER,
)
from app.services.analysis import SchedulingAnalyzer, normalize_subject
from app.services.llm import DEFAULT_OPENAI_MODEL, OpenAIAnalysisError, OpenAIEmailAnalyzer
from app.services.scheduler import Scheduler

logger = logging.getLogger(__name__)


class SchedulingAssistantService:
    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self._vault: Optional[SecretVault] = None
        self.analyzer = SchedulingAnalyzer()
        self.scheduler = Scheduler()
        self._state_lock = RLock()
        self._automation_lock = Lock()
        self._demo_reset_lock = Lock()
        self.automation_runtime = AutomationRuntimeService(self)
        self.provider_integrations = ProviderIntegrationService(self)
        self.gmail_transport = GmailTransportService(self)
        self.google_calendar = GoogleCalendarService(self)
        self.demo_scenarios = DemoScenarioService(self)
        self.request_lifecycle = RequestLifecycleService(self)
        self.db.init()
        self._bootstrap_defaults()

    @property
    def demo_mode(self) -> bool:
        return bool(getattr(self.settings, "app_demo_mode", True))

    def require_live_integrations(self) -> None:
        if self.demo_mode:
            raise LiveIntegrationsDisabled(
                "Live integrations are disabled in demo mode. "
                "Use an isolated local instance with APP_DEMO_MODE=false to opt in."
            )

    @property
    def vault(self) -> SecretVault:
        self.require_live_integrations()
        if self._vault is None:
            Path(self.settings.app_secrets_key_path).parent.mkdir(parents=True, exist_ok=True)
            self._vault = SecretVault(self.settings.app_secrets_key_path)
        return self._vault

    def dashboard_snapshot(self) -> Dict[str, Any]:
        now = self._now()
        workspace = self.require_workspace()
        integrations = self.list_integration_credentials()
        requests = [self._serialize_request_summary(row) for row in self.db.list_requests()]
        meetings = [self._serialize_meeting(row) for row in self.db.list_meetings()]

        return {
            "generated_at": isoformat_minute(now),
            "demo_mode": self.demo_mode,
            "mode": "demo" if self.demo_mode else "live",
            "workspace": workspace,
            "integrations": integrations,
            "availability_rules": self.db.rows_to_dicts(self.db.list_availability_rules()),
            "calendars": self.db.rows_to_dicts(self.db.list_calendars()),
            "busy_slots": self._serialize_busy_slots(),
            "locations": self.db.rows_to_dicts(self.db.list_locations()),
            "requests": requests,
            "meetings": meetings,
            "automation": {
                "follow_ups_due": self._follow_ups_due(now),
                "confirmations_due": self._confirmations_due(now),
                "runtime": self.automation_runtime.snapshot(),
            },
            "summary": {
                "requests_total": len(requests),
                "negotiating": sum(
                    1 for item in requests if item["status"] == RequestStatus.NEGOTIATING.value
                ),
                "confirmed": sum(
                    1 for item in requests if item["status"] == RequestStatus.CONFIRMED.value
                ),
                "follow_ups_due": len(self._follow_ups_due(now)),
                "confirmations_due": len(self._confirmations_due(now)),
                "integrations_configured": sum(1 for item in integrations if item["configured"]),
                "integrations_total": len(integrations),
            },
        }

    def get_request_detail(self, request_id: int) -> Dict[str, Any]:
        request_row = self.db.get_request(request_id)
        if request_row is None:
            raise ValueError("Request %s not found" % request_id)

        workspace = self.require_workspace()
        participants = self.db.rows_to_dicts(self.db.list_participants(request_id))
        options = self.db.rows_to_dicts(self.db.list_options(request_id))
        emails = self.db.rows_to_dicts(self.db.list_emails(request_id))
        meeting = self.db.row_to_dict(self.db.get_meeting_by_request(request_id))

        detail = dict(request_row)
        detail["scheduled_display"] = display_datetime(
            detail.get("scheduled_starts_at"), workspace["timezone"]
        )
        detail["participants"] = participants
        detail["options"] = [self._serialize_option(option) for option in options]
        detail["emails"] = emails
        detail["meeting"] = self._serialize_meeting(meeting) if meeting else None
        return detail

    def update_workspace(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now_iso = isoformat_minute(self._now())
        payload = dict(payload)
        payload["updated_at"] = now_iso
        with self._state_lock:
            self.db.update_workspace(payload)
        return self.require_workspace()

    def list_integration_credentials(self) -> List[Dict[str, Any]]:
        return self.provider_integrations.list_credentials()

    def save_integration_credentials(self, provider: str, values: Dict[str, Any]) -> Dict[str, Any]:
        return self.provider_integrations.save_credentials(provider, values)

    def start_google_oauth(
        self, provider: str = GOOGLE_WORKSPACE_PROVIDER, redirect_to: Optional[str] = None
    ) -> str:
        return self.provider_integrations.start_google_oauth(provider, redirect_to)

    def complete_google_oauth(self, authorization_response: str, state: str) -> Dict[str, Any]:
        return self.provider_integrations.complete_google_oauth(authorization_response, state)

    def disconnect_google_provider(self, provider: str) -> Dict[str, Any]:
        return self.provider_integrations.disconnect_google_provider(provider)

    def connect_google_calendar(self) -> Dict[str, Any]:
        return self.google_calendar.connect_google_calendar()

    def replace_availability(self, rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._state_lock:
            self.db.replace_availability_rules(rules)
            return self.db.rows_to_dicts(self.db.list_availability_rules())

    def add_calendar(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._state_lock:
            calendar_id = self.db.add_calendar(
                name=payload["name"],
                provider=payload.get("provider", "manual"),
                external_id=payload.get("external_id"),
                is_primary=bool(payload.get("is_primary", False)),
                created_at=isoformat_minute(self._now()),
            )
            return self.db.row_to_dict(
                self.db._fetchone("SELECT * FROM calendars WHERE id = ?", (calendar_id,))
            )

    def add_busy_slot(self, calendar_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._state_lock:
            slot_id = self.db.add_busy_slot(
                calendar_id=calendar_id,
                title=payload["title"],
                starts_at=isoformat_minute(payload["starts_at"]),
                ends_at=isoformat_minute(payload["ends_at"]),
                source=payload.get("source", "manual"),
            )
            return self.db.row_to_dict(
                self.db._fetchone("SELECT * FROM busy_slots WHERE id = ?", (slot_id,))
            )

    def add_location(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._state_lock:
            location_id = self.db.add_location(
                label=payload["label"],
                address=payload["address"],
                priority=int(payload.get("priority", 100)),
                is_default=bool(payload.get("is_default", False)),
                lat=payload.get("lat"),
                lng=payload.get("lng"),
            )
            return self.db.row_to_dict(
                self.db._fetchone("SELECT * FROM location_preferences WHERE id = ?", (location_id,))
            )

    def _analyze_message(
        self,
        *,
        subject: str,
        body: str,
        reference_at: datetime,
        timezone_name: str,
        default_duration_minutes: int,
        forced_intent: Optional[MessageIntent],
    ) -> AnalysisResult:
        if forced_intent == MessageIntent.NEW_REQUEST:
            baseline = self.analyzer.analyze_new_request(
                subject=subject,
                body=body,
                reference_at=reference_at,
                timezone_name=timezone_name,
                default_duration_minutes=default_duration_minutes,
            )
        else:
            baseline = self.analyzer.analyze_reply(
                subject=subject,
                body=body,
                reference_at=reference_at,
                timezone_name=timezone_name,
                default_duration_minutes=default_duration_minutes,
            )

        overlay = self._analyze_with_openai(
            subject=subject,
            body=body,
            reference_at=reference_at,
            timezone_name=timezone_name,
            default_duration_minutes=default_duration_minutes,
            forced_intent=forced_intent.value if forced_intent else None,
        )
        if not overlay:
            return baseline
        return self._merge_analysis_result(baseline, overlay, forced_intent)

    def _analyze_with_openai(
        self,
        *,
        subject: str,
        body: str,
        reference_at: datetime,
        timezone_name: str,
        default_duration_minutes: int,
        forced_intent: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if self.demo_mode:
            return None
        bundle = self._load_provider_bundle("openai")
        api_key = str(bundle.get("api_key") or "").strip()
        if not api_key:
            return None
        model = str(
            bundle.get("model") or getattr(self.settings, "openai_default_model", "")
        ).strip()
        if not model:
            model = DEFAULT_OPENAI_MODEL
        system_prompt = str(bundle.get("system_prompt") or "").strip()
        analyzer = OpenAIEmailAnalyzer(
            api_key=api_key,
            allow_network=True,
            model=model,
            system_prompt=system_prompt,
            timeout_seconds=float(getattr(self.settings, "openai_timeout_seconds", 20.0)),
        )
        try:
            return analyzer.analyze_message(
                subject=subject,
                body=body,
                reference_at=reference_at,
                timezone_name=timezone_name,
                default_duration_minutes=default_duration_minutes,
                forced_intent=forced_intent,
            )
        except OpenAIAnalysisError as exc:
            logger.warning("OpenAI analysis fallback: %s", exc)
        except Exception as exc:
            logger.warning("OpenAI runtime error, falling back to heuristics: %s", exc)
        return None

    def _merge_analysis_result(
        self,
        baseline: AnalysisResult,
        overlay: Dict[str, Any],
        forced_intent: Optional[MessageIntent],
    ) -> AnalysisResult:
        intent = forced_intent or baseline.intent
        if forced_intent is None:
            intent_value = str(overlay.get("intent") or "").strip()
            if intent_value:
                try:
                    intent = MessageIntent(intent_value)
                except ValueError:
                    intent = baseline.intent

        priority = baseline.priority
        priority_value = str(overlay.get("priority") or "").strip()
        if priority_value:
            try:
                priority = MeetingPriority(priority_value)
            except ValueError:
                priority = baseline.priority

        mode = baseline.mode
        mode_value = str(overlay.get("mode") or "").strip()
        if mode_value:
            try:
                mode = MeetingMode(mode_value)
            except ValueError:
                mode = baseline.mode

        duration = baseline.duration_minutes
        overlay_duration = overlay.get("duration_minutes")
        if isinstance(overlay_duration, int) and overlay_duration > 0:
            duration = max(15, overlay_duration)

        objective = str(overlay.get("objective") or "").strip() or baseline.objective
        summary = str(overlay.get("summary") or "").strip() or baseline.summary
        agenda = str(overlay.get("agenda") or "").strip() or baseline.agenda

        requested_location = baseline.requested_location
        if "requested_location" in overlay:
            requested_location = str(overlay.get("requested_location") or "").strip() or None
        if mode == MeetingMode.ONLINE:
            requested_location = None

        availability_hints = (
            self._availability_hints_from_payload(overlay) or baseline.availability_hints
        )

        return AnalysisResult(
            intent=intent,
            priority=priority,
            mode=mode,
            duration_minutes=duration,
            objective=objective,
            summary=summary,
            agenda=agenda,
            requested_location=requested_location,
            availability_hints=availability_hints,
        )

    def _availability_hints_from_payload(self, payload: Dict[str, Any]) -> List[AvailabilityHint]:
        hints: List[AvailabilityHint] = []
        for item in payload.get("availability_hints", []):
            if not isinstance(item, dict):
                continue
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            if not start or not end:
                continue
            try:
                start_dt = parse_iso_datetime(start)
                end_dt = parse_iso_datetime(end)
            except Exception:
                continue
            if end_dt <= start_dt:
                continue
            hints.append(
                AvailabilityHint(
                    start=start_dt,
                    end=end_dt,
                    exact=bool(item.get("exact", False)),
                    source_text=str(item.get("source_text") or "").strip()[:120],
                )
            )
        return hints

    def create_outgoing_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request_lifecycle.create_outgoing_request(payload)

    def ingest_email(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request_lifecycle.ingest_email(payload)

    def run_automation(self, current_time: Optional[datetime] = None) -> Dict[str, Any]:
        return self.request_lifecycle.run_automation(current_time)

    def reset_demo_data(self) -> Dict[str, Any]:
        return self.request_lifecycle.reset_demo_data()

    def run_one_click_test_pipeline(self) -> Dict[str, Any]:
        return self.demo_scenarios.run_one_click_test_pipeline()

    def run_cc_assistant_test_pipeline(self) -> Dict[str, Any]:
        return self.demo_scenarios.run_cc_assistant_test_pipeline()

    def sync_assistant_gmail(self) -> Dict[str, Any]:
        with self._automation_lock:
            sync_result = self._sync_gmail_inbox(ASSISTANT_GMAIL_PROVIDER)
            snapshot = self._integration_snapshot(ASSISTANT_GMAIL_PROVIDER)
            snapshot["gmail_sync"] = sync_result
            return snapshot

    def run_live_gmail_cc_demo(self) -> Dict[str, Any]:
        self.require_live_integrations()
        return self.demo_scenarios.run_live_gmail_cc_demo()

    def require_workspace(self) -> Dict[str, Any]:
        workspace = self.db.row_to_dict(self.db.get_workspace())
        if workspace is None:
            raise ValueError("Workspace is not configured")
        return workspace

    def _bootstrap_defaults(self) -> None:
        now_iso = isoformat_minute(self._now())
        default_assistant_email = self.settings.assistant_email or "assistant@example.com"
        default_owner_name = self.settings.owner_name or "Alex Morgan"
        default_owner_email = self.settings.owner_email or "owner@example.com"
        self.db.ensure_workspace(
            {
                "assistant_name": self.settings.assistant_name,
                "assistant_email": default_assistant_email,
                "owner_name": default_owner_name,
                "owner_email": default_owner_email,
                "timezone": self.settings.default_timezone,
                "default_duration_minutes": self.settings.default_duration_minutes,
                "meeting_buffer_minutes": self.settings.meeting_buffer_minutes,
                "min_notice_hours": self.settings.min_notice_hours,
                "confirmation_lead_hours": self.settings.confirmation_lead_hours,
                "workday_start": "09:00",
                "workday_end": "18:00",
                "base_location_label": "Demo Office",
                "base_lat": 51.5000,
                "base_lng": -0.1200,
                "online_provider": self.settings.online_provider,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
        if not self.db.list_availability_rules():
            self.db.replace_availability_rules(
                [
                    {"weekday": weekday, "start_time": "09:00", "end_time": "18:00"}
                    for weekday in range(5)
                ]
            )
        if not self.db.list_calendars():
            self.db.add_calendar(
                name="Business Calendar",
                provider="manual",
                is_primary=True,
                created_at=now_iso,
            )
            self.db.add_calendar(
                name="Personal Calendar",
                provider="manual",
                is_primary=False,
                created_at=now_iso,
            )
        if not self.db.list_locations():
            self.db.add_location(
                label="Demo Office",
                address="1 Example Street (fictional)",
                lat=51.5000,
                lng=-0.1200,
                priority=10,
                is_default=True,
            )
            self.db.add_location(
                label="Demo Cafe",
                address="2 Example Street (fictional)",
                lat=51.5010,
                lng=-0.1210,
                priority=20,
                is_default=False,
            )
        if self.demo_mode:
            return

        bootstrap_client_id = getattr(self.settings, "google_oauth_client_id", None) or getattr(
            self.settings, "oauth_client_id", None
        )
        bootstrap_client_secret = getattr(
            self.settings, "google_oauth_client_secret", None
        ) or getattr(self.settings, "oauth_client_secret", None)
        bootstrap_redirect_uri = getattr(
            self.settings, "google_oauth_redirect_uri", None
        ) or getattr(self.settings, "oauth_redirect_uri", None)
        for provider in GOOGLE_OAUTH_PROVIDERS:
            google_bundle = self._load_provider_bundle(provider)
            changed = False
            if bootstrap_client_id and not google_bundle.get("client_id"):
                google_bundle["client_id"] = bootstrap_client_id
                changed = True
            if bootstrap_client_secret and not google_bundle.get("client_secret"):
                google_bundle["client_secret"] = bootstrap_client_secret
                changed = True
            if bootstrap_redirect_uri and not google_bundle.get("redirect_uri"):
                google_bundle["redirect_uri"] = bootstrap_redirect_uri
                changed = True
            if changed:
                self._save_provider_bundle(provider, google_bundle, now_iso)

    def _create_incoming_request(
        self,
        payload: Dict[str, Any],
        workspace: Dict[str, Any],
        sent_at: datetime,
        thread_key: str,
    ) -> Dict[str, Any]:
        return self.request_lifecycle.create_incoming_request(
            payload, workspace, sent_at, thread_key
        )

    def _handle_existing_thread(
        self,
        request_row: Dict[str, Any],
        payload: Dict[str, Any],
        workspace: Dict[str, Any],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        return self.request_lifecycle.handle_existing_thread(
            request_row, payload, workspace, sent_at
        )

    def _register_acceptance(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        analysis: AnalysisResult,
        sender_email: str,
        selected_option: Optional[Dict[str, Any]],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        return self.request_lifecycle.register_acceptance(
            request_row=request_row,
            workspace=workspace,
            analysis=analysis,
            sender_email=sender_email,
            selected_option=selected_option,
            sent_at=sent_at,
        )

    def _confirm_request(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        option_row: Dict[str, Any],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        return self.request_lifecycle.confirm_request(request_row, workspace, option_row, sent_at)

    def _replan_request(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        analysis: AnalysisResult,
        sent_at: datetime,
    ) -> Dict[str, Any]:
        return self.request_lifecycle.replan_request(request_row, workspace, analysis, sent_at)

    def _cancel_request(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        sent_at: datetime,
    ) -> Dict[str, Any]:
        return self.request_lifecycle.cancel_request(request_row, workspace, sent_at)

    def _send_follow_up(self, request_id: int, sent_at: datetime) -> None:
        self.request_lifecycle.send_follow_up(request_id, sent_at)

    def _send_confirmation_reminder(self, request_id: int, sent_at: datetime) -> None:
        self.request_lifecycle.send_confirmation_reminder(request_id, sent_at)

    def _create_request_record(
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
        return self.request_lifecycle.create_request_record(
            analysis=analysis,
            direction=direction,
            source=source,
            subject=subject,
            organizer_name=organizer_name,
            organizer_email=organizer_email,
            requested_location=requested_location,
            thread_key=thread_key,
            created_at=created_at,
        )

    def _derive_external_participants(
        self, payload: Dict[str, Any], workspace: Dict[str, Any]
    ) -> List[str]:
        return self.request_lifecycle.derive_external_participants(payload, workspace)

    def _persist_options(self, request_id: int, options: List[Any], created_at: datetime) -> None:
        self.request_lifecycle.persist_options(request_id, options, created_at)

    def _compose_negotiation_email(
        self,
        request: Dict[str, Any],
        options: List[Dict[str, Any]],
        workspace: Dict[str, Any],
        revision: bool,
    ) -> str:
        intro = (
            "I reworked the schedule based on the latest reply."
            if revision
            else ("I found a few options that fit %s's calendar." % workspace["owner_name"])
        )
        lines = [
            "Hi everyone,",
            "",
            intro,
            "",
            "Meeting: %s" % request["subject"],
        ]
        if request.get("agenda"):
            lines.append("Agenda: %s" % request["agenda"])
        lines.append("")
        for index, option in enumerate(options, start=1):
            lines.append("Option %s: %s" % (index, self._option_line(option, workspace)))
        lines.extend(
            [
                "",
                "Reply with the option number that works best, or send an alternative window and I will adjust.",
                "",
                "Best,",
                workspace["assistant_name"],
            ]
        )
        return "\n".join(lines)

    def _compose_needs_input_email(self, request: Dict[str, Any], workspace: Dict[str, Any]) -> str:
        return (
            "Hi everyone,\n\n"
            'I could not find a clean slot from the information in this thread yet for "%s".\n'
            "Please reply with two or three concrete windows that could work, and I will continue the scheduling from there.\n\n"
            "Best,\n%s"
        ) % (request["subject"], workspace["assistant_name"])

    def _compose_waiting_email(
        self, request: Dict[str, Any], chosen_option: Dict[str, Any], workspace: Dict[str, Any]
    ) -> str:
        return (
            "Thanks. I have you down for %s and I am waiting on the remaining participants before I finalize the invite.\n\n"
            "Best,\n%s"
        ) % (self._option_line(chosen_option, workspace), workspace["assistant_name"])

    def _compose_clarification_email(
        self, request: Dict[str, Any], options: List[Dict[str, Any]], workspace: Dict[str, Any]
    ) -> str:
        if options:
            option_lines = "\n".join(
                "Option %s: %s" % (index, self._option_line(option, workspace))
                for index, option in enumerate(options, start=1)
            )
            return (
                "Thanks. I need a specific confirmation to lock this in.\n\n"
                "%s\n\n"
                "Reply with the option number that works, or send a new window.\n\n"
                "Best,\n%s"
            ) % (option_lines, workspace["assistant_name"])
        return (
            "Thanks. I still need a concrete window to move this thread forward.\n\n"
            "Please reply with a few specific times that could work.\n\n"
            "Best,\n%s"
        ) % workspace["assistant_name"]

    def _compose_follow_up_email(
        self, request: Dict[str, Any], options: List[Dict[str, Any]], workspace: Dict[str, Any]
    ) -> str:
        option_lines = "\n".join(
            "Option %s: %s" % (index, self._option_line(option, workspace))
            for index, option in enumerate(options, start=1)
        )
        return (
            "Hi everyone,\n\n"
            'Following up on "%s" so we can lock in a time.\n\n'
            "%s\n\n"
            "If none of these work, send back a couple of alternatives and I will rework the thread.\n\n"
            "Best,\n%s"
        ) % (request["subject"], option_lines, workspace["assistant_name"])

    def _compose_confirmation_email(
        self, request: Dict[str, Any], meeting: Dict[str, Any], workspace: Dict[str, Any]
    ) -> str:
        return (
            "Hi everyone,\n\n"
            'We are confirmed for "%s".\n\n'
            "%s\n"
            "Agenda: %s\n\n"
            "I will send a brief confirmation note before the meeting.\n\n"
            "Best,\n%s"
        ) % (
            request["subject"],
            self._meeting_detail_line(meeting, workspace),
            request.get("agenda") or request["summary"],
            workspace["assistant_name"],
        )

    def _compose_already_confirmed_email(
        self,
        request: Dict[str, Any],
        meeting: Optional[Dict[str, Any]],
        workspace: Dict[str, Any],
    ) -> str:
        detail_line = (
            self._meeting_detail_line(meeting, workspace)
            if meeting
            else "The meeting is already confirmed."
        )
        return (
            "Hi everyone,\n\n"
            "This meeting thread is already confirmed.\n\n"
            "%s\n\n"
            "If you need to change the timing, reply with a new window or say cancel and I will reopen scheduling.\n\n"
            "Best,\n%s"
        ) % (detail_line, workspace["assistant_name"])

    def _compose_cancelled_thread_email(
        self, request: Dict[str, Any], workspace: Dict[str, Any]
    ) -> str:
        return (
            "Hi everyone,\n\n"
            "This thread is currently marked as cancelled.\n\n"
            'If you want to reopen it, reply with a concrete time window and I will propose fresh options for "%s".\n\n'
            "Best,\n%s"
        ) % (request["subject"], workspace["assistant_name"])

    def _meeting_detail_line(self, meeting: Dict[str, Any], workspace: Dict[str, Any]) -> str:
        when = display_datetime(meeting["starts_at"], workspace["timezone"])
        ends = display_datetime(meeting["ends_at"], workspace["timezone"])
        detail = "%s to %s" % (when, ends.split(" ", 3)[-1] if ends else "")
        if meeting["mode"] == MeetingMode.ONLINE.value:
            return "%s via %s" % (detail, meeting.get("meeting_link") or "online link pending")
        location = (
            meeting.get("location_label") or meeting.get("location_address") or "location pending"
        )
        return "%s at %s" % (detail, location)

    def _option_line(self, option: Dict[str, Any], workspace: Dict[str, Any]) -> str:
        start = display_datetime(option["starts_at"], workspace["timezone"])
        end = display_datetime(option["ends_at"], workspace["timezone"])
        line = "%s to %s" % (start, end.split(" ", 3)[-1] if end else "")
        if option["mode"] == MeetingMode.ONLINE.value:
            return "%s online" % line
        place = option.get("location_label") or option.get("location_address") or "offline"
        travel = option.get("travel_minutes") or 0
        return "%s at %s (%s min travel)" % (line, place, travel)

    def _all_required_participants_accept(self, request_id: int, option_id: int) -> bool:
        return self.request_lifecycle.all_required_participants_accept(request_id, option_id)

    def _serialize_request_summary(self, row: Any) -> Dict[str, Any]:
        request = dict(row)
        options = self.db.rows_to_dicts(self.db.list_options(int(request["id"])))
        participants = self.db.rows_to_dicts(self.db.list_participants(int(request["id"])))
        request["scheduled_display"] = display_datetime(
            request.get("scheduled_starts_at"), self.require_workspace()["timezone"]
        )
        request["participant_count"] = len(participants)
        request["options"] = [self._serialize_option(option) for option in options]
        request["pending_participants"] = [
            participant["email"]
            for participant in participants
            if participant["response_status"] != "accepted"
        ]
        return request

    def _serialize_option(self, option: Dict[str, Any]) -> Dict[str, Any]:
        serialized = dict(option)
        serialized["label"] = self._option_line(serialized, self.require_workspace())
        return serialized

    def _serialize_meeting(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        meeting = dict(row)
        meeting["label"] = self._meeting_detail_line(meeting, self.require_workspace())
        return meeting

    def _serialize_busy_slots(self) -> List[Dict[str, Any]]:
        workspace = self.require_workspace()
        busy_slots = self.db.rows_to_dicts(self.db.list_busy_slots())
        for slot in busy_slots:
            slot["label"] = "%s to %s" % (
                display_datetime(slot["starts_at"], workspace["timezone"]),
                display_datetime(slot["ends_at"], workspace["timezone"]),
            )
        return busy_slots

    def _serialize_hints(self, hints: Iterable[Any]) -> List[Dict[str, Any]]:
        return [
            {
                "start": isoformat_minute(hint.start),
                "end": isoformat_minute(hint.end),
                "exact": hint.exact,
                "source_text": hint.source_text,
            }
            for hint in hints
        ]

    def _follow_ups_due(self, now: datetime) -> List[Dict[str, Any]]:
        return self.request_lifecycle.follow_ups_due(now)

    def _confirmations_due(self, now: datetime) -> List[Dict[str, Any]]:
        return self.request_lifecycle.confirmations_due(now)

    def _thread_key(self, subject: str) -> str:
        normalized = normalize_subject(subject).lower()
        normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
        return normalized or uuid.uuid4().hex[:10]

    def _new_thread_key(self, subject: str) -> str:
        base = self._thread_key(subject)
        if self.db.get_request_by_thread(base) is None:
            return base
        for index in range(2, 1000):
            candidate = f"{base}-{index}"
            if self.db.get_request_by_thread(candidate) is None:
                return candidate
        return f"{base}-{uuid.uuid4().hex[:8]}"

    def _one_click_test_start(self, now: datetime, workspace: Dict[str, Any]) -> datetime:
        return self.demo_scenarios.one_click_test_start(now, workspace)

    def _one_click_slot_is_available(
        self,
        *,
        workspace: Dict[str, Any],
        availability_rules: List[Dict[str, Any]],
        busy_slots: List[Dict[str, Any]],
        start: datetime,
        end: datetime,
    ) -> bool:
        return self.demo_scenarios.one_click_slot_is_available(
            workspace=workspace,
            availability_rules=availability_rules,
            busy_slots=busy_slots,
            start=start,
            end=end,
        )

    def _find_one_click_test_option_index(
        self,
        options: List[Dict[str, Any]],
        target_start: datetime,
        target_end: datetime,
    ) -> Optional[int]:
        return self.demo_scenarios.find_one_click_test_option_index(
            options,
            target_start=target_start,
            target_end=target_end,
        )

    def _looks_like_reply_subject(self, subject: str) -> bool:
        stripped = str(subject or "").strip().lower()
        return stripped.startswith(("re:", "fw:", "fwd:"))

    def _now(self) -> datetime:
        return datetime.now(tz=ZoneInfo(self.settings.default_timezone))

    def _oauth_transport_override(self, redirect_uri: str):
        return self.provider_integrations.oauth_transport_override(redirect_uri)

    def _is_local_http_redirect(self, redirect_uri: str) -> bool:
        return self.provider_integrations.is_local_http_redirect(redirect_uri)

    def _integration_snapshot(self, provider: str) -> Dict[str, Any]:
        return self.provider_integrations.integration_snapshot(provider)

    def _load_provider_bundle(self, provider: str) -> Dict[str, Any]:
        return self.provider_integrations.load_provider_bundle(provider)

    def _save_provider_bundle(self, provider: str, bundle: Dict[str, Any], updated_at: str) -> None:
        self.provider_integrations.save_provider_bundle(provider, bundle, updated_at)

    def _google_scopes(self) -> List[str]:
        return self.provider_integrations.google_scopes()

    def _google_project_hint(self, bundle: Dict[str, Any]) -> Optional[str]:
        return self.provider_integrations.google_project_hint(bundle)

    def _google_credentials(self, bundle: Dict[str, Any]) -> Any:
        return self.provider_integrations.google_credentials(bundle)

    def _google_calendar_service(self, bundle: Dict[str, Any]) -> Any:
        return self.provider_integrations.google_calendar_service(bundle)

    def _gmail_service(self, provider: str) -> Any:
        return self.provider_integrations.gmail_service(provider)

    def _email_metadata_from_payload(self, payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
        return self.gmail_transport.email_metadata_from_payload(payload)

    def _gmail_reply_context(self, request_id: int, sending_provider: str) -> Dict[str, Any]:
        return self.gmail_transport.gmail_reply_context(request_id, sending_provider)

    def _record_outbound_email(
        self,
        *,
        request_id: int,
        sender_email: str,
        recipients: List[str],
        cc: List[str],
        subject: str,
        body: str,
        sent_at: str,
        intent: Optional[str],
    ) -> int:
        return self.gmail_transport.record_outbound_email(
            request_id=request_id,
            sender_email=sender_email,
            recipients=recipients,
            cc=cc,
            subject=subject,
            body=body,
            sent_at=sent_at,
            intent=intent,
        )

    def _send_gmail_message(
        self,
        *,
        provider: str,
        sender_email: str,
        recipients: List[str],
        cc: List[str],
        subject: str,
        body: str,
        reply_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Optional[str]]:
        return self.gmail_transport.send_gmail_message(
            provider=provider,
            sender_email=sender_email,
            recipients=recipients,
            cc=cc,
            subject=subject,
            body=body,
            reply_context=reply_context,
        )

    def _gmail_header_value(self, message: Dict[str, Any], header_name: str) -> Optional[str]:
        return self.gmail_transport.gmail_header_value(message, header_name)

    def _parse_email_header_list(self, value: str) -> List[str]:
        return self.gmail_transport.parse_email_header_list(value)

    def _sync_gmail_inbox(self, provider: str) -> Dict[str, Any]:
        return self.gmail_transport.sync_gmail_inbox(provider)

    def _parse_gmail_message(
        self, message: Dict[str, Any], provider: str
    ) -> Optional[Dict[str, Any]]:
        return self.gmail_transport.parse_gmail_message(message, provider)

    def _extract_gmail_text_body(self, payload: Dict[str, Any]) -> str:
        return self.gmail_transport.extract_gmail_text_body(payload)

    def _decode_gmail_body_data(self, value: str) -> str:
        return self.gmail_transport.decode_gmail_body_data(value)

    def _refresh_google_calendar_context(
        self,
        force: bool,
        reference_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        return self.google_calendar.refresh_google_calendar_context(force, reference_time)

    def _google_calendar_sync_is_stale(
        self, bundle: Dict[str, Any], reference_time: datetime
    ) -> bool:
        return self.google_calendar.google_calendar_sync_is_stale(bundle, reference_time)

    def _sync_google_calendar(
        self,
        bundle: Dict[str, Any],
        force: bool,
        reference_time: datetime,
    ) -> Dict[str, Any]:
        return self.google_calendar.sync_google_calendar(bundle, force, reference_time)

    def _ensure_google_calendar_row(
        self, calendar_external_id: str, label: str, created_at: datetime
    ) -> int:
        return self.google_calendar.ensure_google_calendar_row(
            calendar_external_id, label, created_at
        )

    def _google_event_window(
        self,
        event: Dict[str, Any],
        default_tzinfo: Any,
    ) -> tuple[Optional[datetime], Optional[datetime]]:
        return self.google_calendar.google_event_window(event, default_tzinfo)

    def _create_google_calendar_event(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        option_row: Dict[str, Any],
        participant_emails: List[str],
        fallback_meeting_link: Optional[str],
    ) -> Optional[Dict[str, Optional[str]]]:
        return self.google_calendar.create_google_calendar_event(
            request_row=request_row,
            workspace=workspace,
            option_row=option_row,
            participant_emails=participant_emails,
            fallback_meeting_link=fallback_meeting_link,
        )

    def _build_google_event_body(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        option_row: Dict[str, Any],
        participant_emails: List[str],
        fallback_meeting_link: Optional[str],
        include_conference_data: bool,
    ) -> Dict[str, Any]:
        return self.google_calendar.build_google_event_body(
            request_row=request_row,
            workspace=workspace,
            option_row=option_row,
            participant_emails=participant_emails,
            fallback_meeting_link=fallback_meeting_link,
            include_conference_data=include_conference_data,
        )

    def _should_retry_google_booking_without_meet(self, exc: Any) -> bool:
        return self.google_calendar.should_retry_google_booking_without_meet(exc)

    def _google_event_datetime(self, value: str) -> str:
        return self.google_calendar.google_event_datetime(value)

    def _sync_google_event_link_metadata(
        self,
        service: Any,
        calendar_id: str,
        event: Dict[str, Any],
        meeting_link: str,
        workspace: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.google_calendar.sync_google_event_link_metadata(
            service=service,
            calendar_id=calendar_id,
            event=event,
            meeting_link=meeting_link,
            workspace=workspace,
        )

    def _repair_missing_google_calendar_events(self, reference_time: datetime) -> Dict[str, int]:
        return self.google_calendar.repair_missing_google_calendar_events(reference_time)

    def _meeting_option_for_repair(
        self,
        request_row: Dict[str, Any],
        meeting: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        return self.google_calendar.meeting_option_for_repair(request_row, meeting)

    def _remove_google_calendar_event(self, meeting: Optional[Dict[str, Any]]) -> bool:
        return self.google_calendar.remove_google_calendar_event(meeting)

    def delete_request(self, request_id: int) -> Dict[str, Any]:
        return self.request_lifecycle.delete_request(request_id)

    def _google_conference_link(self, event: Dict[str, Any]) -> Optional[str]:
        return self.google_calendar.google_conference_link(event)

    def _append_provider_warning(self, provider: str, message: str) -> None:
        self.provider_integrations.append_provider_warning(provider, message)

    def _clear_provider_warnings(self, provider: str, prefixes: List[str]) -> None:
        self.provider_integrations.clear_provider_warnings(provider, prefixes)

    def _append_google_warning(self, message: str) -> None:
        self.provider_integrations.append_google_warning(message)

    def _clear_google_warnings(self, prefixes: List[str]) -> None:
        self.provider_integrations.clear_google_warnings(prefixes)

    def _fetch_google_profile(
        self,
        credentials: Any,
        *,
        include_calendar_list: bool = True,
    ) -> Dict[str, Any]:
        return self.provider_integrations.fetch_google_profile(
            credentials,
            include_calendar_list=include_calendar_list,
            session_factory=AuthorizedSession,
            build_fn=build,
        )

    def _describe_google_api_issue(self, service_name: str, exc: Exception) -> str:
        return self.provider_integrations.describe_google_api_issue(service_name, exc)

    def _dedupe_messages(self, messages: List[str]) -> List[str]:
        return self.provider_integrations.dedupe_messages(messages)
