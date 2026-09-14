from __future__ import annotations

from typing import Any, Dict, List

INTEGRATION_SPECS: List[Dict[str, Any]] = [
    {
        "provider": "google_workspace",
        "title": "Google Workspace",
        "description": "Owner account for Google Calendar booking and Google Meet creation.",
        "services": ["Google Calendar API", "Google Meet"],
        "oauth_login": True,
        "calendar_connection": True,
        "fields": [
            {"key": "client_id", "label": "OAuth Client ID", "required": True, "secret": False},
            {
                "key": "client_secret",
                "label": "OAuth Client Secret",
                "required": True,
                "secret": True,
            },
            {"key": "redirect_uri", "label": "Redirect URI", "required": True, "secret": False},
        ],
    },
    {
        "provider": "assistant_gmail",
        "title": "Assistant Gmail",
        "description": "Gmail account used by the agent for real inbox sync and outbound email sending.",
        "services": ["Gmail API"],
        "oauth_login": True,
        "fields": [
            {"key": "client_id", "label": "OAuth Client ID", "required": True, "secret": False},
            {
                "key": "client_secret",
                "label": "OAuth Client Secret",
                "required": True,
                "secret": True,
            },
            {"key": "redirect_uri", "label": "Redirect URI", "required": True, "secret": False},
        ],
    },
    {
        "provider": "test_client_gmail",
        "title": "Test Client Gmail",
        "description": "Optional Gmail account used for live end-to-end test emails from the client side.",
        "services": ["Gmail API"],
        "oauth_login": True,
        "fields": [
            {"key": "client_id", "label": "OAuth Client ID", "required": True, "secret": False},
            {
                "key": "client_secret",
                "label": "OAuth Client Secret",
                "required": True,
                "secret": True,
            },
            {"key": "redirect_uri", "label": "Redirect URI", "required": True, "secret": False},
        ],
    },
    {
        "provider": "google_maps",
        "implemented": False,
        "title": "Google Maps API",
        "description": "For location lookup, travel-time estimates, and traffic-aware scheduling.",
        "services": ["Google Maps API"],
        "fields": [
            {"key": "api_key", "label": "API Key", "required": True, "secret": True},
            {"key": "default_city", "label": "Default City", "required": False, "secret": False},
        ],
    },
    {
        "provider": "zoom",
        "implemented": False,
        "title": "Zoom API",
        "description": "Planned integration. Zoom meeting creation is not implemented.",
        "services": ["Zoom API"],
        "fields": [
            {"key": "account_id", "label": "Account ID", "required": True, "secret": False},
            {"key": "client_id", "label": "Client ID", "required": True, "secret": False},
            {"key": "client_secret", "label": "Client Secret", "required": True, "secret": True},
        ],
    },
    {
        "provider": "openai",
        "title": "LLM / OpenAI",
        "description": "Optional live email analysis; demo mode uses local scheduling heuristics.",
        "services": ["LLM / OpenAI"],
        "fields": [
            {"key": "api_key", "label": "API Key", "required": True, "secret": True},
            {"key": "model", "label": "Model", "required": True, "secret": False},
            {
                "key": "system_prompt",
                "label": "System Prompt",
                "required": False,
                "secret": False,
                "multiline": True,
            },
        ],
    },
]


def get_integration_spec(provider: str) -> Dict[str, Any]:
    for spec in INTEGRATION_SPECS:
        if spec["provider"] == provider:
            return spec
    raise KeyError("Unknown integration provider: %s" % provider)


def normalize_integration_values(spec: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for field in spec["fields"]:
        raw_value = values.get(field["key"], "")
        normalized[field["key"]] = str(raw_value).strip()
    return normalized


def build_integration_snapshot(
    spec: Dict[str, Any],
    stored_values: Dict[str, str],
    updated_at: str | None,
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    metadata = metadata or {}
    missing_required = [
        field["key"]
        for field in spec["fields"]
        if field.get("required") and not stored_values.get(field["key"])
    ]
    app_ready = not missing_required
    warnings = [str(item) for item in metadata.get("warnings", []) if str(item).strip()]
    has_calendar_api_warning = any("Google Calendar API" in warning for warning in warnings)
    account_connected = bool(metadata.get("account_connected") or metadata.get("connected_email"))
    calendar_connected = bool(metadata.get("selected_calendar_id"))
    calendar_required = bool(spec.get("calendar_connection"))
    configured = (
        bool(spec.get("implemented", True))
        and app_ready
        and (account_connected if spec.get("oauth_login") else True)
        and (calendar_connected if calendar_required else True)
        and not warnings
    )
    filled_fields = sum(1 for value in stored_values.values() if value)
    if not spec.get("implemented", True):
        status_label = "Not implemented"
        status_tone = "warn"
    elif configured:
        status_label = "Configured"
        status_tone = "ok"
    elif has_calendar_api_warning:
        status_label = "Enable Calendar API"
        status_tone = "warn"
    elif account_connected and warnings:
        status_label = "Partial"
        status_tone = "warn"
    elif calendar_required and account_connected and not calendar_connected:
        status_label = "Awaiting calendar"
        status_tone = "warn"
    elif spec.get("oauth_login") and app_ready:
        status_label = "Awaiting login"
        status_tone = "warn"
    else:
        status_label = "Not configured"
        status_tone = "warn"
    return {
        "provider": spec["provider"],
        "title": spec["title"],
        "description": spec["description"],
        "services": spec.get("services", []),
        "oauth_login": bool(spec.get("oauth_login")),
        "fields": spec["fields"],
        "values": stored_values,
        "configured": configured,
        "app_ready": app_ready,
        "account_connected": account_connected,
        "connected_email": metadata.get("connected_email"),
        "granted_scopes": metadata.get("granted_scopes", []),
        "calendar_summaries": metadata.get("calendar_summaries", []),
        "calendar_connected": calendar_connected,
        "selected_calendar_id": metadata.get("selected_calendar_id"),
        "selected_calendar_summary": metadata.get("selected_calendar_summary"),
        "last_calendar_sync_at": metadata.get("last_calendar_sync_at"),
        "warnings": warnings,
        "has_calendar_api_warning": has_calendar_api_warning,
        "google_project_hint": metadata.get("google_project_hint"),
        "status_label": status_label,
        "status_tone": status_tone,
        "missing_required": missing_required,
        "filled_fields": filled_fields,
        "total_fields": len(spec["fields"]),
        "updated_at": updated_at,
    }
