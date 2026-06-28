"""Lightweight audit logging and TTL caching for agent requests."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class AgentExecutionCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.time():
            self._entries.pop(key, None)
            return None
        return payload

    def set(self, key: str, payload: dict[str, Any]) -> None:
        self._entries[key] = (time.time() + self._ttl_seconds, payload)


class AgentAuditLogger:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def record(self, event: dict[str, Any]) -> str:
        audit_id = str(uuid.uuid4())
        payload = {"audit_id": audit_id, **event, "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
        except Exception as exc:
            logger.warning("agent_audit_write_failed error_type=%s", type(exc).__name__, exc_info=exc)
        return audit_id


def build_agent_cache_key(*, scope: str, plan: dict[str, Any], allowed_client_slugs: tuple[str, ...]) -> str:
    normalized = json.dumps(
        {
            "scope": scope,
            "plan": plan,
            "allowed_client_slugs": allowed_client_slugs,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


agent_execution_cache = AgentExecutionCache(settings.AGENT_CACHE_TTL_S)
agent_audit_logger = AgentAuditLogger(settings.AGENT_AUDIT_LOG_PATH)
