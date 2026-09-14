from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import OpenAI

DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"

DEFAULT_SYSTEM_PROMPT = """
You are the language-understanding layer for an executive scheduling assistant.
Extract only scheduling facts from emails and short dashboard notes.

Return JSON only with this exact shape:
{
  "intent": "new_request|accept|propose_time|reschedule|cancel|general",
  "priority": "low|normal|high|critical",
  "mode": "online|offline|flexible",
  "duration_minutes": 30,
  "objective": "short objective",
  "summary": "one-sentence summary",
  "agenda": "short agenda",
  "requested_location": "optional place or null",
  "availability_hints": [
    {
      "start": "2026-04-10T13:00:00+03:00",
      "end": "2026-04-10T14:00:00+03:00",
      "exact": false,
      "source_text": "tomorrow afternoon"
    }
  ]
}

Rules:
- Be conservative. If the email is vague, prefer "general" and an empty availability_hints list.
- Preserve only concrete scheduling facts.
- Use the provided local time reference and timezone when converting relative dates.
- Keep objective, summary, and agenda short and business-like.
- If no location is mentioned, use null for requested_location.
- If duration is not stated, return null for duration_minutes.
""".strip()

JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

ALLOWED_INTENTS = {"new_request", "accept", "propose_time", "reschedule", "cancel", "general"}
ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}
ALLOWED_MODES = {"online", "offline", "flexible"}


class OpenAIAnalysisError(RuntimeError):
    pass


class OpenAIEmailAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENAI_MODEL,
        system_prompt: str = "",
        timeout_seconds: float = 20.0,
        *,
        allow_network: bool = False,
    ):
        if not allow_network:
            raise OpenAIAnalysisError("OpenAI network access requires explicit opt-in.")
        self.client = OpenAI(api_key=api_key, timeout=timeout_seconds)
        self.model = model or DEFAULT_OPENAI_MODEL
        self.system_prompt = (system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT

    def analyze_message(
        self,
        *,
        subject: str,
        body: str,
        reference_at: datetime,
        timezone_name: str,
        default_duration_minutes: int,
        forced_intent: Optional[str] = None,
    ) -> Dict[str, Any]:
        response = self.client.responses.create(
            model=self.model,
            instructions=self.system_prompt,
            input=self._build_prompt(
                subject=subject,
                body=body,
                reference_at=reference_at,
                timezone_name=timezone_name,
                default_duration_minutes=default_duration_minutes,
                forced_intent=forced_intent,
            ),
            max_output_tokens=1200,
        )
        payload = self._parse_output(getattr(response, "output_text", "") or "")
        return self._normalize_payload(payload)

    def _build_prompt(
        self,
        *,
        subject: str,
        body: str,
        reference_at: datetime,
        timezone_name: str,
        default_duration_minutes: int,
        forced_intent: Optional[str],
    ) -> str:
        forced_line = forced_intent or "none"
        return (
            "Analyze this scheduling-related message.\n\n"
            f"Timezone: {timezone_name}\n"
            f"Local reference datetime: {reference_at.isoformat()}\n"
            f"Default duration if missing: {default_duration_minutes} minutes\n"
            f"Forced intent: {forced_line}\n\n"
            f"Subject:\n{subject.strip()}\n\n"
            f"Body:\n{body.strip()}\n"
        )

    def _parse_output(self, text: str) -> Dict[str, Any]:
        raw = text.strip()
        if not raw:
            raise OpenAIAnalysisError("OpenAI returned an empty response.")
        if raw.startswith("{") and raw.endswith("}"):
            return self._load_json(raw)
        match = JSON_BLOCK_RE.search(raw)
        if match:
            return self._load_json(match.group(0))
        raise OpenAIAnalysisError("OpenAI did not return JSON.")

    def _load_json(self, raw: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenAIAnalysisError("OpenAI returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise OpenAIAnalysisError("OpenAI returned JSON in an unexpected shape.")
        return parsed

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}

        intent = str(payload.get("intent") or "").strip().lower()
        if intent in ALLOWED_INTENTS:
            normalized["intent"] = intent

        priority = str(payload.get("priority") or "").strip().lower()
        if priority in ALLOWED_PRIORITIES:
            normalized["priority"] = priority

        mode = str(payload.get("mode") or "").strip().lower()
        if mode in ALLOWED_MODES:
            normalized["mode"] = mode

        duration_raw = payload.get("duration_minutes")
        if isinstance(duration_raw, str) and duration_raw.strip().isdigit():
            duration_raw = int(duration_raw.strip())
        if isinstance(duration_raw, (int, float)) and int(duration_raw) > 0:
            normalized["duration_minutes"] = int(duration_raw)

        for key in ["objective", "summary", "agenda"]:
            value = str(payload.get(key) or "").strip()
            if value:
                normalized[key] = value[:280]

        requested_location = payload.get("requested_location")
        if requested_location is None:
            normalized["requested_location"] = None
        else:
            location_text = str(requested_location).strip()
            normalized["requested_location"] = location_text or None

        hints: List[Dict[str, Any]] = []
        raw_hints = payload.get("availability_hints")
        if isinstance(raw_hints, list):
            for item in raw_hints:
                if not isinstance(item, dict):
                    continue
                start = str(item.get("start") or "").strip()
                end = str(item.get("end") or "").strip()
                if not start or not end:
                    continue
                hints.append(
                    {
                        "start": start,
                        "end": end,
                        "exact": bool(item.get("exact", False)),
                        "source_text": str(item.get("source_text") or "").strip()[:120],
                    }
                )
        normalized["availability_hints"] = hints
        return normalized
