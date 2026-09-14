from __future__ import annotations

import base64
import logging
import re
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Optional

from app.modules.shared.constants import ASSISTANT_GMAIL_PROVIDER
from app.services.analysis import normalize_subject

logger = logging.getLogger(__name__)

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_FULL_SCOPE = "https://mail.google.com/"
AUTO_IGNORE_SUBJECT_PATTERNS = (
    "delivery status notification",
    "undelivered mail returned to sender",
    "mail delivery subsystem",
    "failure notice",
)
MARKETING_KEYWORDS = (
    "newsletter",
    "free trial",
    "unsubscribe",
    "manage preferences",
    "view in browser",
    "special offer",
    "promotion",
    "promo",
    "sale",
    "discount",
    "verify your email",
    "developer newsletter",
    "last chance",
    "see what you're missing",
)
SCHEDULING_KEYWORDS = (
    "schedule",
    "scheduling",
    "meeting",
    "call",
    "availability",
    "available",
    "reschedule",
    "calendar",
    "slot",
    "option 1",
    "option 2",
    "option 3",
    "works for me",
    "google meet",
    "zoom",
)
SCHEDULING_DATE_RE = re.compile(
    r"\b(today|tomorrow|next\s+(?:monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)|monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)\b",
    re.IGNORECASE,
)
SCHEDULING_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE)
SCHEDULING_DURATION_RE = re.compile(r"\b\d{1,3}\s*(?:minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)
SCHEDULING_COORDINATION_RE = re.compile(
    r"\b(works for me|that works|is ok|can we do|could we do|what works|what time|reply with the option|option\s+\d+)\b",
    re.IGNORECASE,
)
SCHEDULING_THRESHOLD = 4


class GmailTransportService:
    """Encapsulates live Gmail send/sync behavior behind a small service boundary."""

    def __init__(self, assistant: Any):
        self.assistant = assistant
        self._modify_scope_notice_emitted: set[str] = set()

    @property
    def db(self):
        return self.assistant.db

    def email_metadata_from_payload(self, payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
        return {
            "provider": str(payload.get("provider") or "local").strip() or "local",
            "external_message_id": str(payload.get("external_message_id") or "").strip() or None,
            "external_thread_id": str(payload.get("external_thread_id") or "").strip() or None,
            "internet_message_id": str(payload.get("internet_message_id") or "").strip() or None,
        }

    def gmail_reply_context(self, request_id: int, sending_provider: str) -> Dict[str, Any]:
        emails = self.db.rows_to_dicts(self.db.list_emails(request_id))
        internet_ids = [
            str(item.get("internet_message_id") or "").strip()
            for item in emails
            if str(item.get("internet_message_id") or "").strip()
        ]
        thread_id = None
        for item in reversed(emails):
            if (
                str(item.get("provider") or "").strip() == sending_provider
                and str(item.get("external_thread_id") or "").strip()
            ):
                thread_id = str(item["external_thread_id"]).strip()
                break
        return {
            "thread_id": thread_id,
            "in_reply_to": internet_ids[-1] if internet_ids else None,
            "references": internet_ids[-8:],
        }

    def record_outbound_email(
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
        with self.assistant._state_lock:
            email_id = self.db.add_email(
                request_id=request_id,
                direction="outbound",
                sender_email=sender_email,
                recipients=recipients,
                cc=cc,
                subject=subject,
                body=body,
                sent_at=sent_at,
                intent=intent,
            )
        if self.assistant.demo_mode:
            return email_id
        bundle = self.assistant._load_provider_bundle(ASSISTANT_GMAIL_PROVIDER)
        if not bundle.get("tokens"):
            return email_id
        self.db.update_email(email_id, {"delivery_status": "pending"})
        try:
            metadata = self.assistant._send_gmail_message(
                provider=ASSISTANT_GMAIL_PROVIDER,
                sender_email=sender_email,
                recipients=recipients,
                cc=cc,
                subject=subject,
                body=body,
                reply_context=self.assistant._gmail_reply_context(
                    request_id, ASSISTANT_GMAIL_PROVIDER
                ),
            )
        except Exception as exc:
            warning = self.assistant._describe_google_api_issue("Assistant Gmail send", exc)
            logger.exception(
                "Assistant Gmail send failed request_id=%s subject=%s reason=%s",
                request_id,
                subject,
                warning,
            )
            self.db.update_email(email_id, {"delivery_status": "failed", "delivery_error": warning})
            self.assistant._append_provider_warning(ASSISTANT_GMAIL_PROVIDER, warning)
            return email_id
        self.assistant._clear_provider_warnings(
            ASSISTANT_GMAIL_PROVIDER, ["Assistant Gmail send", "Assistant Gmail sync"]
        )
        with self.assistant._state_lock:
            self.db.update_email(
                email_id,
                {
                    "provider": ASSISTANT_GMAIL_PROVIDER,
                    "delivery_status": "sent",
                    "delivery_error": None,
                    "external_message_id": metadata.get("external_message_id"),
                    "external_thread_id": metadata.get("external_thread_id"),
                    "internet_message_id": metadata.get("internet_message_id"),
                },
            )
        return email_id

    def send_gmail_message(
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
        self.assistant.require_live_integrations()
        gmail = self.assistant._gmail_service(provider)
        bundle = self.assistant._load_provider_bundle(provider)
        connected_email = str(bundle.get("connected_email") or sender_email).strip() or sender_email
        message = EmailMessage()
        message["From"] = connected_email
        if recipients:
            message["To"] = ", ".join(recipients)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        if reply_context and reply_context.get("in_reply_to"):
            message["In-Reply-To"] = str(reply_context["in_reply_to"])
        if reply_context and reply_context.get("references"):
            message["References"] = " ".join(reply_context["references"])
        message.set_content(body)
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        send_body: Dict[str, Any] = {"raw": raw_message}
        if reply_context and reply_context.get("thread_id"):
            send_body["threadId"] = reply_context["thread_id"]
        sent = gmail.users().messages().send(userId="me", body=send_body).execute()
        # A metadata lookup failure does not undo successful provider delivery.
        metadata: Dict[str, Any] = {}
        try:
            metadata = (
                gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=sent["id"],
                    format="metadata",
                    metadataHeaders=["Message-ID"],
                )
                .execute()
            )
        except Exception:
            logger.warning("Gmail message sent, but Message-ID metadata could not be retrieved")
        return {
            "provider": provider,
            "external_message_id": str(sent.get("id") or "").strip() or None,
            "external_thread_id": str(
                sent.get("threadId") or metadata.get("threadId") or ""
            ).strip()
            or None,
            "internet_message_id": self.assistant._gmail_header_value(metadata, "Message-ID"),
        }

    def gmail_header_value(self, message: Dict[str, Any], header_name: str) -> Optional[str]:
        headers = (message.get("payload") or {}).get("headers") or []
        for item in headers:
            if str(item.get("name") or "").lower() == header_name.lower():
                value = str(item.get("value") or "").strip()
                return value or None
        return None

    def parse_email_header_list(self, value: str) -> List[str]:
        # getaddresses handles quoted display names containing commas correctly.
        return [
            email.strip().lower() for _, email in getaddresses([str(value or "")]) if email.strip()
        ]

    def has_gmail_modify_scope(self, provider: str) -> bool:
        bundle = self.assistant._load_provider_bundle(provider)
        granted_scopes = {
            str(scope).strip()
            for scope in (bundle.get("granted_scopes") or [])
            if str(scope).strip()
        }
        return GMAIL_MODIFY_SCOPE in granted_scopes or GMAIL_FULL_SCOPE in granted_scopes

    def remember_inbox_ignore(
        self,
        provider: str,
        *,
        external_message_id: Optional[str],
        external_thread_id: Optional[str],
        reason: str,
    ) -> None:
        created_at = self.assistant._now().isoformat(timespec="minutes")
        if external_message_id:
            self.db.add_ignored_inbox_message(provider, external_message_id, created_at, reason)
        if external_thread_id:
            self.db.add_ignored_inbox_thread(provider, external_thread_id, created_at, reason)

    def should_ignore_inbox_item(
        self,
        provider: str,
        *,
        external_message_id: Optional[str],
        external_thread_id: Optional[str],
    ) -> bool:
        if external_message_id and self.db.is_ignored_inbox_message(provider, external_message_id):
            return True
        if external_thread_id and self.db.is_ignored_inbox_thread(provider, external_thread_id):
            return True
        return False

    def scheduling_relevance(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        raw_subject = str(parsed.get("subject") or "").strip()
        subject = normalize_subject(raw_subject).lower()
        body = str(parsed.get("body") or "").lower()
        text = f"{subject}\n{body}"
        score = 0
        signals: List[str] = []

        if any(
            keyword in subject
            for keyword in ("meeting", "schedule", "scheduling", "call", "availability")
        ):
            score += 4
            signals.append("subject-scheduling")
        if any(keyword in text for keyword in SCHEDULING_KEYWORDS):
            score += 3
            signals.append("scheduling-keyword")
        if SCHEDULING_COORDINATION_RE.search(text):
            score += 2
            signals.append("coordination-language")
        if SCHEDULING_DATE_RE.search(text):
            score += 2
            signals.append("date-reference")
        if SCHEDULING_TIME_RE.search(text):
            score += 2
            signals.append("time-reference")
        if SCHEDULING_DURATION_RE.search(text):
            score += 1
            signals.append("duration")

        if any(keyword in raw_subject.lower() for keyword in MARKETING_KEYWORDS):
            score -= 4
            signals.append("marketing-subject")
        if any(keyword in body for keyword in MARKETING_KEYWORDS):
            score -= 3
            signals.append("marketing-body")

        return {"score": score, "signals": signals, "normalized_subject": subject}

    def looks_like_scheduling_message(self, parsed: Dict[str, Any]) -> bool:
        return int(self.scheduling_relevance(parsed)["score"]) >= SCHEDULING_THRESHOLD

    def should_auto_ignore_message(
        self,
        provider: str,
        message: Dict[str, Any],
        parsed: Dict[str, Any],
        existing_request: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if existing_request is not None:
            return None
        sender_email = str(parsed.get("sender_email") or "").lower()
        subject = str(parsed.get("subject") or "").lower()
        auto_submitted = (
            str(self.assistant._gmail_header_value(message, "Auto-Submitted") or "").strip().lower()
        )
        precedence = (
            str(self.assistant._gmail_header_value(message, "Precedence") or "").strip().lower()
        )
        list_id = str(self.assistant._gmail_header_value(message, "List-Id") or "").strip().lower()

        if any(pattern in subject for pattern in AUTO_IGNORE_SUBJECT_PATTERNS):
            return "delivery-status-or-bounce"
        if "mailer-daemon" in sender_email or "postmaster" in sender_email:
            return "delivery-status-or-bounce"
        if auto_submitted and auto_submitted != "no":
            return "auto-submitted"
        if precedence in {"bulk", "list", "junk"} or list_id:
            return "mailing-list-or-bulk"
        relevance = self.scheduling_relevance(parsed)
        if int(relevance["score"]) < SCHEDULING_THRESHOLD:
            return "not-scheduling-related"
        workspace = self.assistant.require_workspace()
        assistant_email = str(workspace.get("assistant_email") or "").strip().lower()
        recipients = {
            str(email).strip().lower()
            for email in [*(parsed.get("to") or []), *(parsed.get("cc") or [])]
        }
        if not assistant_email or assistant_email not in recipients:
            return "assistant-not-addressed"
        return None

    def sync_gmail_inbox(self, provider: str) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        gmail = self.assistant._gmail_service(provider)
        response = (
            gmail.users()
            .messages()
            .list(
                userId="me",
                labelIds=["INBOX"],
                q="newer_than:14d",
                maxResults=25,
            )
            .execute()
        )
        imported = 0
        skipped = 0
        messages: List[Dict[str, Any]] = []
        for item in response.get("messages", []):
            external_message_id = str(item.get("id") or "").strip()
            external_thread_id = str(item.get("threadId") or "").strip()
            if not external_message_id:
                continue
            if self.should_ignore_inbox_item(
                provider,
                external_message_id=external_message_id,
                external_thread_id=external_thread_id,
            ):
                skipped += 1
                continue
            if self.db.get_email_by_external_message_id(external_message_id):
                skipped += 1
                continue
            full_message = (
                gmail.users()
                .messages()
                .get(userId="me", id=external_message_id, format="full")
                .execute()
            )
            messages.append(full_message)
        messages.sort(key=lambda item: int(item.get("internalDate") or 0))
        workspace = self.assistant.require_workspace()
        can_mark_as_read = self.has_gmail_modify_scope(provider)
        for message in messages:
            parsed = self.assistant._parse_gmail_message(message, provider)
            if parsed is None:
                skipped += 1
                continue
            if parsed["sender_email"] == workspace["assistant_email"]:
                skipped += 1
                continue
            existing_request = None
            external_thread_id = parsed.get("external_thread_id")
            if external_thread_id:
                existing_request = self.db.row_to_dict(
                    self.db.get_request_by_external_thread_id(external_thread_id)
                )
            auto_ignore_reason = self.should_auto_ignore_message(
                provider, message, parsed, existing_request
            )
            if auto_ignore_reason:
                relevance = self.scheduling_relevance(parsed)
                logger.info(
                    "Assistant Gmail auto-ignored message provider=%s message_id=%s thread_id=%s reason=%s score=%s subject=%s",
                    provider,
                    parsed.get("external_message_id"),
                    external_thread_id,
                    auto_ignore_reason,
                    relevance["score"],
                    parsed.get("subject"),
                )
                self.remember_inbox_ignore(
                    provider,
                    external_message_id=parsed.get("external_message_id"),
                    external_thread_id=None
                    if auto_ignore_reason == "assistant-not-addressed"
                    else external_thread_id,
                    reason=auto_ignore_reason,
                )
                skipped += 1
                continue
            if existing_request is not None:
                parsed["thread_key"] = existing_request["thread_key"]
            self.assistant.ingest_email(parsed)
            if can_mark_as_read:
                try:
                    gmail.users().messages().modify(
                        userId="me",
                        id=message["id"],
                        body={"removeLabelIds": ["UNREAD"]},
                    ).execute()
                except Exception as exc:
                    logger.warning(
                        "Assistant Gmail sync could not mark message as read provider=%s message_id=%s reason=%s",
                        provider,
                        message.get("id"),
                        self.assistant._describe_google_api_issue("Assistant Gmail sync", exc),
                    )
            imported += 1
        if can_mark_as_read:
            self._modify_scope_notice_emitted.discard(provider)
        elif provider not in self._modify_scope_notice_emitted:
            self._modify_scope_notice_emitted.add(provider)
            logger.info(
                "Assistant Gmail sync provider=%s will keep imported messages unread because gmail.modify scope is not granted; reconnect this provider if you want auto-mark-read",
                provider,
            )
        logger.info(
            "Assistant Gmail sync finished provider=%s imported=%s skipped=%s",
            provider,
            imported,
            skipped,
        )
        return {"ok": True, "imported_messages": imported, "skipped_messages": skipped}

    def parse_gmail_message(
        self, message: Dict[str, Any], provider: str
    ) -> Optional[Dict[str, Any]]:
        payload = message.get("payload") or {}
        sender_value = self.assistant._gmail_header_value(message, "From") or ""
        sender_name, sender_email = parseaddr(sender_value)
        sender_email = sender_email.strip().lower()
        if not sender_email:
            return None
        subject = self.assistant._gmail_header_value(message, "Subject") or "(no subject)"
        to_value = self.assistant._gmail_header_value(message, "To") or ""
        cc_value = self.assistant._gmail_header_value(message, "Cc") or ""
        sent_at_value = self.assistant._gmail_header_value(message, "Date") or ""
        try:
            sent_at = (
                parsedate_to_datetime(sent_at_value) if sent_at_value else self.assistant._now()
            )
        except Exception:
            sent_at = self.assistant._now()
        body = (
            self.assistant._extract_gmail_text_body(payload)
            or str(message.get("snippet") or "").strip()
        )
        return {
            "sender_email": sender_email,
            "sender_name": sender_name.strip() or None,
            "to": self.assistant._parse_email_header_list(to_value),
            "cc": self.assistant._parse_email_header_list(cc_value),
            "subject": subject,
            "body": body,
            "sent_at": sent_at,
            "provider": provider,
            "external_message_id": str(message.get("id") or "").strip() or None,
            "external_thread_id": str(message.get("threadId") or "").strip() or None,
            "internet_message_id": self.assistant._gmail_header_value(message, "Message-ID"),
        }

    def extract_gmail_text_body(self, payload: Dict[str, Any]) -> str:
        mime_type = str(payload.get("mimeType") or "")
        body = payload.get("body") or {}
        body_data = body.get("data")
        if mime_type == "text/plain" and body_data:
            return self.assistant._decode_gmail_body_data(body_data)
        for part in payload.get("parts", []) or []:
            text = self.assistant._extract_gmail_text_body(part)
            if text:
                return text
        if body_data:
            return self.assistant._decode_gmail_body_data(body_data)
        return ""

    def decode_gmail_body_data(self, value: str) -> str:
        padding = "=" * (-len(value) % 4)
        try:
            return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii")).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""
