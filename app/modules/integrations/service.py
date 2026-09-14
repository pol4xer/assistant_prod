from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.models import isoformat_minute
from app.modules.shared.constants import (
    GOOGLE_CALENDAR_SOURCE,
    GOOGLE_OAUTH_PROVIDERS,
    GOOGLE_WORKSPACE_PROVIDER,
)
from app.services.integrations import (
    INTEGRATION_SPECS,
    build_integration_snapshot,
    get_integration_spec,
    normalize_integration_values,
)

logger = logging.getLogger(__name__)


class ProviderIntegrationService:
    """Infrastructure-facing service for encrypted provider config and Google OAuth."""

    def __init__(self, assistant: Any):
        self.assistant = assistant

    @property
    def db(self):
        return self.assistant.db

    @property
    def settings(self):
        return self.assistant.settings

    @property
    def vault(self):
        return self.assistant.vault

    def _now(self):
        return self.assistant._now()

    def list_credentials(self) -> List[Dict[str, Any]]:
        return [self.integration_snapshot(spec["provider"]) for spec in INTEGRATION_SPECS]

    def save_credentials(self, provider: str, values: Dict[str, Any]) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        try:
            spec = get_integration_spec(provider)
        except KeyError as exc:
            raise ValueError(str(exc))
        bundle = self.load_provider_bundle(provider)
        normalized_values = normalize_integration_values(spec, values)
        for field in spec["fields"]:
            if field.get("secret") and not normalized_values.get(field["key"]):
                normalized_values[field["key"]] = str(bundle.get(field["key"]) or "")
        if provider in GOOGLE_OAUTH_PROVIDERS:
            auth_fields = {"client_id", "client_secret", "redirect_uri"}
            auth_changed = any(
                bundle.get(key) != value
                for key, value in normalized_values.items()
                if key in auth_fields
            )
            if auth_changed:
                if provider == GOOGLE_WORKSPACE_PROVIDER:
                    self.db.delete_busy_slots_by_source(GOOGLE_CALENDAR_SOURCE)
                for key in [
                    "tokens",
                    "account_connected",
                    "connected_email",
                    "granted_scopes",
                    "calendar_summaries",
                    "selected_calendar_id",
                    "selected_calendar_summary",
                    "last_calendar_sync_at",
                    "warnings",
                ]:
                    bundle.pop(key, None)
        for key, value in normalized_values.items():
            bundle[key] = value
        updated_at = isoformat_minute(self._now())
        self.save_provider_bundle(provider, bundle, updated_at)
        return self.integration_snapshot(provider)

    def start_google_oauth(self, provider: str, redirect_to: Optional[str]) -> str:
        self.assistant.require_live_integrations()
        if provider not in GOOGLE_OAUTH_PROVIDERS:
            raise ValueError("Unsupported Google OAuth provider: %s" % provider)
        bundle = self.load_provider_bundle(provider)
        client_id = str(bundle.get("client_id") or "").strip()
        client_secret = str(bundle.get("client_secret") or "").strip()
        redirect_uri = str(bundle.get("redirect_uri") or "").strip()
        if not client_id or not client_secret or not redirect_uri:
            raise ValueError("Google OAuth credentials are not configured yet")

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=self.google_scopes(provider),
        )
        flow.redirect_uri = redirect_uri
        with self.oauth_transport_override(redirect_uri):
            auth_url, state = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent select_account",
            )
        self.db.create_oauth_state(
            state=state,
            provider=provider,
            created_at=isoformat_minute(self._now()),
            redirect_to=redirect_to,
        )
        return auth_url

    def complete_google_oauth(self, authorization_response: str, state: str) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        state_row = self.db.consume_oauth_state_any(state)
        if state_row is None:
            raise ValueError("Google OAuth state is missing or expired")
        provider = str(state_row["provider"] or "").strip() or GOOGLE_WORKSPACE_PROVIDER

        bundle = self.load_provider_bundle(provider)
        client_id = str(bundle.get("client_id") or "").strip()
        client_secret = str(bundle.get("client_secret") or "").strip()
        redirect_uri = str(bundle.get("redirect_uri") or "").strip()
        if not client_id or not client_secret or not redirect_uri:
            raise ValueError("Google OAuth credentials are not configured yet")

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=self.google_scopes(provider),
            state=state,
        )
        flow.redirect_uri = redirect_uri
        try:
            with self.oauth_transport_override(redirect_uri):
                flow.fetch_token(authorization_response=authorization_response)
        except Exception as exc:
            if exc.__class__.__name__ == "InsecureTransportError":
                raise ValueError(
                    "Google OAuth for localhost needs local HTTP transport enabled. "
                    "The project now does this automatically, so restart the server and try Login with Google again."
                ) from exc
            raise

        token_payload = json.loads(flow.credentials.to_json())
        profile = self.fetch_google_profile(
            flow.credentials,
            include_calendar_list=provider == GOOGLE_WORKSPACE_PROVIDER,
        )
        bundle["tokens"] = token_payload
        bundle["account_connected"] = True
        bundle["connected_email"] = profile["connected_email"]
        bundle["granted_scopes"] = profile["granted_scopes"]
        bundle["calendar_summaries"] = profile["calendar_summaries"]
        bundle["warnings"] = profile["warnings"]
        updated_at = isoformat_minute(self._now())
        self.save_provider_bundle(provider, bundle, updated_at)
        snapshot = self.integration_snapshot(provider)
        snapshot["provider"] = provider
        snapshot["redirect_to"] = state_row["redirect_to"] or "/"
        return snapshot

    def disconnect_google_provider(self, provider: str) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        if provider not in GOOGLE_OAUTH_PROVIDERS:
            raise ValueError("Unsupported Google provider: %s" % provider)
        bundle = self.load_provider_bundle(provider)
        if provider == GOOGLE_WORKSPACE_PROVIDER:
            self.db.delete_busy_slots_by_source(GOOGLE_CALENDAR_SOURCE)
        for key in [
            "tokens",
            "account_connected",
            "connected_email",
            "granted_scopes",
            "calendar_summaries",
            "warnings",
            "selected_calendar_id",
            "selected_calendar_summary",
            "last_calendar_sync_at",
        ]:
            bundle.pop(key, None)
        updated_at = isoformat_minute(self._now())
        self.save_provider_bundle(provider, bundle, updated_at)
        return self.integration_snapshot(provider)

    @contextmanager
    def oauth_transport_override(self, redirect_uri: str):
        previous_insecure = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
        previous_relax_scope = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
        should_allow_http = self.is_local_http_redirect(redirect_uri)
        if should_allow_http:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        try:
            yield
        finally:
            if should_allow_http:
                if previous_insecure is None:
                    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
                else:
                    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous_insecure
            if previous_relax_scope is None:
                os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
            else:
                os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous_relax_scope

    def is_local_http_redirect(self, redirect_uri: str) -> bool:
        parsed = urlparse(redirect_uri)
        return parsed.scheme == "http" and parsed.hostname in {
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
        }

    def integration_snapshot(self, provider: str) -> Dict[str, Any]:
        spec = get_integration_spec(provider)
        bundle = self.load_provider_bundle(provider)
        values = {field["key"]: str(bundle.get(field["key"], "") or "") for field in spec["fields"]}
        metadata = {
            "account_connected": bundle.get("account_connected"),
            "connected_email": bundle.get("connected_email"),
            "granted_scopes": bundle.get("granted_scopes", []),
            "calendar_summaries": bundle.get("calendar_summaries", []),
            "selected_calendar_id": bundle.get("selected_calendar_id"),
            "selected_calendar_summary": bundle.get("selected_calendar_summary"),
            "last_calendar_sync_at": bundle.get("last_calendar_sync_at"),
            "warnings": bundle.get("warnings", []),
            "google_project_hint": self.google_project_hint(bundle),
        }
        stored_row = self.db.row_to_dict(self.db.get_provider_credentials(provider))
        updated_at = stored_row.get("updated_at") if stored_row else None
        snapshot = build_integration_snapshot(spec, values, updated_at, metadata)
        secret_keys = {field["key"] for field in spec["fields"] if field.get("secret")}
        snapshot["secret_fields_configured"] = sorted(key for key in secret_keys if values.get(key))
        snapshot["values"] = {
            key: "" if key in secret_keys else value for key, value in values.items()
        }
        return snapshot

    def load_provider_bundle(self, provider: str) -> Dict[str, Any]:
        if self.assistant.demo_mode:
            return {}
        row = self.db.get_provider_credentials(provider)
        if row is None:
            return {}
        raw_payload = row["credentials_json"]
        if not raw_payload:
            return {}
        if raw_payload.strip().startswith("{"):
            parsed = json.loads(raw_payload)
            encrypted = parsed.get("encrypted")
            if encrypted:
                return self.vault.decrypt_json(encrypted)
            return {str(key): value for key, value in parsed.items()}
        return self.vault.decrypt_json(raw_payload)

    def save_provider_bundle(self, provider: str, bundle: Dict[str, Any], updated_at: str) -> None:
        self.assistant.require_live_integrations()
        self.db.upsert_provider_credentials(
            provider=provider,
            credentials={"encrypted": self.vault.encrypt_json(bundle)},
            updated_at=updated_at,
        )

    def google_scopes(self, provider: Optional[str] = None) -> List[str]:
        # Every Google-backed provider currently shares one baseline scope set.
        # The provider argument keeps this API extensible for per-provider scopes later.
        _ = provider
        return [
            scope.strip()
            for scope in self.settings.google_workspace_scopes.split(",")
            if scope.strip()
        ]

    def google_bundle_scopes(
        self, bundle: Dict[str, Any], provider: Optional[str] = None
    ) -> List[str]:
        granted_scopes = [
            str(scope).strip()
            for scope in (bundle.get("granted_scopes") or [])
            if str(scope).strip()
        ]
        if granted_scopes:
            return granted_scopes
        token_scopes = [
            str(scope).strip()
            for scope in ((bundle.get("tokens") or {}).get("scopes") or [])
            if str(scope).strip()
        ]
        if token_scopes:
            return token_scopes
        return self.google_scopes(provider)

    def google_project_hint(self, bundle: Dict[str, Any]) -> Optional[str]:
        client_id = str(bundle.get("client_id") or "").strip()
        if not client_id or "-" not in client_id:
            return None
        project_hint = client_id.split("-", 1)[0].strip()
        return project_hint or None

    def google_credentials(
        self, bundle: Dict[str, Any], provider: Optional[str] = None
    ) -> Credentials:
        self.assistant.require_live_integrations()
        token_payload = bundle.get("tokens")
        if not isinstance(token_payload, dict):
            raise ValueError("Google account is not connected yet. Use Login with Google first.")
        return Credentials.from_authorized_user_info(
            token_payload,
            scopes=self.google_bundle_scopes(bundle, provider),
        )

    def google_calendar_service(self, bundle: Dict[str, Any]) -> Any:
        self.assistant.require_live_integrations()
        credentials = self.google_credentials(bundle, GOOGLE_WORKSPACE_PROVIDER)
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def gmail_service(self, provider: str) -> Any:
        self.assistant.require_live_integrations()
        bundle = self.load_provider_bundle(provider)
        if not bundle.get("tokens"):
            raise ValueError("Google account is not connected yet for %s." % provider)
        credentials = self.google_credentials(bundle, provider)
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def append_provider_warning(self, provider: str, message: str) -> None:
        if not message:
            return
        bundle = self.load_provider_bundle(provider)
        bundle["warnings"] = self.dedupe_messages([*(bundle.get("warnings", []) or []), message])
        self.save_provider_bundle(provider, bundle, isoformat_minute(self._now()))

    def clear_provider_warnings(self, provider: str, prefixes: List[str]) -> None:
        bundle = self.load_provider_bundle(provider)
        warnings = [
            message
            for message in (bundle.get("warnings", []) or [])
            if not any(prefix in message for prefix in prefixes)
        ]
        if warnings == bundle.get("warnings", []):
            return
        bundle["warnings"] = warnings
        self.save_provider_bundle(provider, bundle, isoformat_minute(self._now()))

    def append_google_warning(self, message: str) -> None:
        self.append_provider_warning(GOOGLE_WORKSPACE_PROVIDER, message)

    def clear_google_warnings(self, prefixes: List[str]) -> None:
        self.clear_provider_warnings(GOOGLE_WORKSPACE_PROVIDER, prefixes)

    def fetch_google_profile(
        self,
        credentials: Any,
        *,
        include_calendar_list: bool = True,
        session_factory: Any = AuthorizedSession,
        build_fn: Any = build,
    ) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        warnings: List[str] = []
        connected_email: Optional[str] = None
        calendar_summaries: List[str] = []

        try:
            session = session_factory(credentials)
            response = session.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=10)
            response.raise_for_status()
            connected_email = response.json().get("email")
        except Exception as exc:
            warnings.append(self.describe_google_api_issue("Google userinfo", exc))

        if not connected_email:
            try:
                gmail = build_fn("gmail", "v1", credentials=credentials, cache_discovery=False)
                profile = gmail.users().getProfile(userId="me").execute()
                connected_email = profile.get("emailAddress")
            except Exception as exc:
                warnings.append(self.describe_google_api_issue("Gmail API", exc))

        if include_calendar_list:
            try:
                calendar = build_fn(
                    "calendar", "v3", credentials=credentials, cache_discovery=False
                )
                calendar_list = calendar.calendarList().list(maxResults=10).execute()
                calendar_summaries = [
                    item.get("summary")
                    for item in calendar_list.get("items", [])
                    if item.get("summary")
                ]
            except Exception as exc:
                warnings.append(self.describe_google_api_issue("Google Calendar API", exc))

        return {
            "connected_email": connected_email,
            "granted_scopes": list(credentials.scopes or self.google_scopes()),
            "calendar_summaries": calendar_summaries,
            "warnings": self.dedupe_messages(warnings),
        }

    def describe_google_api_issue(self, service_name: str, exc: Exception) -> str:
        if isinstance(exc, HttpError):
            reason = str(getattr(exc, "reason", "") or "").strip()
            if reason.lower() == "bad request":
                content = getattr(exc, "content", b"") or b""
                try:
                    payload = json.loads(content.decode("utf-8"))
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or "").strip()
                        if message:
                            reason = message
            if "accessNotConfigured" in str(exc) or "has not been used in project" in str(exc):
                return (
                    f"{service_name} is not enabled in Google Cloud for this OAuth client. "
                    "Enable the required API in Google Cloud Console, wait a few minutes, and retry."
                )
            if reason:
                return f"{service_name}: {reason}"
        message = str(exc).strip()
        return (
            f"{service_name}: {message}" if message else f"{service_name} is unavailable right now."
        )

    def dedupe_messages(self, messages: List[str]) -> List[str]:
        seen: set[str] = set()
        unique: List[str] = []
        for message in messages:
            cleaned = str(message).strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            unique.append(cleaned)
        return unique
