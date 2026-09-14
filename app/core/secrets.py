from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken


class SecretVault:
    def __init__(self, key_path: str):
        self.key_path = Path(key_path)
        self._fernet = Fernet(self._load_or_create_key())

    def encrypt_json(self, payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        token = self._fernet.encrypt(raw)
        return token.decode("utf-8")

    def decrypt_json(self, payload: str) -> Dict[str, Any]:
        if not payload:
            return {}
        stripped = payload.strip()
        if stripped.startswith("{"):
            data = json.loads(stripped)
            return {str(key): value for key, value in data.items()}
        try:
            decrypted = self._fernet.decrypt(stripped.encode("utf-8"))
            data = json.loads(decrypted.decode("utf-8"))
            return {str(key): value for key, value in data.items()}
        except (InvalidToken, json.JSONDecodeError):
            return {}

    def _load_or_create_key(self) -> bytes:
        env_value = os.getenv("APP_SECRETS_KEY")
        if env_value:
            return self._normalize_key(env_value)

        if self.key_path.exists():
            return self._normalize_key(self.key_path.read_text(encoding="utf-8").strip())

        self.key_path.write_text(Fernet.generate_key().decode("utf-8"), encoding="utf-8")
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return self._normalize_key(self.key_path.read_text(encoding="utf-8").strip())

    def _normalize_key(self, raw_key: str) -> bytes:
        raw = raw_key.encode("utf-8")
        try:
            base64.urlsafe_b64decode(raw)
            return raw
        except Exception:
            return base64.urlsafe_b64encode(raw.ljust(32, b"0")[:32])
